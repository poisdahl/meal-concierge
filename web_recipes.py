"""First-party recipe search, explicit alternatives and source scopes; no storage."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import re
from urllib.parse import unquote, urlencode, urljoin, urlsplit


DEFAULT_WEB_SEARCH = {
    "enabled": True,
    "broad": False,
    "sites": [
        {"name": name, "domain": domain, "enabled": True}
        for name, domain in (
            ("MatPrat", "matprat.no"),
            ("Vegetarentusiast", "vegetarentusiast.no"),
            ("Frukt.no", "frukt.no"),
            ("Godfisk", "godfisk.no"),
            ("TINE Kjøkken", "tine.no"),
            ("Godt", "godt.no"),
            ("Trines Matblogg", "trinesmatblogg.no"),
        )
    ],
}


def validate_web_search(value):
    if not isinstance(value, dict) or set(value) != {"enabled", "broad", "sites"}:
        raise ValueError("web_search requires enabled, broad and sites")
    if type(value["enabled"]) is not bool or type(value["broad"]) is not bool:
        raise ValueError("web_search enabled and broad must be boolean")
    if not isinstance(value["sites"], list) or len(value["sites"]) > 32:
        raise ValueError("web_search sites must contain at most 32 sites")
    seen = set()
    for site in value["sites"]:
        if not isinstance(site, dict) or set(site) != {"name", "domain", "enabled"}:
            raise ValueError("web_search sites require name, domain and enabled")
        name, domain = site["name"], site["domain"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or any(ord(c) < 32 for c in name):
            raise ValueError("web_search site name must be short text")
        if (not isinstance(domain, str) or len(domain) > 253
                or re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain) is None):
            raise ValueError("web_search domains must be lowercase DNS names without paths")
        if domain in seen or type(site["enabled"]) is not bool:
            raise ValueError("web_search sites require unique domains and boolean enabled")
        seen.add(domain)
    return deepcopy(value)


def search_digest(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def url_enabled(url, settings):
    """Disabled parent domains also exclude their subdomains in broad search."""
    try:
        parsed = urlsplit(url or "")
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (ValueError, TypeError):
        return False
    if (not settings["enabled"] or parsed.scheme != "https" or parsed.username or parsed.password
            or port not in {None, 443} or not host or "." not in host or not re.search(r"\.[a-z]{2,63}$", host)):
        return False
    matches = [site for site in settings["sites"] if host == site["domain"] or host.endswith("." + site["domain"])]
    if any(not site["enabled"] for site in matches):
        return False
    return any(site["enabled"] for site in matches) or settings["broad"]


def search_plan(settings, query):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise ValueError("web recipe search query must contain 1 to 200 characters")
    settings = validate_web_search(settings)
    excluded = [site["domain"] for site in settings["sites"] if not site["enabled"]]
    scopes = [{"query": query.strip(), "domains": [site["domain"]]}
              for site in settings["sites"] if site["enabled"]] if settings["enabled"] else []
    if settings["enabled"] and settings["broad"]:
        scopes.append({"query": query.strip(), "domains": [], "exclude_domains": excluded})
    return {"status": "host_search_required" if scopes else "disabled", "settings": settings,
            "settings_digest": search_digest(settings), "scopes": scopes,
            "exclude_domains": excluded, "maximum_candidates": 8,
            "searched": False,
            "next": "Use the host's web search with these scopes. Read selected recipe pages and assess storage rights before import. Return exact discovery refs as planner_input.web_candidates. If search is unavailable, report unavailable and continue library/store selection."}


DIRECT_SOURCES = {"matprat.no", "vegetarentusiast.no", "frukt.no", "godfisk.no",
                  "tine.no", "godt.no", "trinesmatblogg.no"}


class _SearchLinks(HTMLParser):
    """Only publisher search-result links, not navigation or arbitrary scripts."""
    def __init__(self, domain):
        super().__init__(convert_charrefs=True)
        self.domain, self.links, self.entry_title = domain, [], False
        self.current, self.text = None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h2":
            self.entry_title = "entry-title" in (attrs.get("class") or "").split()
        if tag == "a":
            href = attrs.get("href", "")
            selected = self.entry_title if self.domain == "trinesmatblogg.no" else "m-card__read-more-btn" in (attrs.get("class") or "").split()
            if selected and href:
                self.current, self.text = href, []

    def handle_data(self, data):
        if self.current is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "h2":
            self.entry_title = False
        if tag == "a" and self.current is not None:
            title = " ".join(" ".join(self.text).split())
            if self.domain == "tine.no":
                title = unquote(urlsplit(self.current).path.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ")
            self.links.append({"url": self.current, "title": title})
            self.current = None


def _records(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("invalid search records")
    return value


def _source_search(domain, query):
    """Fixed first-party routes; responses stay in memory and project to links."""
    from recipe_import_sources import _get_bytes
    origin = "https://" + ("www." if domain not in {"vegetarentusiast.no", "trinesmatblogg.no"} else "") + domain
    body = None
    if domain == "matprat.no":
        url = origin + "/api/ContentSearch/Search?" + urlencode({"text": query, "content": "recipes", "page": 1, "sort": "relevant"})
    elif domain == "vegetarentusiast.no":
        url = origin + "/wp-json/wp/v2/search?" + urlencode({"search": query, "per_page": 8, "_fields": "title,url,type,subtype"})
    elif domain == "frukt.no":
        url = origin + "/api/search?" + urlencode({"q": query, "sort": "relevance", "tab": 1, "recipePage": 1, "ingredientPage": 1, "articlePage": 1, "pageSize": 8})
    elif domain == "godfisk.no":
        url, body = origin + "/sok/_Search", {"q": query, "type": ["Recipe"]}
    elif domain == "godt.no":
        url = origin + "/oppskrifter?" + urlencode({"query": query})
    elif domain == "tine.no":
        url = origin + "/sok?" + urlencode({"q": query})
    elif domain == "trinesmatblogg.no":
        url = origin + "/?" + urlencode({"s": query})
    else:
        raise ValueError("unsupported first-party search source")
    raw, content_type = _get_bytes(url, maximum=2 * 1024 * 1024,
                                  **({"json_body": body} if body is not None else {}))
    if domain in {"godt.no", "tine.no", "trinesmatblogg.no"}:
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("incompatible search response")
        page = raw.decode("utf-8")
        if domain == "godt.no":
            match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
            data = json.loads(match.group(1))["props"]["pageProps"] if match else None
            # Godt canonicalizes multiword queries by reordering the words.
            if (not isinstance(data, dict) or not isinstance(data["params"]["query"], str)
                    or sorted(data["params"]["query"].casefold().split()) != sorted(query.casefold().split())):
                raise ValueError("search response query mismatch")
            hits = [{"title": h["title"], "url": h["links"]["relativeUrl"]} for h in _records(data["recipes"]["items"])]
        else:
            # Do not call a changed/error page an empty successful search.
            marker = "o-search-page--text__hits" if domain == "tine.no" else "search-header__searchform"
            if marker not in page:
                raise ValueError("search results marker missing")
            parser = _SearchLinks(domain)
            parser.feed(page)
            hits = parser.links
    else:
        if content_type != "application/json":
            raise ValueError("incompatible search response")
        data = json.loads(raw)
        if domain == "matprat.no":
            hits = [{"title": h["title"], "url": h["data"]["linkUrl"]} for h in _records(data["searchHits"]) if h["type"] == "recipe"]
            if type(data["totalHits"]) is not int or data["totalHits"] < 0 or bool(data["totalHits"]) != bool(data["searchHits"]):
                raise ValueError("invalid search total")
        elif domain == "frukt.no":
            hits = [{"title": h["pageTitle"], "url": h["url"]} for h in _records(data["recipeSearchResult"]["pages"])]
        elif domain == "godfisk.no":
            hits = [{"title": h["heading"], "url": h["url"]} for section in _records(data["sections"])
                    if section["pageType"] == "Recipe" for h in _records(section["hits"])]
        else:
            hits = [{"title": h["title"], "url": h["url"]} for h in _records(data) if h["type"] == "post"]
    terms = [t for t in re.findall(r"[^\W\d_]+", query.casefold())
             if len(t) >= 3 and t not in {"med", "uten", "oppskrift", "oppskrifter"}]
    def relevance(hit):
        text = (hit["title"] + " " + unquote(hit["url"])).casefold()
        return sum(term in text for term in terms)
    results = []
    for hit in hits[:100]:
        if not isinstance(hit["title"], str) or not isinstance(hit["url"], str):
            raise ValueError("invalid search hit")
        url = urljoin(origin, hit["url"])
        host = (urlsplit(url).hostname or "").lower()
        # Even another enabled publisher cannot be asserted by this source.
        if host != domain and not host.endswith("." + domain):
            continue
        if domain in {"tine.no", "matprat.no", "frukt.no", "godfisk.no", "godt.no"} and not urlsplit(url).path.startswith("/oppskrifter/"):
            continue
        if domain == "trinesmatblogg.no" and not urlsplit(url).path.startswith("/recipe/"):
            continue
        # TINE fuzzy search can return unrelated meat stews for "kikertgryte".
        if domain == "tine.no" and terms and not relevance(hit):
            continue
        results.append({"title": unescape(hit["title"])[:300], "url": url})
    return sorted(results, key=relevance, reverse=True)


def _filtered_hits(hits, settings):
    results, seen = [], set()
    for hit in hits:
        url = hit.get("url")
        if not isinstance(url, str) or len(url) > 2000 or not url_enabled(url, settings) or url in seen:
            continue
        if urlsplit(url).path in {"", "/"}:
            continue
        seen.add(url)
        results.append({"url": url, "title": str(hit.get("title") or "")[:300],
                        "description": str(hit.get("description") or "")[:1000]})
        if len(results) == 8:
            break
    return results


def _brave_search(settings, query, api_key):
    from recipe_import_sources import _get_bytes, RecipeImportSourceError
    # The provider scopes retrieval; local URL checks still enforce the settings.
    scoped = query
    if not settings["broad"]:
        scoped += " (" + " OR ".join("site:" + s["domain"] for s in settings["sites"] if s["enabled"]) + ")"
    if len(scoped) > 600 or len(scoped.split()) > 75:
        raise RecipeImportSourceError("Brave query/scope is too long; shorten the query or select fewer sites")
    url = "https://api.search.brave.com/res/v1/web/search?" + urlencode({
        "q": scoped, "count": 8, "country": "NO", "search_lang": "no",
        "safesearch": "strict", "spellcheck": "false", "text_decorations": "false", "result_filter": "web",
    })
    raw, content_type = _get_bytes(url, maximum=2 * 1024 * 1024, accept="application/json", search_api_key=api_key)
    try:
        value = json.loads(raw)
        if content_type != "application/json" or not isinstance(value, dict) or value.get("type") != "search":
            raise ValueError("incompatible response")
        if not isinstance(value.get("query"), dict) or value["query"].get("original") != scoped:
            raise ValueError("query mismatch")
        web = value.get("web")
        hits = [] if web is None else web["results"]
        return _records(hits)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise RecipeImportSourceError("Brave search response is unavailable or incompatible") from None


def search_web(settings, query, backend=None, *, config=None):
    """No provider switching. Explicit coverage prevents false broad-search claims."""
    from recipe_import_sources import firecrawl_request, RecipeImportSourceError
    from recipe_search_setup import BACKENDS, GUIDE, search_configuration, load_search_key
    config = {} if config is None else config
    plan = search_plan(settings, query)
    unavailable = {**plan, "status": "unavailable", "searched": False, "results": [], "persisted": False,
                   "coverage": "none", "broad_searched": False, "pending_scopes": plan["scopes"],
                   "guide": GUIDE, "next": "Report unavailable and continue local/store planning. No provider fallback was attempted. Use setup guidance to repair the chosen provider."}
    if backend is not None and (not isinstance(backend, str) or backend not in BACKENDS):
        raise ValueError("web search backend must be direct, host, brave or firecrawl")
    if not plan["scopes"]:
        return {**plan, "backend": backend, "results": [], "persisted": False,
                "coverage": "none", "broad_searched": False, "pending_scopes": []}
    if backend is None:
        try:
            backend = search_configuration(config)["backend"]
        except ValueError as exc:
            return {**unavailable, "backend": None, "reason": str(exc)}
    if backend == "host":
        return {**plan, "backend": backend, "results": [], "persisted": False,
                "coverage": "none", "broad_searched": False, "pending_scopes": plan["scopes"]}
    next_step = ("Read selected original pages directly and assess culinary relevance; search hits are not ingredient evidence or storage permission. "
                 "Import only with a valid storage_decision. No automatic provider fallback. "
                 "For pending scopes select host search or an optional API explicitly; see " + GUIDE)
    if backend == "direct":
        def search_site(site):
            domain = site["domain"]
            if domain not in DIRECT_SOURCES:
                return {"domain": domain, "status": "unsupported", "count": 0}, []
            try:
                hits = _filtered_hits(_source_search(domain, query.strip()), settings)
                return {"domain": domain, "status": "completed", "count": len(hits)}, hits
            except (RecipeImportSourceError, ValueError, TypeError, KeyError, IndexError):
                return {"domain": domain, "status": "unavailable", "count": 0,
                        "reason": "First-party search unavailable or incompatible"}, []
        sites = [s for s in plan["settings"]["sites"] if s["enabled"]]
        with ThreadPoolExecutor(max_workers=4) as pool:
            batches = list(pool.map(search_site, sites))
        sources = [source for source, _ in batches]
        # Round-robin keeps one high-volume publisher from crowding out others.
        hits = [batch[index] for index in range(8) for _, batch in batches if index < len(batch)]
        pending = [scope for scope in plan["scopes"] if not scope["domains"] or
                   any(s["domain"] in scope["domains"] and s["status"] != "completed" for s in sources)]
        searched = any(s["status"] == "completed" for s in sources)
        return {**plan, "status": "completed" if searched else "unavailable", "searched": searched,
                "backend": backend, "results": _filtered_hits(hits, settings), "persisted": False,
                "sources": sources, "coverage": "partial" if pending else "complete",
                "broad_searched": False, "pending_scopes": pending,
                "next": ("Only the listed first-party searches ran. Broad/custom or failed scopes remain unsearched. " if pending else
                         "Completed bounded first-page searches, not an exhaustive recipe inventory. ") + next_step}
    try:
        key = load_search_key(config, backend)
        recipe_query = query.strip()
        if "oppskrift" not in recipe_query.casefold():
            recipe_query += " oppskrift"
        if backend == "brave":
            hits = _brave_search(settings, recipe_query, key)
        else:
            payload = {"query": recipe_query, "limit": 8, "country": "NO", "excludeDomains": plan["exclude_domains"], "sources": ["web"]}
            if not settings["broad"]:
                payload["includeDomains"] = [s["domain"] for s in settings["sites"] if s["enabled"]]
            hits = firecrawl_request("search", payload, **({"api_key": key} if key is not None else {})).get("web")
        if not isinstance(hits, list) or any(not isinstance(hit, dict)
                or not isinstance(hit.get("url"), str) or not hit["url"]
                or not isinstance(hit.get("title", ""), str) for hit in hits):
            raise RecipeImportSourceError("Search API results are incompatible")
        return {**plan, "status": "completed", "searched": True, "backend": backend,
                "results": _filtered_hits(hits[:100], settings), "persisted": False, "next": next_step,
                "coverage": "complete", "broad_searched": settings["broad"], "pending_scopes": [],
                "candidate_relevance": "unverified", "search_requests": 1,
                "attribution": "Powered by Brave Search" if backend == "brave" else "Search by Firecrawl"}
    except ValueError as exc:
        return {**unavailable, "backend": backend, "reason": str(exc)}


def storage_decision(value, *, source_kind):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"storage", "basis", "evidence", "license_url"}:
        raise ValueError("storage_decision has unknown fields")
    if value.get("storage") == "link_only":
        if set(value) != {"storage"}:
            raise ValueError("link_only storage_decision needs only storage")
        return {"storage": "link_only"}
    if value.get("storage") != "full" or value.get("basis") not in {"own_recipe", "permission", "license", "private_use"}:
        raise ValueError("full storage_decision needs own_recipe, permission, license or private_use basis")
    if value["basis"] == "own_recipe" and source_kind != "transcript":
        raise ValueError("own_recipe applies only to supplied recipe text")
    evidence = value.get("evidence")
    if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 500:
        raise ValueError("full storage_decision needs a concrete evidence/assessment statement")
    # URL validation and credential rejection use the shared culinary boundary.
    from recipes import normalize_source_url, _bounded_text
    result = {"storage": "full", "basis": value["basis"],
              "evidence": _bounded_text(evidence, "storage decision evidence", maximum=500)}
    if value.get("license_url") is not None:
        result["license_url"] = normalize_source_url(value["license_url"], version=2)
    return result
