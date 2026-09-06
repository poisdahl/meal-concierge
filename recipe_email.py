"""Frozen recipe-cover descriptors and local MIME construction; never sends.

The Application supplies frozen HTML and image records. A sender on the data
host resolves digest references here rather than exposing paths/bytes via MCP.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP
from html.parser import HTMLParser
import re
from typing import Any, Mapping

from core import HouseholdError
from recipe_assets import RecipeAssets, RecipeAssetError, asset_filename


def _descriptor(asset_id: str) -> dict[str, str]:
    filename = asset_filename(asset_id)
    return {"asset_id": asset_id, "content_id": "recipe-" + filename[:-4] + "@meal-concierge.local",
            "filename": filename, "content_type": "image/jpeg"}


def prepare_recipe_media(menu_snapshot: Mapping[str, Any], assets: RecipeAssets, *, images_supported: bool = False) -> dict[str, Any]:
    """Resolve only frozen cover references, never current bank entries or URLs.

    Attribution remains in the frozen recipe and is rendered independently by
    menu_email_html, including when this destination cannot display images.
    """
    result = {"image_cids": {}, "inline_images": [], "image_warnings": []}
    seen = set()
    for group in ("dishes", "salads"):
        rows = menu_snapshot.get(group)
        if not isinstance(rows, list):
            continue
        for recipe in rows:
            cover = recipe.get("image") if isinstance(recipe, Mapping) else None
            if not isinstance(cover, Mapping):
                continue
            asset_id = cover.get("asset_id")
            if not isinstance(asset_id, str) or asset_id in seen:
                continue
            seen.add(asset_id)
            if images_supported is not True:
                result["image_warnings"].append({"asset_id": asset_id, "reason": "destination_has_no_inline_image_support"})
                continue
            try:
                descriptor = _descriptor(asset_id)
                assets.read(asset_id)
            except RecipeAssetError:
                result["image_warnings"].append({"asset_id": asset_id, "reason": "cover_missing_or_invalid"})
                continue
            result["image_cids"][asset_id] = descriptor["content_id"]
            result["inline_images"].append(descriptor)
    return result


class _ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "script", "style"}:
            self.skip += 1
        if self.skip:
            return
        if tag in {"p", "h1", "h2", "h3", "li", "br", "section"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))

    def handle_endtag(self, tag):
        if tag in {"head", "script", "style"} and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "a" and self.links:
            href = self.links.pop()
            if href:
                self.parts.append(" (" + href + ")")
        if tag in {"p", "h1", "h2", "h3", "li", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _plain_text(html: str) -> str:
    parser = _ReadableHTML()
    parser.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


class _CoverReferences(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.cids = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        source = dict(attrs).get("src", "")
        if not isinstance(source, str) or not source.startswith("cid:") or not source[4:]:
            raise RecipeAssetError("recipe email image must use a local inline reference")
        self.cids.add(source[4:])


def build_message(payload: Mapping[str, Any], assets: RecipeAssets, *, sender: str | None = None) -> EmailMessage:
    """Build a message for an existing sender, without network or send effects.

    Cover failure at sender time drops all inline parts and uses the supplied
    frozen fallback HTML. Never replace a missing frozen cover with a newer one.
    The caller retains due/begin_send/mark_sent and uncertain-outcome ownership.
    """
    for field in ("recipient", "subject", "html"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise HouseholdError(f"recipe email payload requires {field}")
    descriptors = payload.get("inline_images") or []
    fallback = payload.get("html_without_images") or payload["html"]
    try:
        if not isinstance(fallback, str) or _CoverReferences(fallback).cids:
            raise RecipeAssetError("fallback HTML contains images")
    except RecipeAssetError as exc:
        raise HouseholdError("recipe email requires frozen HTML without inline image references") from exc
    html = payload["html"]
    inline = {}
    try:
        if not isinstance(descriptors, list):
            raise RecipeAssetError("invalid inline image descriptors")
        for descriptor in descriptors:
            if not isinstance(descriptor, Mapping):
                raise RecipeAssetError("invalid inline image descriptor")
            expected = _descriptor(descriptor.get("asset_id"))
            if dict(descriptor) != expected:
                raise RecipeAssetError("inline image descriptor does not match its managed asset")
            inline[expected["content_id"]] = (expected, assets.read(expected["asset_id"]))
        references = _CoverReferences(html).cids
        if references != set(inline):
            raise RecipeAssetError("inline HTML references do not match the resolved assets")
    except RecipeAssetError:
        html = fallback
        inline = {}
    message = EmailMessage(policy=SMTP)
    try:
        message["To"] = payload["recipient"]
        message["Subject"] = payload["subject"]
        if sender is not None:
            message["From"] = sender
    except (ValueError, TypeError) as exc:
        raise HouseholdError("recipe email headers are invalid") from exc
    message.set_content(_plain_text(fallback))
    message.add_alternative(html, subtype="html")
    html_part = message.get_payload()[-1]
    for content_id, (descriptor, data) in inline.items():
        html_part.add_related(data, maintype="image", subtype="jpeg", cid="<" + content_id + ">",
                              filename=descriptor["filename"], disposition="inline")
    return message
