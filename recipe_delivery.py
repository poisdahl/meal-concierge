"""Render only a saved menu and its managed covers. No URLs or host paths opened."""
from __future__ import annotations

from copy import deepcopy
from email.message import EmailMessage
from email.policy import SMTP
import html
from html.parser import HTMLParser
import io
from pathlib import Path
import re

from recipe_assets import RecipeAssetError
from recipe_email import _plain_text, prepare_recipe_media
from service_common import format_portions, menu_email_html, meal_type_label


def render_menu(menu, assets, *, images=True):
    media = prepare_recipe_media(menu, assets, images_supported=images)
    covers = {}
    for descriptor in media["inline_images"]:
        try:
            covers[descriptor["content_id"]] = assets.read(descriptor["asset_id"])
        except RecipeAssetError:
            media["image_cids"].pop(descriptor["asset_id"], None)
            media["image_warnings"].append({"reason": "cover_missing_or_invalid"})
    warnings = [w["reason"] for w in media["image_warnings"]] if images else []
    frozen = deepcopy(menu)
    for group in ("dishes", "salads"):
        for recipe in frozen.get(group, []):
            missing = []
            if (recipe.get("rights") or {}).get("storage") == "link_only":
                missing.append("Kilden tillater bare lenke; full oppskrift er ikke tilgjengelig her.")
            else:
                if not recipe.get("ingredients"):
                    missing.append("Ingredienser mangler i kilden.")
                if not recipe.get("steps"):
                    missing.append("Fremgangsmåte mangler i kilden.")
            if missing:
                recipe["notes"] = "\n".join(filter(None, [recipe.get("notes"), *missing]))
                warnings.extend(missing)
    full = menu_email_html(frozen, image_cids=media["image_cids"])
    # Saved structured slots carry canonical dates; legacy schedule text alone
    # must not be mistaken for dated slots.
    dates = []
    names = {r.get("recipe_key"): r.get("name", "")
             for group in ("dishes", "salads") for r in frozen.get(group, [])}
    for slot in frozen.get("slots", []):
        dates.append(" · ".join(str(v) for v in (slot.get("date"), meal_type_label(slot.get("meal_type")), names.get(slot.get("recipe_key")),
                     f"{format_portions(slot['portions'])} porsjoner" if slot.get("portions") else None) if v))
    if dates:
        position = full.index("</h1>") + len("</h1>")
        full = full[:position] + "<h2>Datoer</h2>" + "".join("<p>" + html.escape(v) + "</p>" for v in dates) + full[position:]
    return {"html": full, "text": _plain_text(full), "covers": covers,
            "warnings": list(dict.fromkeys(warnings))}


def split_text(document, maximum):
    """Preserve every character, preferring recipe, section, then line boundaries.

    Limits count UTF-8 bytes, which also bounds Unicode codepoints and UTF-16
    units used by native clients. No content is truncated or re-resolved.
    """
    sections = document.split('<section class="recipe">')
    blocks = []
    for section in sections:
        value = _plain_text(section)
        if value:
            if len(value.encode()) <= maximum:
                blocks.append(value)
            else:
                # Keep an entire ingredient/step section together whenever it
                # fits a fresh message, before considering line/word splits.
                blocks.extend(text for raw in re.split(r"(?=<h3>)", section)
                              if (text := _plain_text(raw)))
    parts = []
    for block in blocks:
        if parts and len((parts[-1] + "\n\n" + block).encode()) <= maximum:
            parts[-1] += "\n\n" + block
            continue
        rest = block
        while rest:
            if len(rest.encode()) <= maximum:
                parts.append(rest)
                break
            prefix = rest.encode()[:maximum].decode("utf-8", errors="ignore")
            stop = prefix.rfind("\n") + 1
            if stop < len(prefix) // 2:
                stop = prefix.rfind(" ") + 1
            if not stop:
                stop = len(prefix)
            parts.append(rest[:stop])
            rest = rest[stop:]
    return parts


