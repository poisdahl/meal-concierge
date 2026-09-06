"""Read-only recipe import sources; no credentials or source trust in recipes.

Configured sources and native adapters are supplied by the service's existing
configuration/credential owner. Never construct them from embedded recipe data.
Public webpage reads use unauthenticated HTTPS and pin a validated DNS result
while verifying TLS against the original hostname. No proxies or redirects.

Native API projections target Mealie 3.24.0 and RecipeSage 4.0.6. RecipeSage's
projection follows packages/util/server/src/general/jsonLD.ts:recipeToJSONLD
at https://github.com/julianpoy/recipesage/tree/v4.0.6. Extraction and quantity
interpretation remain in recipe_import_readers and the shared source parsers.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.parse import quote, urlencode, urlsplit

from recipe_import_readers import (
    MAX_EXPORT_BYTES, MAX_RECORD_BYTES, MAX_RECORDS, MAX_WEBPAGE_BYTES, MAX_MEALIE_COVER_BYTES,
    RecipeImportReaderError, _extract, _input_text, _json, _text, _url,
    read_mealie_json, read_recipesage_export, read_webpage, source_candidate,
)
from recipe_libraries import (
    normalize_library_origin, require_authenticated_origin,
    validate_library_recipe_ref,
)


MAX_PAGES = 1000
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGE_SIZE = 50
TIMEOUT = 10.0
MAX_IMPORT_SECONDS = 600
_FIELD_NAMES = {
    "id": "identifier", "name": "name", "ingredients": "recipeIngredient",
    "steps": "recipeInstructions", "yield": "recipeYield", "tags": "recipeCategory",
    "source_url": "isBasedOn", "credit": "creditText", "image": "image",
    "description": "description", "language": "inLanguage", "prep_time": "prepTime",
    "cook_time": "cookTime", "total_time": "totalTime",
}


class RecipeImportSourceError(ValueError):
    """Source retrieval is incomplete, incompatible or outside its boundary."""


def _wire_json(value: Any) -> bytes:
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise RecipeImportSourceError("native source recipe JSON is invalid") from exc
    if len(result) > MAX_RECORD_BYTES:
        raise RecipeImportSourceError("native source recipe exceeds the import record limit")
    return result


def _source_result(record: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {**record, "candidate": source_candidate(record), "source_context": context}


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _public_address(str(ip.ipv4_mapped))
        # Do not permit translation/tunnelling prefixes to hide private IPv4.
        if ip.sixtofour is not None or ip.teredo is not None or ip in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


class _PinnedConnection(http.client.HTTPConnection):
    """One request; socket.connect receives only prevalidated numeric addresses."""

    def __init__(self, host: str, port: int, *, tls: bool, public_only: bool):
        super().__init__(host, port, timeout=TIMEOUT)
        self._tls = tls
        self._public_only = public_only
        self._deadline = time.monotonic() + TIMEOUT
        self._transport: socket.socket | None = None

    def abort(self) -> None:
        """Interrupt a peer slowly dripping response headers on this socket."""
        if self._transport is not None:
            try:
                self._transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()

    def connect(self) -> None:
        try:
            addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
            if not addresses or len(addresses) > 32:
                raise RecipeImportSourceError("source DNS result is empty or oversized")
            for family, socktype, _protocol, _name, address in addresses:
                if family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM:
                    raise RecipeImportSourceError("source DNS result is unsupported")
                if self._public_only and not _public_address(address[0]):
                    raise RecipeImportSourceError("public recipe URLs cannot resolve to nonpublic addresses")
            deadline = self._deadline
            for family, socktype, protocol, _name, address in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                transport = socket.socket(family, socktype, protocol)
                self._transport = transport
                self.sock = transport
                try:
                    transport.settimeout(remaining)
                    transport.connect(address)
                    if self._tls:
                        # PROTOCOL_TLS_CLIENT enables chain/hostname checking;
                        # unlike create_default_context this does not enable an
                        # ambient SSLKEYLOGFILE or create a secret-bearing log.
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                        context.load_default_certs()
                        transport = context.wrap_socket(transport, server_hostname=self.host)
                        self._transport = transport
                    self.sock = transport
                    return
                except (OSError, ssl.SSLError):
                    transport.close()
            raise RecipeImportSourceError("source connection or TLS verification failed")
        except (OSError, ValueError) as exc:
            if isinstance(exc, RecipeImportSourceError):
                raise
            raise RecipeImportSourceError("source DNS or connection is unavailable") from None


def _get_bytes(url: str, *, maximum: int, configured_origin: str | None = None,
               authorization: str | None = None,
               accept: str = "application/json, text/html, application/xhtml+xml") -> tuple[bytes, str]:
    """Fixed-origin GET. Only an independently configured origin permits private IPs."""
    checked = _url(url, "source URL")
    if not checked:
        raise RecipeImportSourceError("source URL is required")
    parsed = urlsplit(checked)
    if configured_origin is None:
        if parsed.scheme != "https" or authorization is not None:
            raise RecipeImportSourceError("public source retrieval requires unauthenticated HTTPS")
    else:
        require_authenticated_origin(configured_origin, checked)
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = _PinnedConnection(host, port, tls=parsed.scheme == "https", public_only=configured_origin is None)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        path += "?" + quote(parsed.query, safe="=&?/%:@!$'()*+,;~-._")
    headers = {"Accept": accept, "Accept-Encoding": "identity", "User-Agent": "meal-concierge/source-import-v1"}
    if authorization is not None:
        headers["Authorization"] = authorization
    timer = threading.Timer(TIMEOUT, connection.abort)
    timer.daemon = True
    timer.start()
    response = None
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise RecipeImportSourceError("source redirects are forbidden")
        if response.status != 200:
            raise RecipeImportSourceError(f"source GET failed with HTTP {response.status}")
        if response.headers.get("Content-Encoding", "identity").casefold() != "identity":
            raise RecipeImportSourceError("compressed source responses are unsupported")
        lengths = response.headers.get_all("Content-Length", [])
        if lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 12 or int(lengths[0]) > maximum):
            raise RecipeImportSourceError("source response size is invalid or too large")
        chunks, size = [], 0
        deadline = connection._deadline
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecipeImportSourceError("source response timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(64 * 1024, maximum - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise RecipeImportSourceError("source response is too large")
            chunks.append(chunk)
        if lengths and size != int(lengths[0]):
            raise RecipeImportSourceError("source response is incomplete")
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset()
        if charset and charset.casefold() not in {"utf-8", "utf8", "us-ascii"}:
            raise RecipeImportSourceError("source response encoding is unsupported")
        return b"".join(chunks), content_type
    except (OSError, http.client.HTTPException, UnicodeError) as exc:
        raise RecipeImportSourceError("source response is unavailable or malformed") from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def fetch_public_webpage(url: str) -> dict[str, Any]:
    """Fetch once: structured recipes or bounded text requiring host interpretation.

    Embedded contexts, images and links stay inert. Malformed JSON-LD is an
    explicit failure, not a reason to silently switch extraction methods.
    """
    raw, content_type = _get_bytes(url, maximum=MAX_WEBPAGE_BYTES)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise RecipeImportSourceError("public recipe source must return HTML")
    result = read_webpage(raw, source_url=url)
    result["recipes"] = [_source_result(record, {"kind": "web", "url": url})
                         for record in result["recipes"]]
    return result


def _selector(value: Any) -> tuple[str | int, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise RecipeImportSourceError("mapped selectors must be lists of at most 16 keys/indexes")
    for part in value:
        if type(part) is int and 0 <= part <= MAX_RECORDS:
            continue
        if not isinstance(part, str) or not 1 <= len(part) <= 128 or any(ord(char) < 32 for char in part):
            raise RecipeImportSourceError("mapped selector contains an invalid key/index")
    return tuple(value)


def _select(value: Any, selector: tuple[str | int, ...]) -> Any:
    for part in selector:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and part < len(value):
            value = value[part]
        else:
            raise RecipeImportSourceError("mapped source field is missing or incompatible")
    return value


def _parameter(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value) is None:
        raise RecipeImportSourceError("pagination/query parameter is invalid")
    return value


def _cursor(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 1000 or any(ord(char) < 32 for char in value):
        raise RecipeImportSourceError("source cursor is invalid")
    return value


class MappedAPISource:
    """User-mapped GET API, instantiated from independently approved configuration.

    Required configuration: base_url, endpoint_path, fields, records_path,
    pagination. fields maps id/name/ingredients/steps and optional _FIELD_NAMES
    or notes to lists of literal object keys/list indexes. Pagination modes
    page/offset require end_condition=empty|short_page; cursor requires an exact
    next_cursor_path whose explicit null terminates. No next-URL interpretation.
    Optional query is static; allow_insecure_http follows existing library rules.
    Credentials, when needed, come from the existing secret loader as {token}.
    """

    def __init__(self, configuration: Mapping[str, Any], *, credential: Mapping[str, Any] | None = None):
        allowed = {"base_url", "endpoint_path", "fields", "records_path", "pagination", "query", "allow_insecure_http"}
        if not isinstance(configuration, Mapping) or set(configuration) - allowed:
            raise RecipeImportSourceError("mapped source configuration is invalid")
        allow_http = configuration.get("allow_insecure_http", False)
        if type(allow_http) is not bool:
            raise RecipeImportSourceError("allow_insecure_http must be boolean")
        self.origin = normalize_library_origin(configuration.get("base_url"), allow_insecure_http=allow_http)
        path = configuration.get("endpoint_path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or any(char in path for char in "?#\\"):
            raise RecipeImportSourceError("mapped source endpoint must be one fixed absolute path")
        self.endpoint = self.origin + path
        _url(self.endpoint, "mapped source endpoint")
        self._authorization = None
        if credential is not None:
            if not isinstance(credential, Mapping) or set(credential) != {"token"}:
                raise RecipeImportSourceError("mapped source credential must contain only token")
            token = credential["token"]
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(ord(char) < 33 or ord(char) == 127 for char in token):
                raise RecipeImportSourceError("mapped source token is invalid")
            self._authorization = "Bearer " + token
        fields = configuration.get("fields")
        if not isinstance(fields, dict) or set(fields) - {*_FIELD_NAMES, "notes"} or not {"id", "name", "ingredients", "steps"} <= set(fields):
            raise RecipeImportSourceError("mapped source requires id, name, ingredients and steps selectors")
        self.fields = {field: _selector(selector) for field, selector in fields.items()}
        self.records_path = _selector(configuration.get("records_path"))
        self.query = configuration.get("query", {})
        if not isinstance(self.query, dict) or len(self.query) > 32:
            raise RecipeImportSourceError("mapped source query is invalid")
        self.query = {_parameter(key): _text(value, "query value", 1000) for key, value in self.query.items()}
        pagination = configuration.get("pagination")
        if not isinstance(pagination, dict) or set(pagination) - {"mode", "parameter", "page_size_parameter", "page_size", "start", "end_condition", "next_cursor_path"}:
            raise RecipeImportSourceError("mapped source pagination is invalid")
        self.mode = pagination.get("mode")
        if self.mode not in {"page", "offset", "cursor"}:
            raise RecipeImportSourceError("mapped source pagination mode is invalid")
        self.parameter = _parameter(pagination.get("parameter"))
        self.size_parameter = _parameter(pagination.get("page_size_parameter"))
        if self.parameter == self.size_parameter or self.parameter in self.query or self.size_parameter in self.query:
            raise RecipeImportSourceError("mapped pagination parameters overlap")
        self.page_size = pagination.get("page_size", MAX_PAGE_SIZE)
        if type(self.page_size) is not int or not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise RecipeImportSourceError("mapped source page size is invalid")
        self.start = pagination.get("start", 1 if self.mode == "page" else 0 if self.mode == "offset" else None)
        if self.mode == "cursor":
            self.start = _cursor(self.start)
            self.next_cursor_path = _selector(pagination.get("next_cursor_path"))
            if "end_condition" in pagination:
                raise RecipeImportSourceError("cursor pagination terminates only on explicit null")
            self.end_condition = None
        else:
            if type(self.start) is not int or not 0 <= self.start <= 10**9:
                raise RecipeImportSourceError("mapped pagination start is invalid")
            self.end_condition = pagination.get("end_condition")
            if self.end_condition not in {"empty", "short_page"} or "next_cursor_path" in pagination:
                raise RecipeImportSourceError("numeric pagination requires an explicit termination condition")
        # Reject secret-bearing query spellings before any network request.
        _url(self.endpoint + "?" + urlencode({**self.query, self.parameter: "0", self.size_parameter: self.page_size}), "mapped source query")

    def records(self) -> Iterator[dict[str, Any]]:
        state, total_bytes, count = self.start, 0, 0
        deadline = time.monotonic() + MAX_IMPORT_SECONDS
        seen_states, seen_ids = set(), set()
        for _page in range(MAX_PAGES):
            if time.monotonic() >= deadline:
                raise RecipeImportSourceError("mapped import time limit reached; resume from the last page state")
            if state in seen_states:
                raise RecipeImportSourceError("mapped source pagination repeats a cursor")
            seen_states.add(state)
            query = {**self.query, self.size_parameter: str(self.page_size)}
            if state is not None:
                query[self.parameter] = str(state)
            raw, content_type = _get_bytes(self.endpoint + "?" + urlencode(query), maximum=MAX_PAGE_BYTES,
                                           configured_origin=self.origin, authorization=self._authorization)
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise RecipeImportSourceError("mapped source must return JSON")
            total_bytes += len(raw)
            if total_bytes > MAX_EXPORT_BYTES:
                raise RecipeImportSourceError("mapped source exceeds total import bytes")
            page = _json(_input_text(raw, MAX_PAGE_BYTES))
            rows = _select(page, self.records_path)
            if not isinstance(rows, list) or len(rows) > self.page_size:
                raise RecipeImportSourceError("mapped source returned an invalid page size")
            for row in rows:
                values = {field: _select(row, path) for field, path in self.fields.items()}
                identifier = values["id"]
                if type(identifier) is int and 0 <= identifier <= 10**15:
                    identifier = str(identifier)
                identifier = _text(identifier, "mapped source id", 300, required=True)
                if identifier in seen_ids:
                    raise RecipeImportSourceError("mapped source repeats a recipe identity")
                seen_ids.add(identifier)
                count += 1
                if count > MAX_RECORDS:
                    raise RecipeImportSourceError("mapped source exceeds the recipe limit")
                jsonld = {"@type": "Recipe", **{_FIELD_NAMES[field]: value for field, value in values.items() if field in _FIELD_NAMES}}
                jsonld["identifier"] = identifier
                if "notes" in values:
                    jsonld["comment"] = [{"@type": "Comment", "name": "Author Notes", "text": values["notes"]}]
                record = _extract(jsonld, kind="generic_api", source_url=None)
                yield _source_result(record, {"kind": "generic_api", "configured_origin": self.origin,
                                               "endpoint": self.endpoint, "external_id": identifier, "page_state": state})
            if self.mode == "cursor":
                state = _cursor(_select(page, self.next_cursor_path))
                if state is None:
                    return
            else:
                if not rows or self.end_condition == "short_page" and len(rows) < self.page_size:
                    return
                state += 1 if self.mode == "page" else self.page_size
        raise RecipeImportSourceError("mapped source exceeds the pagination limit")


def _recipesage_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Project actual native fields to the pinned exporter shape; ignore sidecars."""
    from recipe_library_recipesage import METADATA_BEGIN, METADATA_END
    # Existing adapters have already decoded JSON. Re-enter the shared boundary
    # so even ignored field names cannot leak credential-bearing URL text.
    raw = _json(_input_text(_wire_json(raw), MAX_RECORD_BYTES))
    ignored = []
    notes = raw.get("notes") or ""
    if not isinstance(notes, str):
        raise RecipeImportSourceError("RecipeSage native notes must be text")
    if notes.startswith(METADATA_BEGIN):
        delimiter = "\n" + METADATA_END
        end = notes.find(delimiter, len(METADATA_BEGIN))
        if not notes.startswith(METADATA_BEGIN + "\n") or end < 0:
            raise RecipeImportSourceError("RecipeSage metadata framing is invalid")
        tail = notes[end + len(delimiter):]
        if tail and not tail.startswith("\n\n"):
            raise RecipeImportSourceError("RecipeSage metadata framing is invalid")
        notes = tail[2:] if tail else ""
        ignored.append("notes.hermes_metadata")

    def lines(field: str) -> list[str]:
        value = _text(raw.get(field), f"RecipeSage {field}", 100_000, required=True)
        return [line.strip() for line in value.splitlines() if line.strip()]

    labels, images = raw.get("recipeLabels") or [], raw.get("recipeImages") or []
    if not isinstance(labels, list) or not isinstance(images, list) or len(labels) > 50 or len(images) > 100:
        raise RecipeImportSourceError("RecipeSage native labels/images are invalid")
    try:
        categories = [value["label"]["title"] for value in labels]
        image_urls = [value["image"]["location"] for value in images]
    except (TypeError, KeyError) as exc:
        raise RecipeImportSourceError("RecipeSage native labels/images are incompatible") from exc
    instructions = [{"@type": "HowToSection", "name": line[1:-1]} if re.fullmatch(r"\[.*\]", line)
                    else {"@type": "HowToStep", "text": line} for line in lines("instructions")]
    jsonld = {
        "@type": "Recipe", "identifier": raw.get("id"), "name": raw.get("title"),
        "description": raw.get("description"), "recipeIngredient": lines("ingredients"),
        "recipeInstructions": instructions, "recipeYield": raw.get("yield"),
        "prepTime": raw.get("activeTime"), "totalTime": raw.get("totalTime"),
        "recipeCategory": categories, "creditText": raw.get("source"), "isBasedOn": raw.get("url"),
        "image": image_urls, "comment": [{"@type": "Comment", "name": "Author Notes", "text": notes}],
    }
    record = read_recipesage_export(_wire_json({"recipes": [jsonld]}))[0]
    culinary_fields = {"id", "title", "description", "ingredients", "instructions", "yield", "activeTime", "totalTime", "recipeLabels", "recipeImages", "source", "url", "notes"}
    record["unsupported_fields"] = sorted(set(record["unsupported_fields"]) | set(ignored) | (set(raw) - culinary_fields))
    return record


