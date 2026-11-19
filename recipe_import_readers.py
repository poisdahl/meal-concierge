"""Bounded, offline extraction of untrusted native recipe source content.

RecipeSage JSON-LD export shape is pinned to v4.0.6:
https://github.com/julianpoy/recipesage/blob/v4.0.6/packages/util/server/src/general/jsonLD.ts
https://github.com/julianpoy/recipesage/blob/v4.0.6/packages/util/server/src/general/queue/export/handlers/jsonldExportJobHandler.ts
Webpage input supports Recipe objects in JSON-LD objects, arrays and @graph,
with text ingredients and text/HowToStep/HowToSection instructions as described
at https://schema.org/Recipe. This is not a general JSON-LD processor.

JSON readers accept supplied content; native ZIP readers open only the explicit
staged file. No functions fetch URLs, save recipes or grant provider/origin/user
acceptance. Callers must retain the extraction report and establish their own
trusted import context before using source_candidate at the service boundary.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from html import unescape
from html.parser import HTMLParser
import json
import hashlib
import math
from pathlib import Path
import re
import stat
from typing import Any
import unicodedata
from urllib.parse import parse_qsl, urlsplit
import zipfile
import zlib

from core import contains_line_controls


MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_WEBPAGE_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_RECORDS = 10_000
MAX_JSONLD_SCRIPTS = 64
MAX_TEXT = 4_000  # Match the stored recipe step/notes boundary.
MAX_MEALIE_COVER_BYTES = 12 * 1024 * 1024
_CREDENTIAL_KEYS = {
    "access_token", "api_key", "apikey", "authorization", "auth", "token",
    "password", "passwd", "secret", "key", "signature", "sig",
    "x-amz-credential", "x-amz-signature", "x-goog-credential", "x-goog-signature",
}
_SUPPORTED_FIELDS = {
    "@context", "@type", "name", "identifier", "inLanguage", "description",
    "recipeIngredient", "recipeInstructions", "recipeYield", "recipeCategory",
    "prepTime", "cookTime", "totalTime", "creditText", "isBasedOn", "comment",
    "image", "author",
}


class RecipeImportReaderError(ValueError):
    """The source cannot be read completely under the declared input contract."""


def _input_text(data: bytes | str, maximum: int) -> str:
    try:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if not isinstance(raw, bytes) or len(raw) > maximum:
            raise RecipeImportReaderError("source input is invalid or too large")
        return raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise RecipeImportReaderError("source input must be valid UTF-8") from exc


def _json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            _text(key, "source field name", 1000)
            if any(ord(char) < 32 for char in key):
                raise RecipeImportReaderError("source field name contains control characters")
            if key in result:
                raise RecipeImportReaderError("source JSON contains duplicate keys")
            result[key] = value
        return result

    def number(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise RecipeImportReaderError("source JSON contains a nonfinite number")
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_float=number,
                           parse_constant=number)
        pending = [(iter((value,)), 0)]
        while pending:
            children, depth = pending[-1]
            try:
                child = next(children)
            except StopIteration:
                pending.pop()
                continue
            if depth > 32:
                raise RecipeImportReaderError("source JSON nesting is too deep")
            if isinstance(child, dict):
                pending.append((iter(child.values()), depth + 1))
            elif isinstance(child, list):
                pending.append((iter(child), depth + 1))
        return value
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, RecipeImportReaderError):
            raise
        raise RecipeImportReaderError("source JSON is invalid") from exc


def _text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise RecipeImportReaderError(f"{field} must be bounded text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise RecipeImportReaderError(f"{field} contains invalid Unicode") from exc
    # Do not retain credentials embedded in otherwise ordinary source wording.
    for url in re.findall(r"https?://[^\s<>\"']+", value, re.IGNORECASE):
        _url(url, field)
    return value


def _label(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if isinstance(value, str) and contains_line_controls(value):
        raise RecipeImportReaderError(f"{field} must be single-line text without directional controls")
    return _text(value, field, maximum, required=required)


def _url(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise RecipeImportReaderError(f"{field} must be an HTTP(S) URL")
    try:
        value.encode("utf-8")
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "\\" in value or parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError
        parameters = parse_qsl(parsed.query, keep_blank_values=True) + parse_qsl(parsed.fragment, keep_blank_values=True)
        if any(key.casefold() in _CREDENTIAL_KEYS for key, _ in parameters):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise RecipeImportReaderError(f"{field} contains an invalid or credential-bearing URL") from exc
    return value


def _type(value: Any, expected: str) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(item in (expected, f"https://schema.org/{expected}", f"http://schema.org/{expected}") for item in values)


def _strings(value: Any, field: str, maximum: int, count: int) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not 1 <= len(values) <= count:
        raise RecipeImportReaderError(f"{field} must contain one to {count} text entries")
    return [_text(item, field, maximum, required=True) for item in values]


def _instructions(value: Any, unsupported: set[str], depth: int = 0) -> list[str]:
    if depth > 8:
        raise RecipeImportReaderError("recipeInstructions nesting is too deep")
    values = [value] if isinstance(value, (str, dict)) else value
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise RecipeImportReaderError("recipeInstructions must contain one to 100 steps")
    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            result.append(_text(item, "recipeInstructions", MAX_TEXT, required=True))
        elif isinstance(item, dict) and _type(item.get("@type"), "HowToSection"):
            unsupported.update(f"recipeInstructions.{key}" for key in set(item) - {"@type", "name", "itemListElement"})
            name = _label(item.get("name"), "section name", 500)
            if name:
                result.append(f"[{name}]")
            if item.get("itemListElement") is not None:
                result.extend(_instructions(item["itemListElement"], unsupported, depth + 1))
            if not name and item.get("itemListElement") is None:
                raise RecipeImportReaderError("recipeInstructions section has no name or steps")
        elif isinstance(item, dict) and _type(item.get("@type"), "HowToStep"):
            unsupported.update(f"recipeInstructions.{key}" for key in set(item) - {"@type", "text"})
            result.append(_text(item.get("text"), "step text", MAX_TEXT, required=True))
        else:
            raise RecipeImportReaderError("unsupported recipeInstructions entry")
        if len(result) > 100:
            raise RecipeImportReaderError("recipeInstructions exceeds 100 entries")
    return result


def _extract(raw: Any, *, kind: str, source_url: str | None, allow_incomplete: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict) or not _type(raw.get("@type"), "Recipe"):
        raise RecipeImportReaderError("source entry must be a JSON-LD Recipe")
    try:
        size = len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except UnicodeError as exc:
        raise RecipeImportReaderError("recipe contains invalid Unicode") from exc
    if size > MAX_RECORD_BYTES:
        raise RecipeImportReaderError("source recipe is too large")
    unsupported = set(raw) - _SUPPORTED_FIELDS
    tolerant_metadata = kind == "web" and allow_incomplete
    name = _label(raw.get("name"), "name", 300, required=not (allow_incomplete and raw.get("name") is None))
    notes = []
    comments = raw.get("comment") or []
    if tolerant_metadata and isinstance(comments, dict):
        comments = [comments]
    if not isinstance(comments, list) or len(comments) > 100:
        if not tolerant_metadata:
            raise RecipeImportReaderError("comment must be a bounded list")
        unsupported.add("comment")
        comments = []
    for comment in comments:
        if isinstance(comment, dict) and comment.get("name") == "Author Notes" and _type(comment.get("@type"), "Comment"):
            notes.append(_text(comment.get("text"), "author notes", MAX_TEXT))
            unsupported.update(f"comment.{key}" for key in set(comment) - {"@type", "name", "text"})
        else:
            unsupported.add("comment")
    author = raw.get("author")
    if isinstance(author, dict):
        unsupported.update(f"author.{key}" for key in set(author) - {"@type", "name"})
        author = author.get("name")
    elif author is not None and not isinstance(author, str):
        unsupported.add("author")
        author = None
    images = raw.get("image") or []
    if isinstance(images, (str, dict)):
        images = [images]
    if not isinstance(images, list) or len(images) > 100:
        raise RecipeImportReaderError("image must be a bounded list")
    image_candidates = []
    for item in images:
        if isinstance(item, dict):
            unsupported.update(f"image.{key}" for key in set(item) - {"@type", "url", "contentUrl"})
            item = item.get("contentUrl") or item.get("url")
        try:
            url = _url(item, "image")
        except RecipeImportReaderError:
            if not tolerant_metadata:
                raise
            # Media is optional and inert. Do not echo or retain signed,
            # credential-bearing or relative URLs as managed image candidates.
            unsupported.add("image")
            continue
        if not url:
            unsupported.add("image")
        elif url not in image_candidates:
            image_candidates.append(url)
    tags = raw.get("recipeCategory") or []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list) or len(tags) > 50:
        raise RecipeImportReaderError("recipeCategory must be a bounded text list")
    original_url = _url(raw.get("isBasedOn"), "isBasedOn")
    credit = _text(raw.get("creditText"), "creditText", 300)
    ingredient_input = raw.get("recipeIngredient")
    if tolerant_metadata and isinstance(ingredient_input, list):
        if len(ingredient_input) > 200:
            raise RecipeImportReaderError("recipeIngredient must contain at most 200 text entries")
        without_empty = [line for line in ingredient_input if not isinstance(line, str) or line.strip()]
        if without_empty != ingredient_input:
            unsupported.add("recipeIngredient.empty")
        ingredient_input = without_empty
    identifier = raw.get("identifier")
    if tolerant_metadata and type(identifier) is int:
        identifier = str(identifier)  # An exact numeric ID has no culinary units to infer.
    elif tolerant_metadata and identifier is not None and not isinstance(identifier, str):
        unsupported.add("identifier")
        identifier = None
    times = {}
    for key, field in (("prep", "prepTime"), ("cook", "cookTime"), ("total", "totalTime")):
        if raw.get(field):
            if tolerant_metadata and not isinstance(raw[field], str):
                unsupported.add(field)  # A bare number cannot establish minutes or seconds.
                continue
            times[key] = _text(raw[field], field, 1000)
    extracted = {
        "name": name,
        "language": _text(raw.get("inLanguage"), "inLanguage", 20) or "und",
        "ingredients": ([] if allow_incomplete and ingredient_input in (None, "", []) else
                        _strings(ingredient_input, "recipeIngredient", 500, 200)),
        "steps": ([] if allow_incomplete and raw.get("recipeInstructions") in (None, "", []) else
                  _instructions(raw.get("recipeInstructions"), unsupported)),
        "yield_text": _web_yield_text(raw.get("recipeYield")),
        "description": _text(raw.get("description"), "description", MAX_TEXT),
        "notes": _text("\n\n".join(notes), "author notes", MAX_TEXT),
        "tags": [_text(tag, "recipeCategory", 80, required=True) for tag in tags],
        "times": times,
        "credit": credit,
        "source": {
            "kind": kind, "publisher": "RecipeSage" if kind == "recipesage" else None,
            "title": name, "author": _text(author, "author", 200) or None,
            "url": source_url, "external_id": _text(identifier, "identifier", 300) or None,
            "relationship": "user_supplied",
            "original": {"url": original_url, "publisher": credit or None},
        },
    }
    encoded_source_text = {}
    if kind == "web":
        for field in ("name", "ingredients", "steps", "yield_text", "description", "notes", "tags"):
            original = extracted[field]
            decoded = ([_decoded_web_prose(item) for item in original] if isinstance(original, list)
                       else _decoded_web_prose(original))
            if decoded != original:
                encoded_source_text[field] = original
                extracted[field] = decoded
        extracted["source"]["title"] = extracted["name"]
    return {"extracted": extracted, "image_candidates": image_candidates,
            **({"encoded_source_text": encoded_source_text} if encoded_source_text else {}),
            "unsupported_fields": sorted(unsupported),
            "image_status": "requires_asset_import" if image_candidates else "none"}


def _web_yield_text(value: Any) -> str:
    # Publishers use numbers and multiple labels (e.g. servings and pieces).
    # Preserve the labels together; a bare number is not proof of person portions.
    if isinstance(value, list):
        if not 1 <= len(value) <= 8 or any(isinstance(item, list) for item in value):
            raise RecipeImportReaderError("recipeYield must be a bounded list of labels")
        return _text("; ".join(_web_yield_text(item) for item in value), "recipeYield", 500)
    if type(value) in (int, float):
        if not math.isfinite(value) or value <= 0:
            raise RecipeImportReaderError("recipeYield number must be finite and positive")
        value = str(value)
    return _text(value, "recipeYield", 500)


def read_recipesage_export(data: bytes | str) -> list[dict[str, Any]]:
    """Read v4.0.6 {recipes: [JSON-LD Recipe, ...]} without trusting metadata."""
    value = _json(_input_text(data, MAX_EXPORT_BYTES))
    if not isinstance(value, dict) or set(value) != {"recipes"} or not isinstance(value["recipes"], list):
        raise RecipeImportReaderError("RecipeSage export must contain only a recipes list")
    if len(value["recipes"]) > MAX_RECORDS:
        raise RecipeImportReaderError("RecipeSage export has too many recipes")
    return [_extract(raw, kind="recipesage", source_url=None) for raw in value["recipes"]]


class _JSONLDScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and any(key == "type" and (value or "").strip().casefold() == "application/ld+json" for key, value in attrs):
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None
            if len(self.scripts) > MAX_JSONLD_SCRIPTS:
                raise RecipeImportReaderError("webpage has too many JSON-LD scripts")


def read_webpage_jsonld(data: bytes | str, *, source_url: str, allow_empty: bool = False,
                        allow_incomplete: bool = False) -> list[dict[str, Any]]:
    """Extract every Recipe, preserving ambiguity for the caller to resolve."""
    url = _url(source_url, "source_url")
    if url is None or not url.startswith("https://"):
        raise RecipeImportReaderError("webpage source_url must use HTTPS")
    parser = _JSONLDScripts()
    parser.feed(_input_text(data, MAX_WEBPAGE_BYTES))
    parser.close()
    if parser.current is not None:
        raise RecipeImportReaderError("webpage contains an unclosed JSON-LD script")
    result = []
    for script in parser.scripts:
        pending = [_json(script)]
        while pending:
            value = pending.pop()
            if isinstance(value, list):
                pending.extend(reversed(value))
            elif isinstance(value, dict):
                if _type(value.get("@type"), "Recipe"):
                    result.append(_extract(value, kind="web", source_url=url, allow_incomplete=allow_incomplete))
                    if len(result) > MAX_RECORDS:
                        raise RecipeImportReaderError("webpage has too many recipes")
                if "@graph" in value:
                    pending.append(value["@graph"])
    if not result and not allow_empty:
        raise RecipeImportReaderError("webpage contains no supported JSON-LD Recipe")
    return result



MAX_WEBPAGE_TEXT_BYTES = 64 * 1024


class _WebpageText(HTMLParser):
    """Plain text fallback; no rendering, external resources or script execution."""
    _ignored = {"head", "script", "style", "template", "noscript", "svg", "canvas", "iframe", "object", "nav", "footer", "aside"}
    _blocks = {"article", "section", "main", "div", "p", "li", "ul", "ol", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "dl", "dt", "dd", "caption"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, *, main_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.size = 0
        self.main_only = main_only
        self.found_main = False

    def _append(self, data: str) -> None:
        if self.main_only and not any(tag == "main" for tag, _ in self.stack):
            return
        self.size += len(data.encode("utf-8"))
        if self.size > MAX_WEBPAGE_TEXT_BYTES:
            raise RecipeImportReaderError("webpage text exceeds the supported interpretation limit")
        self.parts.append(data)

    @property
    def ignored(self) -> bool:
        return bool(self.stack and self.stack[-1][1])

    def _in_table_row(self) -> bool:
        row = False
        for tag, _ in reversed(self.stack):
            if tag == "table":
                return row
            if tag == "tr":
                row = True
        return False

    def _boundary(self, tag: str) -> None:
        self._append("\t" if tag in {"td", "th"} and self._in_table_row() else "\n")

    def lines(self):
        from recipe_quantities import parse_measure
        for line in "".join(self.parts).splitlines():
            cells = [" ".join(cell.split()) for cell in line.split("\t")]
            cells = [cell for cell in cells if cell]
            # An amount and its adjacent food are one usable source excerpt.
            # A layout table with independent recipes or headings keeps its cells.
            whole_cells = line.strip(" ")
            if (len(cells) == 2 and whole_cells.startswith("\t") and whole_cells.endswith("\t")
                    and cells[1][0].isalpha() and len(cells[0]) <= 100
                    and parse_measure(cells[0])[0] is not None):
                yield " ".join(cells)
            else:
                yield from cells

    def _close(self, names: set[str], boundaries: set[str] | frozenset[str] = frozenset()) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] in names:
                del self.stack[index:]
                return
            if self.stack[index][0] in boundaries:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML permits omission of these common end tags. Keep a hidden child
        # from suppressing the visible paragraph/list/table item that follows.
        if tag not in {"html", "head", "base", "link", "meta", "title", "style", "script", "noscript", "template"}:
            self._close({"head"}, {"template", "svg", "math"})
        if tag in self._blocks:
            self._close({"p"}, {"button", "html", "table", "td", "th", "object", "template", "svg", "math"})
        if tag in {"li", "dt", "dd", "tr", "td", "th"}:
            if tag == "li":
                names, containers = {"li"}, {"ul", "ol", "menu", "table"}
            elif tag in {"dt", "dd"}:
                names, containers = {"dt", "dd"}, {"dl", "table"}
            elif tag == "tr":
                names, containers = {"tr"}, {"table", "tbody", "thead", "tfoot"}
            else:
                names, containers = {"td", "th"}, {"tr", "table"}
            self._close(names, containers | {"template", "svg", "math"})
        attributes = {key: value for key, value in attrs}
        style: dict[str, tuple[str, bool]] = {}
        for declaration in (attributes.get("style") or "").split(";"):
            if ":" in declaration:
                key, value = declaration.split(":", 1)
                key, value = key.strip().casefold(), value.strip().casefold()
                important = re.search(r"!\s*important\s*$", value) is not None
                value = re.sub(r"\s*!\s*important\s*$", "", value)
                current = style.get(key)
                if current is None or important or not current[1]:
                    style[key] = (value, important)
        display = style.get("display", (None, False))[0]
        visibility = style.get("visibility", (None, False))[0]
        content_visibility = style.get("content-visibility", (None, False))[0]
        hidden = ("hidden" in attributes or "inert" in attributes
                  or (attributes.get("aria-hidden") or "").strip().casefold() == "true"
                  or display == "none"
                  or visibility in {"hidden", "collapse"}
                  or content_visibility == "hidden")
        ignored = self.ignored or tag in self._ignored or hidden
        if tag == "main" and not ignored:
            self.found_main = True
        if not ignored and tag in self._blocks:
            self._boundary(tag)
        if tag not in self._void:
            if len(self.stack) >= 128:
                raise RecipeImportReaderError("webpage nesting exceeds the supported text limit")
            self.stack.append((tag, ignored))

    def handle_endtag(self, tag: str) -> None:
        self._close({tag})
        if not self.ignored and tag in self._blocks:
            self._boundary(tag)

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            # HTML source indentation is not a rendered line break inside a cell.
            # Reserve tabs for cell boundaries. Explicit <br> and block tags
            # still emit newlines independently of the surrounding markup.
            if self._in_table_row() and not any(tag == "pre" for tag, _ in self.stack):
                data = re.sub(r"[ \t\r\n\f]+", " ", data)
            self._append(data.replace("\t", " "))


def _decoded_web_prose(value: str) -> str:
    """Decode publisher prose only; embedded HTML remains inert, never fetched."""
    for _ in range(4):
        decoded = unescape(value)
        if decoded == value:
            break
        value = decoded
    if re.search(r"</?[A-Za-z][^>]*>", value):
        parser = _WebpageText()
        parser.feed(value)
        parser.close()
        if any(tag in parser._ignored for tag, _ in parser.stack):
            raise RecipeImportReaderError("structured recipe prose has an unclosed non-text element")
        value = "\n".join(parser.lines())
    return value


def read_webpage(data: bytes | str, *, source_url: str) -> dict[str, Any]:
    """Prefer structured recipes, otherwise expose bounded text for host interpretation.

    A structured failure is explicit in the text result. Text has no inferred
    recipe fields or authority and requires a separate host interpretation.
    """
    raw = _input_text(data, MAX_WEBPAGE_BYTES)
    source_bytes = data.encode("utf-8") if isinstance(data, str) else data
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    structured_error = None
    try:
        recipes = read_webpage_jsonld(raw, source_url=source_url, allow_empty=True, allow_incomplete=True)
    except RecipeImportReaderError:
        # Do not guess that a malformed script was unrelated, or promote a
        # partial extraction to a complete recipe. Only visible text survives.
        recipes = []
        structured_error = {"code": "structured_recipe_unavailable",
                            "message": "Structured recipe data could not be read completely; interpret the bounded visible text explicitly."}
    # Classify each advertised record independently. One incomplete sibling
    # must not erase usable structured recipes, and a physical yield remains
    # valid structured evidence even though person portions stay unresolved.
    complete, incomplete = [], []
    for record in recipes:
        target = complete if (record["extracted"]["name"] and record["extracted"]["ingredients"] and record["extracted"]["steps"]
                              and record["extracted"]["yield_text"]) else incomplete
        target.append(record)
    if complete:
        return {"mode": "structured", "recipes": complete, "incomplete_recipes": incomplete,
                "source_sha256": source_sha256, "text": None, "requires_interpretation": False}
    parser = _WebpageText(main_only=True)
    parser.feed(raw)
    parser.close()
    if not parser.found_main:
        parser = _WebpageText()
        parser.feed(raw)
        parser.close()
    if any(tag in parser._ignored for tag, _ in parser.stack):
        raise RecipeImportReaderError("webpage text has an unclosed non-text element")
    visible_lines = list(parser.lines())
    def record_lines(record):
        extracted = record["extracted"]
        return [value for value in (
            extracted["name"], extracted["yield_text"], *extracted["ingredients"], *extracted["steps"],
            extracted["description"], extracted["notes"], *extracted["tags"]
        ) if value]
    def bounded_text(lines):
        retained, size = [], 0
        for line in lines:
            if retained and line == retained[-1]:
                continue
            encoded = len((line + "\n").encode("utf-8"))
            if size + encoded > MAX_WEBPAGE_TEXT_BYTES:
                break
            retained.append(line)
            size += encoded
        if not retained:
            raise RecipeImportReaderError("webpage has no readable recipe text; supply the recipe text or a readable attachment")
        return _text("\n".join(retained), "webpage text", MAX_WEBPAGE_TEXT_BYTES, required=True)
    if len(incomplete) > 1:
        record_values = [record_lines(record) for record in incomplete]
        if all(record_values):
            record_texts = [{"record_index": index, "text": bounded_text(lines)}
                            for index, lines in enumerate(record_values)]
            return {"mode": "text", "recipes": [], "incomplete_recipes": incomplete,
                    "record_texts": record_texts, "text": None, "source_url": _url(source_url, "source_url"),
                    "requires_record_selection": True, "requires_interpretation": True,
                    "source_sha256": source_sha256}
        structured_error = {"code": "incomplete_recipe_selection",
                            "message": "Incomplete structured entries cannot be separated reliably; explicitly interpret the selected visible recipe."}
    text = bounded_text([*(record_lines(incomplete[0]) if len(incomplete) == 1 else []), *visible_lines])
    return {"mode": "text", "recipes": [], "incomplete_recipes": incomplete, "text": text,
            "source_url": _url(source_url, "source_url"),
            "requires_interpretation": True, "source_sha256": source_sha256,
            **({"structured_error": structured_error} if structured_error else {})}


def source_candidate(record: dict[str, Any]) -> dict[str, Any]:
    """Convert an extraction envelope using shared schema-2 source parsers.

    This helper does not normalize, save or establish trust. Missing shared v2
    code fails explicitly; there is no schema-1 fallback. Images stay outside
    the culinary candidate until the separate managed-asset boundary accepts
    them. Input is an internal result of one of the read_* functions above.
    """
    try:
        from recipes import source_ingredient, source_ingredient_rows, source_ingredient_sections, source_yield, categories_from_tags
    except ImportError as exc:
        raise RecipeImportReaderError("shared schema-2 source parsers are not installed") from exc
    raw = record["extracted"]
    yield_value, portions = source_yield(raw["yield_text"])
    if portions is None and "; " in raw["yield_text"]:
        # Recipe plugins commonly publish ["4", "4 servings"]; the labelled entry names the servings.
        for label in raw["yield_text"].split("; "):
            labelled, portions = source_yield(label)
            if portions is not None:
                yield_value = {**labelled, "original_text": raw["yield_text"]}
                break
    # Imported pages, recipe exports and store recipe pages reach this helper,
    # never the curated collection build.
    empty_sections = []
    if raw["source"]["kind"] == "recipesage":
        ingredients, empty_sections = source_ingredient_sections(raw["ingredients"], language=raw["language"],
                                                               attached_metric_units=True)
    else:
        ingredients = source_ingredient_rows(raw["ingredients"], language=raw["language"])
    portions_input = raw["yield_text"] or None
    if raw["source"]["kind"] == "mealie":
        # Mealie explicitly stores person servings independently from yield.
        portions = raw["servings"] or None
        portions_input = f"recipeServings: {raw['servings']}"
        ingredients = []
        for text, detail in zip(raw["ingredients"], raw["ingredient_details"], strict=True):
            food, unit, quantity = detail["food"], detail["unit"], detail["quantity"]
            if food and unit and quantity:
                unit_text = (unit["abbreviation"] if unit["useAbbreviation"] else "") or unit["name"]
                measure = f"{quantity} {unit_text}"
                # Mealie stores the quantity as a number, so "1.125" is one and an eighth.
                ingredient = source_ingredient(text, item=food["name"], measure=measure, language=raw["language"],
                                               numeric_measure=True)
                # The structured native values, rather than potentially stale
                # originalText/display, are the actual input to this parse.
                for evidence in ingredient["evidence"].values():
                    evidence["input"] = f"quantity: {quantity}; unit: {unit_text}"
            elif food or unit:
                # An explicitly incomplete structured amount must not revive a
                # stale quantity from originalText or Mealie's display cache.
                ingredient = source_ingredient(text, item=food["name"] if food else text, measure="", language=raw["language"])
            else:
                ingredient = source_ingredient(text, language=raw["language"])
            ingredient["notes"] = detail["note"] or None
            ingredients.append(ingredient)
    notes = "\n\n".join(part for part in (raw["description"], raw["notes"], *empty_sections) if part)
    _text(notes, "combined recipe notes", MAX_TEXT)
    return {
        "schema_version": 2, "name": raw["name"], "language": raw["language"],
        "tags": list(raw["tags"]), "source": deepcopy(raw["source"]),
        "categories": categories_from_tags(raw["tags"]),
        "rights": {"storage": "full", "credit": raw["credit"] or None},
        "ingredients": ingredients,
        "steps": list(raw["steps"]), "yield": yield_value, "portions": portions,
        "portions_evidence": {"basis": "source" if portions is not None else "unknown", "input": portions_input},
        "notes": notes or None, "times": deepcopy(raw["times"]) or None,
    }


_FRACTIONS = "¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"


def _whole_quote(text: str, quote: str) -> bool:
    """True when some occurrence of quote cuts neither a number nor a word.

    "5 dl" also occurs inside "1,5 dl", "500 g" inside "1 500 g", "2 ss" inside
    "1-2 ss" and "4 porsjoner" inside "14 porsjoner"; such a cut would record a
    different amount as the source's own. A letter beside a digit is not a
    cut: pages often glue a list number or heading to the text that follows.
    """
    def number(character: str) -> bool:
        return character.isdigit() or character in _FRACTIONS

    def cut(position: int) -> bool:
        before, after = text[max(0, position - 4):position], text[position:position + 2]
        left, right = before[-1:], after[:1]
        if not left or not right:
            return False
        if (left.isalpha() and right.isalpha()) or (number(left) and number(right)):
            return True
        # One separator between two numbers on either side of the cut: a decimal
        # or fraction mark, a range dash, or the single space of "1 500" and "1 1/2".
        joins = ".,/⁄-–— \u00a0\u2009\u202f"
        if left in joins and number(right) and number(before[-2:-1]):
            return True
        if right in joins and number(left) and number(after[1:2]):
            return True
        # A multiplier: "2 x 400 g" is not 400 g.
        if number(right):
            # OCR and pasted text may retain several horizontal spaces around
            # grouped digits, fraction/range marks or multiplication signs.
            # Do not cross lines or mistake a list marker ("1. 2 ss") for a decimal.
            prefix = text[:position]
            spaces = " \t\u00a0\u2009\u202f"
            trimmed = prefix.rstrip(spaces)
            if trimmed != prefix and trimmed and number(trimmed[-1]):
                return True
            if trimmed[-1:] in {",", "/", "⁄", "-", "–", "—", "x", "×"}:
                previous = trimmed[:-1].rstrip(spaces)
                if previous and number(previous[-1]):
                    return True
        if number(left) and right in " \t\u00a0\u2009\u202f":
            suffix = text[position:].lstrip(" \t\u00a0\u2009\u202f")
            if suffix and number(suffix[0]):
                return True
            if suffix[:1] in {",", "/", "⁄", "-", "–", "—", "x", "×"}:
                following = suffix[1:].lstrip(" \t\u00a0\u2009\u202f")
                if following and number(following[0]):
                    return True
        return False

    start, tried = text.find(quote), 0
    while start != -1 and tried < 256:
        if not cut(start) and not cut(start + len(quote)):
            return True
        # A page that repeats the quote hundreds of times is not searched further.
        start, tried = text.find(quote, start + 1), tried + 1
    return False


def read_transcript(value: Any) -> dict[str, Any]:
    """Build a candidate from quoted supplied text, never from caller evidence.

    Photo/PDF text is transcribed by the host. This validates excerpts against
    that supplied text, not against original image pixels or document bytes.
    No file, network, bank or provider operation occurs here.
    """
    from recipes import (
        SOURCE_OIL_CHOICE,
        bind_recipe_source,
        categories_from_tags,
        normalize_recipe,
        source_ingredient,
        source_ingredient_parts,
        source_ingredient_identity,
        source_yield,
    )
    from recipe_quantities import UNITS, normalized_unit, quantity_json, read_quantity
    if not isinstance(value, dict) or set(value) - {"kind", "pages", "interpretation", "attribution"}:
        raise RecipeImportReaderError("transcript contains unsupported fields")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in {"pasted_text", "photo_transcript", "pdf_transcript"}:
        raise RecipeImportReaderError("transcript kind is unsupported")
    pages = value.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
        raise RecipeImportReaderError("transcript requires one to 20 pages")
    page_text, page_issues, byte_count = {}, {}, 0
    for page in pages:
        if (not isinstance(page, dict) or set(page) - {"page", "text", "issue"}
                or type(page.get("page")) is not int or not 1 <= page["page"] <= 100_000
                or page["page"] in page_text):
            raise RecipeImportReaderError("transcript page identity is invalid or repeated")
        text = _text(page.get("text"), "page text", MAX_WEBPAGE_TEXT_BYTES)
        issue = _text(page.get("issue"), "page issue", 500)
        if not text.strip() and not issue.strip():
            raise RecipeImportReaderError("an unreadable page requires an explicit issue")
        byte_count += len(text.encode("utf-8")) + len(issue.encode("utf-8"))
        if byte_count > MAX_WEBPAGE_TEXT_BYTES:
            raise RecipeImportReaderError("transcript exceeds 64 KiB of source text")
        page_text[page["page"]] = text
        if issue:
            page_issues[page["page"]] = issue
    attribution = value.get("attribution", {})
    if not isinstance(attribution, dict) or set(attribution) - {"url", "publisher", "title", "author"}:
        raise RecipeImportReaderError("transcript attribution contains unsupported fields")
    attribution = {key: _url(attribution.get(key), "attribution URL") if key == "url" else
                   _text(attribution.get(key), "attribution " + key, 200 if key in {"publisher", "author"} else 300) or None
                   for key in ("url", "publisher", "title", "author")}
    interpretation = value.get("interpretation")
    if not isinstance(interpretation, dict) or set(interpretation) - {"name", "language", "ingredients", "steps", "yield", "notes", "tags", "categories"}:
        raise RecipeImportReaderError("transcript requires an allowlisted interpretation")

    def excerpt(item, field, *, maximum=500, extra=()):
        if (not isinstance(item, dict) or set(item) - {"page", "quote", *extra}
                or type(item.get("page")) is not int or item["page"] not in page_text):
            raise RecipeImportReaderError(f"{field} requires an exact transcript page and quote")
        quote = _text(item.get("quote"), field + " quote", maximum, required=True)
        # Canonically equivalent letters and invisible line-wrap characters
        # do not change the quote. Retain the supplied pages for source hashing;
        # only the comparison and numeric-boundary check use this readable form.
        def comparable(text):
            return unicodedata.normalize("NFC", text.replace("\u00ad", "").replace("\u200b", ""))
        readable_page, readable_quote = comparable(page_text[item["page"]]), comparable(quote)
        if not readable_quote or readable_quote not in readable_page:
            raise RecipeImportReaderError(f"{field} quote is absent from its supplied page")
        if not _whole_quote(readable_page, readable_quote):
            raise RecipeImportReaderError(f"{field} quote starts or ends inside a number or word on its supplied page")
        return quote, f"Page {item['page']}: {quote}"

    def selected(field, maximum, *, optional=False):
        items = interpretation.get(field, [] if optional else None)
        if not isinstance(items, list) or not (0 if optional else 1) <= len(items) <= maximum:
            raise RecipeImportReaderError(f"{field} selection is missing or oversized")
        return items

    def estimate(value, *, unit):
        fields = {"quantity", "assumptions", *(('unit',) if unit else ())}
        if not isinstance(value, dict) or set(value) != fields:
            raise RecipeImportReaderError("estimate requires quantity and explicit assumptions")
        assumptions = _text(value["assumptions"], "estimate assumptions", 1000, required=True)
        try:
            quantity = quantity_json(read_quantity(value["quantity"]))
        except ValueError as exc:
            raise RecipeImportReaderError("estimate quantity is invalid") from exc
        normalized = normalized_unit(value["unit"]) if unit else None
        if unit and normalized not in UNITS:
            raise RecipeImportReaderError("estimate unit is unsupported")
        return quantity, normalized, assumptions

    language = _text(interpretation.get("language"), "language", 20) or "und"
    ingredients = []
    ingredient_excerpts = selected("ingredients", 200)
    temporal = (r"(?:yesterday(?:'s)?|earlier(?:\s+(?:this|in\s+the))?\s+(?:meal|menu|week)|"
                r"previous\s+(?:meal|menu)|last\s+(?:meal|menu)|leftovers?|i\s+går|gårsdagens|"
                r"tidligere(?:\s+(?:denne|i))?\s+(?:måltid|meny|uke)|forrige\s+(?:måltid|meny|uke)|rester(?:ne)?)")
    dependency_patterns = (
        re.compile(rf"\b(?:from|fra)\b.{{0,50}}\b{temporal}\b", re.IGNORECASE),
        re.compile(rf"\b(?:use|add|fold|stir|mix|bruk|tilsett|vend|rør|bland)\b.{{0,80}}\b{temporal}\b", re.IGNORECASE),
        re.compile(rf"\b{temporal}\b.{{0,50}}\b(?:soup|hummus|sauce|stew|suppe|saus|gryte|mos|deig)\b", re.IGNORECASE),
    )
    def has_dependency(text):
        return any(pattern.search(text) for pattern in dependency_patterns)
    for index, item in enumerate(ingredient_excerpts, 1):
        quote, evidence_input = excerpt(item, "ingredient", extra=("estimated_amount", "item", "choice", "whole_count"))
        original_quote = quote
        oil_choice = SOURCE_OIL_CHOICE.fullmatch(quote)
        page = page_text[item["page"]]
        if not oil_choice or not oil_choice[3]:
            # A fragment such as "oil" or "1 tbsp" can be real page text while
            # losing the complete identity and its frying allowance. Accept it
            # only if the same text is also an independent source line.
            folded_quote = quote.casefold()
            for match in SOURCE_OIL_CHOICE.finditer(page):
                if folded_quote in match[0].casefold() and folded_quote != match[0].casefold():
                    other_lines = SOURCE_OIL_CHOICE.sub("\ufffc", page).splitlines()
                    if not any(line.strip() == quote for line in other_lines):
                        raise RecipeImportReaderError("quote the complete oil alternatives and any extra for frying")
                    break
        if "choice" in item and oil_choice:
            if item["choice"] not in ("sunflower oil", "vegetable oil") or "item" in item or "whole_count" in item:
                raise RecipeImportReaderError("choose the complete oil identity: sunflower oil or vegetable oil")
            if oil_choice[3]:
                if "estimated_amount" not in item:
                    raise RecipeImportReaderError("estimate the total oil including the extra for frying")
                quantity, oil_unit, _ = estimate(item["estimated_amount"], unit=True)
                dimension, multiplier = UNITS[oil_unit]
                if dimension != "ml" or read_quantity(quantity) * multiplier <= int(oil_choice[1]) * UNITS["tbsp"][1]:
                    raise RecipeImportReaderError("estimate a total volume above the stated amount to include the frying oil")
            elif "estimated_amount" in item:
                raise RecipeImportReaderError("the selected oil already has an explicit measure")
            quote = f"{oil_choice[1]} tbsp {item['choice']}"
        elif "choice" in item:
            # Only a single shared explicit measure and two complete named
            # alternatives establish the amount for either selected ingredient.
            parts = re.fullmatch(r"(\d+(?:[.,/]\d+)?\s+[^\W\d_]+)\s+([^\W\d_]+)\s+(?:or|eller)\s+([^\W\d_]+)",
                                 quote, re.IGNORECASE)
            if parts and tuple(part.casefold() for part in parts.groups()[1:]) == ("sunflower", "vegetable"):
                raise RecipeImportReaderError("quote the complete oil alternatives and any extra for frying")
            if (not parts or item["choice"] not in parts.groups()[1:]
                    or "estimated_amount" in item or "item" in item or "whole_count" in item):
                raise RecipeImportReaderError("choose one complete source alternative with the same stated measure")
            quote = f"{parts[1]} {item['choice']}"
        if "whole_count" in item:
            pieces = re.fullmatch(r"([1-9]\d{0,2})\s+((?:large )?eggs?|løk|onion|onions|gulrot|gulrøtter|carrot|carrots)",
                                  quote, re.IGNORECASE)
            if (not pieces or item["whole_count"] is not True or "estimated_amount" in item
                    or "item" in item or "choice" in item):
                raise RecipeImportReaderError("whole_count requires an explicit count of a recognized whole ingredient")
            quote = f"{pieces[1]} stk {pieces[2]}"
        if (re.search(r"\b(?:or|eller)\b", quote, re.IGNORECASE)
                or re.search(r"(?<=[^\W\d_])\s*[/|]\s*(?=[^\W\d_])", quote, re.UNICODE)):
            raise RecipeImportReaderError("ingredient alternatives require one source-supported choice")
        if has_dependency(quote):
            raise RecipeImportReaderError("cross-meal recipe dependencies require an explicit standalone adaptation")
        if set(item) == {"page", "quote"}:
            capacity = 200 - len(ingredients) - (len(ingredient_excerpts) - index)
            parts = source_ingredient_parts(quote, language=language, max_rows=capacity)
            for ingredient in parts:
                for evidence in ingredient["evidence"].values():
                    evidence["input"] = evidence_input
            ingredients.extend(parts)
            continue
        estimated_item = None
        if "item" in item:
            # The quote stays the evidence; the host names what its estimate measures
            # ("1 can chopped tomatoes" as 400 g of "chopped tomatoes").
            if "estimated_amount" not in item:
                raise RecipeImportReaderError("an ingredient item name applies only with estimated_amount")
            estimated_item = _text(item["item"], "ingredient item", 300, required=True)
            # The name is held to the same rules as the quoted line.
            if (re.search(r"\b(?:or|eller)\b", estimated_item, re.IGNORECASE)
                    or re.search(r"(?<=[^\W\d_])\s*[/|]\s*(?=[^\W\d_])", estimated_item, re.UNICODE)):
                raise RecipeImportReaderError("ingredient alternatives require one source-supported choice")
            if has_dependency(estimated_item):
                raise RecipeImportReaderError("cross-meal recipe dependencies require an explicit standalone adaptation")
        elif "estimated_amount" in item and not oil_choice:
            without_leading_amount = re.sub(
                r"^\s*\d+(?:[.,]\d+)?\s+", "", quote, count=1,
            ).strip()
            if without_leading_amount != quote.strip():
                estimated_item = without_leading_amount
        ingredient = source_ingredient(quote, item=estimated_item, language=language, attached_metric_units=True)
        if quote != original_quote:
            if ingredient["quantity"] is None or ingredient["unit"] is None:
                raise RecipeImportReaderError("selected ingredient needs a source-supported measure")
            ingredient["original_text"] = ingredient["raw"] = original_quote
            if "choice" in item:
                ingredient["notes"] = f"Selected from source alternatives: {item['choice']}"
        for evidence in ingredient["evidence"].values():
            evidence["input"] = evidence_input
        if "estimated_amount" in item:
            quantity, unit, assumptions = estimate(item["estimated_amount"], unit=True)
            _, identity_review_required = source_ingredient_identity(
                ingredient["item"], language, require_reviewed=False,
            )
            ingredient.update(
                quantity=quantity,
                unit=unit,
                scalable=not identity_review_required,
            )
            ingredient["evidence"] = {field: {"basis": "estimate", "input": evidence_input,
                "assumptions": assumptions} for field in ("quantity", "unit")}
        ingredients.append(ingredient)
    step_excerpts = selected("steps", 100)
    steps = [excerpt(item, "step", maximum=MAX_TEXT)[0] for item in step_excerpts]
    reserved_marinade = False
    for step in steps:
        checked = step
        if reserved_marinade:
            checked = re.sub(r"\b(?:the leftover marinade|rest(?:en|ene) av marinaden)\b",
                             "marinade", checked, flags=re.IGNORECASE)
        if has_dependency(checked):
            raise RecipeImportReaderError("cross-meal recipe dependencies require an explicit standalone adaptation")
        if re.fullmatch(r"\s*(?:reserve|set aside|sett av|hold av)\s+(?:(?:some|the|half|litt|halve|av)\s+){0,4}marinad(?:e|en)[.!]?\s*", step, re.IGNORECASE):
            reserved_marinade = True
    note_excerpts = selected("notes", 100, optional=True)
    notes = "\n\n".join(excerpt(item, "note", maximum=MAX_TEXT)[0] for item in note_excerpts)
    notes = _text(notes, "combined transcript notes", MAX_TEXT) or None
    yield_value, portions, portions_evidence = None, None, {"basis": "unknown"}
    if interpretation.get("yield") is not None:
        item = interpretation["yield"]
        quote, evidence_input = excerpt(item, "yield", extra=("estimated_portions",))
        yield_value, portions = source_yield(quote)
        for evidence in yield_value["evidence"].values():
            evidence["input"] = evidence_input
        portions_evidence = {"basis": "source" if portions is not None else "unknown", "input": evidence_input}
        if "estimated_portions" in item:
            quantity, _, assumptions = estimate(item["estimated_portions"], unit=False)
            portions = float(read_quantity(quantity))
            portions_evidence = {"basis": "estimate", "input": evidence_input, "assumptions": assumptions}
    tags = selected("tags", 50, optional=True)
    tags = [_text(tag, "tag", 80, required=True) for tag in tags]
    name = _label(interpretation.get("name"), "name", 300, required=True)
    categories = interpretation.get("categories")
    if categories is not None:
        if not isinstance(categories, list):
            raise RecipeImportReaderError("categories must be a list of host classifications")
        categories = [_text(category, "category", 80, required=True) for category in categories]
    else:
        categories = categories_from_tags(tags)
    source_pages = [{"page": page, "text": text, "issue": page_issues.get(page)} for page, text in sorted(page_text.items())]
    source_digest = hashlib.sha256(json.dumps({"kind": kind, "pages": source_pages, "attribution": attribution},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    candidate = normalize_recipe(bind_recipe_source({
        "schema_version": 2, "name": name,
        "language": language,
        "source": {"kind": kind, "external_id": "sha256:" + source_digest,
                   "relationship": "user_supplied", "original": attribution},
        "rights": {"storage": "full"}, "ingredients": ingredients, "steps": steps,
        "yield": yield_value, "portions": portions, "portions_evidence": portions_evidence,
        "tags": tags, "notes": notes,
        "categories": categories,
    }))
    metadata_basis = {"name": "host_interpretation", "language": "host_inference" if language != "und" else "unknown",
                      "tags": "host_classification" if tags else "none",
                      "categories": "host_classification" if categories else "none"}
    return {"candidate": candidate, "source_context": {"kind": kind, "content_sha256": source_digest,
        "source_mode": "supplied_text" if kind == "pasted_text" else "host_transcript",
        "attribution_status": "declared" if any(attribution.values()) else "unknown",
        "metadata_basis": metadata_basis},
        "page_issues": [{"page": page, "issue": issue} for page, issue in sorted(page_issues.items())],
        "source_excerpts": {"ingredients": [{"page": item["page"], "quote": item["quote"]} for item in ingredient_excerpts],
                            "steps": deepcopy(step_excerpts), "notes": deepcopy(note_excerpts)},
        "image_status": "none", "personal_entry_created": False}


# Mealie 3.24.0 Recipe/RecipeIngredient models and export writers:
# https://github.com/mealie-recipes/mealie/blob/v3.24.0/mealie/schema/recipe/recipe.py
# https://github.com/mealie-recipes/mealie/blob/v3.24.0/mealie/schema/recipe/recipe_ingredient.py
# https://github.com/mealie-recipes/mealie/blob/v3.24.0/mealie/services/exporter/_abc_exporter.py
# https://github.com/mealie-recipes/mealie/blob/v3.24.0/mealie/routes/recipe/shared_routes.py
# model_dump_json() uses snake_case; API serialization uses model aliases.
_MEALIE_ALIASES = {
    "recipe_ingredient": "recipeIngredient", "recipe_instructions": "recipeInstructions",
    "recipe_servings": "recipeServings", "recipe_yield": "recipeYield",
    "recipe_yield_quantity": "recipeYieldQuantity", "recipe_category": "recipeCategory",
    "org_url": "orgURL", "original_text": "originalText", "total_time": "totalTime",
    "prep_time": "prepTime", "cook_time": "cookTime", "perform_time": "performTime",
    "plural_name": "pluralName", "plural_abbreviation": "pluralAbbreviation",
    "use_abbreviation": "useAbbreviation",
}
_MEALIE_FIELDS = {
    "id", "slug", "name", "description", "recipeIngredient", "recipeInstructions",
    "recipeServings", "recipeYield", "recipeYieldQuantity", "notes", "tags",
    "recipeCategory", "orgURL", "totalTime", "prepTime", "cookTime", "performTime",
}


def _mealie_names(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecipeImportReaderError(f"Mealie {field} must be an object")
    result = {}
    for key, child in value.items():
        key = _MEALIE_ALIASES.get(key, key)
        if key in result:
            raise RecipeImportReaderError("Mealie source contains duplicate field aliases")
        result[key] = child
    return result


def _mealie_number(value: Any, field: str) -> int | float:
    if value is None:
        return 0
    if type(value) not in {int, float} or not 0 <= value <= 10**12 or not math.isfinite(value):
        raise RecipeImportReaderError(f"Mealie {field} must be a bounded nonnegative number")
    return value


def _mealie_list(value: Any, field: str, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise RecipeImportReaderError(f"Mealie {field} must be a bounded list")
    return value


def _mealie_food_unit(value: Any, field: str, unsupported: set[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = _mealie_names(value, field)
    fields = {"name", "pluralName"}
    if field == "unit":
        fields |= {"abbreviation", "pluralAbbreviation", "useAbbreviation"}
    unsupported.update(f"recipeIngredient.{field}.{key}" for key in set(raw) - fields)
    result = {key: _text(raw.get(key), f"Mealie {field}.{key}", 200, required=key == "name")
              for key in fields - {"useAbbreviation"}}
    if field == "unit":
        use_abbreviation = raw.get("useAbbreviation", False)
        if type(use_abbreviation) is not bool:
            raise RecipeImportReaderError("Mealie unit.useAbbreviation must be boolean")
        result["useAbbreviation"] = use_abbreviation
    return result


def read_mealie_json(data: bytes | str) -> dict[str, Any]:
    """Read one native Mealie 3.24.0 Recipe, not a backup or arbitrary API page.

    Only culinary source fields are retained. Recipe/account settings, source
    sidecars and private account identifiers are reported by field name only.
    The native image field is a cache key, not an image URL or asset authority.
    """
    raw = _mealie_names(_json(_input_text(data, MAX_RECORD_BYTES)), "recipe")
    unsupported = set(raw) - _MEALIE_FIELDS
    ingredients, details = [], []
    for value in _mealie_list(raw.get("recipeIngredient"), "recipeIngredient", 200):
        item = _mealie_names(value, "ingredient")
        fields = {"quantity", "unit", "food", "originalText", "display", "note", "title"}
        unsupported.update(f"recipeIngredient.{key}" for key in set(item) - fields)
        detail = {
            "quantity": _mealie_number(item.get("quantity"), "ingredient quantity"),
            "unit": _mealie_food_unit(item.get("unit"), "unit", unsupported),
            "food": _mealie_food_unit(item.get("food"), "food", unsupported),
            "original_text": _text(item.get("originalText"), "Mealie originalText", 500),
            "display": _text(item.get("display"), "Mealie display", 500),
            "note": _text(item.get("note"), "Mealie ingredient note", 500),
            "title": _text(item.get("title"), "Mealie ingredient title", 500),
        }
        text = detail["original_text"] or detail["display"]
        if not text:
            text = " ".join(str(value) for value in (
                detail["quantity"] or "", (detail["unit"] or {}).get("name", ""),
                (detail["food"] or {}).get("name", ""), detail["note"],
            ) if value)
        if not text and not detail["title"]:
            unsupported.add("recipeIngredient.empty")
            continue
        ingredients.append(_text(text, "Mealie ingredient text", 500, required=True))
        details.append(detail)
        if detail["title"]:
            unsupported.add("recipeIngredient.title")
    steps = []
    for value in _mealie_list(raw.get("recipeInstructions"), "recipeInstructions", 100):
        step = _mealie_names(value, "instruction")
        unsupported.update(f"recipeInstructions.{key}" for key in set(step) - {"title", "summary", "text"})
        title = _text(step.get("title"), "Mealie step title", 500)
        summary = _text(step.get("summary"), "Mealie step summary", MAX_TEXT)
        body = _text(step.get("text"), "Mealie step text", MAX_TEXT, required=True)
        steps.append(_text("\n".join(part for part in (f"[{title}]" if title else "", summary, body) if part), "Mealie step", MAX_TEXT))
    if not ingredients or not steps:
        raise RecipeImportReaderError("Mealie recipe must contain ingredients and instructions")
    notes = []
    for value in _mealie_list(raw.get("notes"), "notes", 100):
        note = _mealie_names(value, "note")
        unsupported.update(f"notes.{key}" for key in set(note) - {"title", "text"})
        title = _text(note.get("title"), "Mealie note title", 500)
        body = _text(note.get("text"), "Mealie note text", MAX_TEXT)
        notes.append("\n".join(part for part in (f"[{title}]" if title else "", body) if part))
    tags = []
    for field in ("tags", "recipeCategory"):
        for value in _mealie_list(raw.get(field), field, 50):
            tag = _mealie_names(value, field)
            unsupported.update(f"{field}.{key}" for key in set(tag) - {"name"})
            name = _label(tag.get("name"), f"Mealie {field} name", 80, required=True)
            if name not in tags:
                tags.append(name)
    if len(tags) > 50:
        raise RecipeImportReaderError("Mealie tags and categories exceed 50 combined values")
    servings = _mealie_number(raw.get("recipeServings"), "recipeServings")
    yield_quantity = _mealie_number(raw.get("recipeYieldQuantity"), "recipeYieldQuantity")
    yield_label = _text(raw.get("recipeYield"), "Mealie recipeYield", 500)
    yield_text = f"{yield_quantity} {yield_label}".strip() if yield_quantity else yield_label
    yield_text = _text(yield_text, "Mealie complete yield", 500)
    name = _label(raw.get("name"), "Mealie name", 300, required=True)
    if isinstance(raw.get("orgURL"), str):
        try:
            raw["orgURL"].encode("utf-8")
        except UnicodeError as exc:
            raise RecipeImportReaderError("Mealie orgURL contains invalid Unicode") from exc
    try:
        original_url = _url(raw.get("orgURL"), "orgURL")
    except RecipeImportReaderError:
        if isinstance(raw.get("orgURL"), str) and raw["orgURL"].strip().casefold().startswith(("http://", "https://")):
            raise  # Preserve the native import's credential-bearing URL rejection.
        original_url = None
        unsupported.add("orgURL")  # Optional provenance text is not a usable URL.
    return {
        "extracted": {
            "name": name, "language": "und", "ingredients": ingredients,
            "ingredient_details": details, "steps": steps, "yield_text": yield_text,
            "yield_quantity": yield_quantity, "yield_label": yield_label, "servings": servings,
            "description": _text(raw.get("description"), "Mealie description", MAX_TEXT),
            "notes": _text("\n\n".join(notes), "Mealie notes", MAX_TEXT),
            "tags": tags, "credit": "", "slug": _text(raw.get("slug"), "Mealie slug", 250),
            "times": {key: _text(raw[field], f"Mealie {field}", 1000) for key, field in
                      (("prep", "prepTime"), ("cook", "cookTime"), ("total", "totalTime"), ("perform", "performTime")) if raw.get(field)},
            "source": {"kind": "mealie", "publisher": "Mealie", "title": name,
                       "author": None, "url": None,
                       "external_id": _text(raw.get("id"), "Mealie recipe id", 300) or None,
                       "relationship": "user_supplied", "original": {"url": original_url}},
        },
        "image_candidates": [], "image_status": "requires_export_cover" if raw.get("image") else "none",
        "unsupported_fields": sorted(unsupported),
    }


class MealieExportArchive:
    """Opened native export inventory; cover reads never leave this ZIP.

    Supported layouts are single <slug>.json plus original.webp, and multiple
    recipes/<slug>/<slug>.json plus recipes/<slug>/images/original.webp.
    Other regular files within a known recipe subtree are counted as unsupported
    attachments and are never read. These layouts come from the pinned exporter,
    shared_routes.py and RecipeImageTypes enum, not guessed API fixtures.
    """

    def __init__(self, archive: zipfile.ZipFile):
        from recipe_portable import MAX_EXPANDED_BYTES, _reject_zip64_extra
        self._archive = archive
        self._entries = {}
        self._covers: set[str] = set()
        self._recipes: list[tuple[str, str, str]] = []
        expanded = 0
        for info in archive.infolist():
            name = info.filename
            parts = name.split("/")
            if (name != info.orig_filename or name in self._entries or len(name) > 1024
                    or "\\" in name or any(part in {"", ".", ".."} for part in parts)
                    or any(ord(char) < 32 for char in name) or ":" in name
                    or info.is_dir() or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}
                    or info.flag_bits & 1 or info.extract_version >= 45
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                raise RecipeImportReaderError("Mealie ZIP contains an unsafe or unsupported member")
            _reject_zip64_extra(info.extra)
            recipe_member = name.endswith(".json") and (
                len(parts) == 1 or (len(parts) == 3 and parts[0] == "recipes" and parts[2] == f"{parts[1]}.json")
            )
            maximum = MAX_RECORD_BYTES if recipe_member else MAX_MEALIE_COVER_BYTES
            if not 0 <= info.file_size <= maximum:
                raise RecipeImportReaderError("Mealie ZIP member is too large")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise RecipeImportReaderError("Mealie ZIP expansion is too large")
            self._entries[name] = info
            if recipe_member:
                if len(parts) == 1:
                    self._recipes.append((name, name[:-5], "original.webp"))
                elif len(parts) == 3 and parts[0] == "recipes" and parts[2] == f"{parts[1]}.json":
                    self._recipes.append((name, parts[1], f"recipes/{parts[1]}/images/original.webp"))
        if not self._recipes or len(self._recipes) > MAX_RECORDS:
            raise RecipeImportReaderError("Mealie ZIP has no supported recipes or too many recipes")
        single = any("/" not in path for path, _, _ in self._recipes)
        if single and (len(self._recipes) != 1 or set(self._entries) - {self._recipes[0][0], "original.webp"}):
            raise RecipeImportReaderError("Mealie single-recipe ZIP layout is ambiguous")
        slugs = {slug for _, slug, _ in self._recipes}
        if not single and any(len(parts := name.split("/", 2)) != 3 or parts[0] != "recipes" or parts[1] not in slugs for name in self._entries):
            raise RecipeImportReaderError("Mealie ZIP contains an unassociated member")
        self._covers = {cover for _, _, cover in self._recipes if cover in self._entries}
        self.unsupported_member_count = len(self._entries) - len(self._recipes) - len(self._covers)

    def _read(self, name: str, maximum: int) -> bytes:
        from recipe_portable import _check_local_header
        info = self._entries[name]
        try:
            _check_local_header(self._archive, info)
            with self._archive.open(info) as handle:
                raw = handle.read(maximum + 1)
            if len(raw) != info.file_size or len(raw) > maximum:
                raise RecipeImportReaderError("Mealie ZIP member size differs or is oversized")
            return raw
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise RecipeImportReaderError("Mealie ZIP member is unreadable or corrupt") from exc

    def records(self):
        """Yield extraction envelopes, with inert image_member_candidates."""
        for path, slug, cover in self._recipes:
            record = read_mealie_json(self._read(path, MAX_RECORD_BYTES))
            if record["extracted"]["slug"] != slug:
                raise RecipeImportReaderError("Mealie ZIP path and recipe slug differ")
            record["image_member_candidates"] = [{"member": cover, "bytes": self._entries[cover].file_size}] if cover in self._covers else []
            if cover in self._covers:
                record["image_status"] = "requires_asset_import"
            yield record

    def read_cover(self, member: str) -> bytes:
        """Read a declared cover only; the asset importer must decode/sanitize it."""
        if member not in self._covers:
            raise RecipeImportReaderError("Mealie ZIP member is not a declared cover")
        return self._read(member, MAX_MEALIE_COVER_BYTES)


@contextmanager
def open_mealie_export_zip(path: Path):
    """Open a staged regular ZIP with the shared bounded directory preflight."""
    from recipe_portable import _directory_bound, _regular_file
    from recipes import RecipeError
    try:
        with _regular_file(Path(path)) as handle:
            _directory_bound(handle)
            with zipfile.ZipFile(handle) as archive:
                yield MealieExportArchive(archive)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, RecipeError) as exc:
        if isinstance(exc, RecipeImportReaderError):
            raise
        raise RecipeImportReaderError("Mealie export ZIP is invalid or unavailable") from exc