def limit_images(rendered, maximum):
    """Apply an email sender's actual per-inline-attachment byte bound."""
    result = {**rendered, "covers": {cid: data for cid, data in rendered["covers"].items() if len(data) <= maximum}}
    removed = rendered["covers"].keys() - result["covers"].keys()
    for cid in removed:
        result["html"] = re.sub(r'<img src="cid:' + re.escape(cid) + r'"[^>]*>', '', result["html"])
    result["warnings"] = [*rendered["warnings"], *(["image exceeds native inline attachment limit"] if removed else [])]
    return result


class _PDFBlocks(HTMLParser):
    """Convert our escaped HTML to plain text blocks; no renderer HTML input."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buffer, self.links = [], [], []
        self.kind, self.skip = "p", 0

    def flush(self):
        if self.buffer:
            self.blocks.append((self.kind, "".join(self.buffer)))
            self.buffer = []

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "style", "script"}:
            self.skip += 1
        if self.skip:
            return
        if tag in {"p", "li", "h1", "h2", "h3", "section"}:
            self.flush()
            self.kind = tag
            if tag == "li":
                self.buffer.append("• ")
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))
        if tag == "img":
            self.flush()
            self.blocks.append(("image", dict(attrs).get("src", "")[4:]))

    def handle_endtag(self, tag):
        if tag in {"head", "style", "script"} and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "a" and self.links:
            self.buffer.append(" (" + self.links.pop() + ")")
        if tag in {"p", "li", "h1", "h2", "h3", "section"}:
            self.flush()

    def handle_data(self, data):
        if not self.skip:
            self.buffer.append(data)


def render_pdf(rendered):
    import reportlab
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Image, Spacer

    fonts = Path(reportlab.__file__).parent / "fonts"
    for name, filename in (("MCVera", "Vera.ttf"), ("MCVeraBold", "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(fonts / filename)))
    styles = {"p": ParagraphStyle("body", fontName="MCVera", fontSize=10, leading=15,
                                  spaceAfter=7, splitLongWords=True)}
    for tag, size in (("h1", 21), ("h2", 15), ("h3", 11)):
        styles[tag] = ParagraphStyle(tag, parent=styles["p"], fontName="MCVeraBold",
                                    fontSize=size, leading=size * 1.3, textColor=colors.HexColor("#173f35"),
                                    spaceBefore=12, keepWithNext=True)
    parser = _PDFBlocks()
    parser.feed(rendered["html"])
    parser.flush()
    story = []
    for kind, value in parser.blocks:
        if kind == "image":
            data = rendered["covers"].get(value)
            if data:
                picture = Image(io.BytesIO(data))
                scale = min(440 / picture.imageWidth, 235 / picture.imageHeight, 1)
                picture.drawWidth, picture.drawHeight = picture.imageWidth * scale, picture.imageHeight * scale
                picture.hAlign = "LEFT"
                story.extend([picture, Spacer(1, 8)])
        elif value.strip():
            story.append(Paragraph(html.escape(value).replace("\n", "<br/>"), styles.get(kind, styles["p"])))
    output = io.BytesIO()
    def footer(canvas, doc):
        canvas.setFont("MCVera", 8)
        canvas.drawRightString(A4[0] - 48, 25, str(doc.page))
    SimpleDocTemplate(output, pagesize=A4, leftMargin=48, rightMargin=48, topMargin=38,
                      bottomMargin=44, title="Ukesmeny og oppskrifter", author="Meal Concierge").build(
                          story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def render_email(rendered, *, recipient, sender, subject, pdf=None):
    message = EmailMessage(policy=SMTP)
    message["To"], message["From"], message["Subject"] = recipient, sender, subject
    message.set_content(rendered["text"])
    message.add_alternative(rendered["html"], subtype="html")
    body = message.get_payload()[-1]
    for cid, data in rendered["covers"].items():
        body.add_related(data, maintype="image", subtype="jpeg", cid="<" + cid + ">", disposition="inline")
    if pdf is not None:
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename="ukesmeny.pdf")
    return message.as_bytes()