def iter_native_recipes(adapter: Any, *, query: str = "", filters: Mapping[str, Any] | None = None,
                        page_size: int = MAX_PAGE_SIZE, start_cursor: str | None = None) -> Iterator[dict[str, Any]]:
    """Read existing configured adapters only, retaining native culinary fields.

    The existing adapter owns authentication, exact-origin redirect rejection,
    native pagination and RecipeSage account ownership. Its search/get-raw calls
    are reads (RecipeSage uses a documented read-semantic POST for getRecipes).
    No source-side creation, updates, labels, favorites or deletions are called.
    """
    from recipe_library_mealie import MealieAdapter
    from recipe_library_recipesage import RecipeSageAdapter
    if not isinstance(adapter, (MealieAdapter, RecipeSageAdapter)):
        raise RecipeImportSourceError("native source requires an existing Mealie or RecipeSage adapter")
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise RecipeImportSourceError("native import page size is invalid")
    kind = "mealie" if isinstance(adapter, MealieAdapter) else "recipesage"
    cursor, count, total_bytes = _cursor(start_cursor), 0, 0
    deadline = time.monotonic() + MAX_IMPORT_SECONDS
    seen_cursors, seen_ids = set(), set()
    for _page in range(MAX_PAGES):
        if time.monotonic() >= deadline:
            raise RecipeImportSourceError("native import time limit reached; resume from the last page cursor")
        if cursor in seen_cursors:
            raise RecipeImportSourceError("native source pagination repeats a cursor")
        seen_cursors.add(cursor)
        page = adapter.search(query, {} if filters is None else filters, cursor, page_size)
        rows = page.get("recipes")
        if not isinstance(rows, list) or len(rows) > page_size:
            raise RecipeImportSourceError("native source page is incompatible")
        for row in rows:
            if time.monotonic() >= deadline:
                raise RecipeImportSourceError("native import time limit reached; resume from the last page cursor")
            reference = validate_library_recipe_ref(row.get("library_recipe_ref"))
            if reference["library_id"] != adapter.library_id or reference["recipe_id"] in seen_ids:
                raise RecipeImportSourceError("native source repeats or changes recipe identity")
            seen_ids.add(reference["recipe_id"])
            count += 1
            if count > MAX_RECORDS:
                raise RecipeImportSourceError("native source exceeds the recipe limit")
            raw = adapter._get_raw(reference["recipe_id"])
            actual_reference = validate_library_recipe_ref(adapter._reference(raw))
            encoded = _wire_json(raw)
            total_bytes += len(encoded)
            if total_bytes > MAX_EXPORT_BYTES:
                raise RecipeImportSourceError("native source exceeds total import bytes")
            record = read_mealie_json(encoded) if kind == "mealie" else _recipesage_record(raw)
            record["source_annotations"] = {"favorite_status": "unavailable", "label_status": "native_recipe_labels"}
            if type(row.get("is_favorite")) is bool:
                record["source_annotations"]["favorite"] = row["is_favorite"]
                record["source_annotations"]["favorite_status"] = "observed"
            if record["image_status"] != "none":
                record["image_status"] = "native_image_pending"
            yield _source_result(record, {"kind": kind, "configured_origin": adapter.base_url,
                                           "library_id": adapter.library_id, "external_id": reference["recipe_id"],
                                           "library_recipe_ref": actual_reference, "page_cursor": cursor})
        cursor = _cursor(page.get("cursor"))
        if cursor is None:
            return
    raise RecipeImportSourceError("native source exceeds the pagination limit")


def fetch_native_cover(adapter: Any, reference: Mapping[str, str], *, image_url: str | None = None) -> dict[str, Any]:
    """Explicitly fetch a source-owned cover; caller must sanitize with RecipeAssets.

    Mealie's pinned media route is /api/media/recipes/<UUID>/images/original.webp
    (v3.24.0 mealie/routes/media/{__init__,media_recipe}.py); it needs no bearer.
    RecipeSage images are external locations: never forward source credentials,
    allow public HTTPS only, and require a selection when multiple images exist.
    A supplied source version must match the exact recipe re-read before fetching
    bytes. Sources without version metadata cannot offer that version check.
    """
    from recipe_library_mealie import MealieAdapter
    from recipe_library_recipesage import RecipeSageAdapter
    if not isinstance(adapter, (MealieAdapter, RecipeSageAdapter)):
        raise RecipeImportSourceError("cover source requires a configured native adapter")
    checked = validate_library_recipe_ref(reference)
    if checked["library_id"] != adapter.library_id:
        raise RecipeImportSourceError("cover reference names a different source")
    raw = adapter._get_raw(checked["recipe_id"])
    actual_reference = validate_library_recipe_ref(adapter._reference(raw))
    if "version" in checked and checked["version"] != actual_reference.get("version"):
        raise RecipeImportSourceError("native cover recipe version changed or is unavailable")
    if isinstance(adapter, MealieAdapter):
        if image_url is not None:
            raise RecipeImportSourceError("Mealie cover uses its fixed media endpoint")
        if not raw.get("image"):
            raise RecipeImportSourceError("Mealie recipe has no declared cover")
        url = f"{adapter.base_url}/api/media/recipes/{quote(checked['recipe_id'], safe='')}/images/original.webp"
        body, content_type = _get_bytes(url, maximum=MAX_MEALIE_COVER_BYTES, configured_origin=adapter.base_url,
                                       accept="image/jpeg, image/png, image/webp")
    else:
        candidates = _recipesage_record(raw)["image_candidates"]
        if image_url is None and len(candidates) == 1:
            image_url = candidates[0]
        if image_url is None or image_url not in candidates:
            raise RecipeImportSourceError("RecipeSage cover requires an exact native image selection")
        url = image_url
        body, content_type = _get_bytes(url, maximum=MAX_MEALIE_COVER_BYTES,
                                       accept="image/jpeg, image/png, image/webp")
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise RecipeImportSourceError("native cover has unsupported image content type")
    return {"bytes": body, "content_type": content_type, "source_url": url,
            "library_recipe_ref": actual_reference,
            "image_status": "requires_asset_sanitization"}
