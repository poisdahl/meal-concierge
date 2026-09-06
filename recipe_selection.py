"""Bounded recipe retrieval helpers; source I/O remains owned by Application."""

from __future__ import annotations

from copy import deepcopy
import math
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError

from core import HouseholdError
from recipes import normalize_attribution_url


PAGE_SIZE = 20
MAX_SOURCE_PAGES = 6
MAX_DETAILS = 80
SEARCH_SECONDS = 30
# Nine candidates fit the planner's state bound but measured 43–53 seconds for
# seven days. Eight retain a spare choice at 40,320 rather than 181,440 states.
MAX_SHORTLIST_CANDIDATES = 8
MAX_HISTORY_DOCUMENTS = 4_000
MAX_HISTORY_READS = 2_000


def source_identities(recipe: Mapping[str, Any]) -> set[str]:
    """Known source aliases only, never a title or ingredient-name fingerprint."""
    source = recipe.get("source") or {}
    result = set()
    for attribution in (source, source.get("original") or {}):
        publisher = str(attribution.get("publisher") or attribution.get("kind") or "").strip().casefold()
        external_id = str(attribution.get("external_id") or "").strip()
        if publisher and external_id:
            result.add(f"source:{publisher}:{external_id}")
        # An original attribution can name an article containing many recipes.
        # Only its explicit publisher/recipe ID above establishes an alias.
        if attribution is source and attribution.get("url"):
            url = normalize_attribution_url(attribution["url"])
            parsed = urlsplit(url)
            # These are observed aliases for the same retailer recipe route.
            # Unknown query parameters (including revisions) stay significant.
            if parsed.hostname in {"oda.com", "www.oda.com", "meny.no", "www.meny.no", "mathem.se", "www.mathem.se"}:
                url = urlunsplit((parsed.scheme, parsed.hostname.removeprefix("www."), parsed.path.rstrip("/") or "/", parsed.query, ""))
            result.add(f"source-url:{url}")
    return result


def candidate_groups(candidates: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Join aliases transitively, preserving every concrete candidate version."""
    groups: list[tuple[set[str], list[Mapping[str, Any]]]] = []
    for candidate in candidates:
        aliases = source_identities(candidate["recipe"])
        aliases.add("exact:" + candidate["reference_key"])
        # A bank's revisions are one family even without upstream attribution.
        ref = candidate["reference"].get("recipe_ref")
        if ref:
            aliases.add("bank:" + ref["id"])
        members = [candidate]
        remaining = []
        for prior_aliases, prior_members in groups:
            if aliases.intersection(prior_aliases):
                aliases.update(prior_aliases)
                members.extend(prior_members)
            else:
                remaining.append((prior_aliases, prior_members))
        # An alias bridge can reach an earlier group after merging a later one.
        changed = True
        while changed:
            changed = False
            for index, (prior_aliases, prior_members) in enumerate(remaining):
                if aliases.intersection(prior_aliases):
                    aliases.update(prior_aliases)
                    members.extend(prior_members)
                    remaining.pop(index)
                    changed = True
                    break
        groups = [*remaining, (aliases, members)]
    return [members for _aliases, members in groups]


def compact_candidate(recipe: Mapping[str, Any], reference: Mapping[str, Any]) -> dict[str, Any]:
    """Project a loaded version without passing a partial document as a recipe."""
    fields = ("name", "portions", "times", "tags", "source", "source_provider", "schema_version",
              "recipe_digest", "entry_origin", "is_favorite", "locally_modified", "status")
    result = {key: deepcopy(recipe[key]) for key in fields if key in recipe}
    result.update(deepcopy(dict(reference)))
    result["representation"] = "summary"
    result["detail_fields"] = {"ingredients": "not_loaded", "steps": "not_loaded"}
    result["unknown_fields"] = [key for key in ("portions", "times") if recipe.get(key) is None]
    if "readiness" in recipe:
        result["readiness"] = deepcopy(recipe["readiness"])
    return result


def merge_family_usage(group: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Preserve a blocker from any known exact alias without rewriting journals."""
    from planner import canonical
    usages = [item.get("usage") or {} for item in group]
    merged = deepcopy(usages[0])
    merged["eligible"] = all(usage.get("eligible") is True for usage in usages)
    for field in ("last_planned_week", "last_ordered_week", "last_cooked_week", "next_eligible_week"):
        merged[field] = max((usage[field] for usage in usages if usage.get(field)), default=None)
    blockers = {canonical(blocker): blocker for usage in usages for blocker in usage.get("blocked_by", [])}
    merged["blocked_by"] = [deepcopy(blockers[key]) for key in sorted(blockers)]
    return merged


def candidate_needs_input(candidate: Mapping[str, Any]) -> bool:
    # A definite failure cannot erase an independent unresolved hard constraint.
    return any(reason.get("status") == "unknown" for reason in candidate["hard_constraints"]["reasons"])


def _local_history_reference(reference: Mapping[str, Any]) -> dict[str, Any] | None:
    legacy = reference.get("library_recipe_ref")
    if set(reference) == {"library_recipe_ref"} and isinstance(legacy, Mapping) and legacy.get("library_id") == "builtin":
        version = legacy.get("version")
        if isinstance(version, str) and version.isascii() and version.isdigit() and 1 <= len(version) <= 12:
            reference = {"recipe_ref": {"id": legacy.get("recipe_id"), "revision": int(version)}}
    bank = reference.get("recipe_ref")
    if set(reference) in ({"recipe_ref"}, {"discovery_ref"}) and (
        isinstance(bank, Mapping) and bank.get("revision") is not None or reference.get("discovery_ref")
    ):
        return dict(reference)
    return None


def history_source_index(state: Mapping[str, Any], resolve_exact: Callable) -> dict[str, Any]:
    """Index retained source evidence without catalog searches or latest reads."""
    from planner import canonical, digest
    from recipe_libraries import library_recipe_key_aliases
    aliases: dict[str, set[str]] = {}
    key_aliases: dict[str, set[str]] = {}
    retained_refs: dict[str, set[str]] = {}
    covered_records: dict[str, set[str]] = {}
    unresolved: dict[tuple[str, str], str] = {}
    documents = reads = 0
    limited = False

    def bind(key, identities):
        for alias in library_recipe_key_aliases(key):
            key_aliases.setdefault(alias, set()).update(identities)
        for identity in identities:
            aliases.setdefault(identity, set()).add(key)

    def remember(recipe, key, context):
        nonlocal documents, limited
        if documents >= MAX_HISTORY_DOCUMENTS:
            limited = True
            return None
        documents += 1
        if not isinstance(recipe, Mapping) or not isinstance(key, str) or not key:
            unresolved[(str(key or "unknown"), context)] = "retained_recipe_identity_unknown"
            return None
        try:
            identities = source_identities(recipe)
        except (HouseholdError, ValueError):
            unresolved[(key, context)] = "invalid_retained_source"
            return None
        if not identities:
            unresolved[(key, context)] = "source_identity_unknown"
            return None
        bind(key, identities)
        return identities

    menus = [state.get("menu"), *(state.get("menu_planning") or {}).get("history", {}).values(),
             *(state.get("order_snapshots") or {}).values()]
    for menu in menus:
        if not isinstance(menu, Mapping):
            continue
        for collection in ("dishes", "salads"):
            for recipe in menu.get(collection, []):
                if documents >= MAX_HISTORY_DOCUMENTS:
                    limited = True
                    break
                snapshot_digest = digest(recipe)
                key = recipe.get("recipe_key") if isinstance(recipe, Mapping) else None
                identities = remember(recipe, key, "snapshot:" + snapshot_digest)
                if identities:
                    if isinstance(menu.get("menu_id"), str):
                        covered_records.setdefault(menu["menu_id"], set()).update(library_recipe_key_aliases(key))
                    for slot in menu.get("slots", []):
                        reference = _local_history_reference(slot.get("reference") or {})
                        if reference is not None and slot.get("snapshot_digest") == snapshot_digest:
                            retained_refs.setdefault(canonical(reference), set()).update(identities)
            if limited:
                break
        if limited:
            break
    memo = {}
    for menu_id, record in (state.get("recipe_usage") or {}).items():
        if limited:
            break
        slotted_keys = set()
        for slot in record.get("slots", []):
            key = slot.get("recipe_key")
            if not isinstance(key, str):
                continue
            slotted_keys.update(library_recipe_key_aliases(key))
            raw_reference = slot.get("reference") or {}
            reference = _local_history_reference(raw_reference)
            if reference is None:
                unresolved[(key, canonical(raw_reference))] = "exact_local_reference_unavailable"
                continue
            signature = canonical(reference)
            if signature in retained_refs:
                bind(key, retained_refs[signature])
                continue
            if signature not in memo:
                if reads >= MAX_HISTORY_READS or documents >= MAX_HISTORY_DOCUMENTS:
                    limited = True
                    break
                reads += 1
                try:
                    recipe = resolve_exact(reference)
                except (HouseholdError, LookupError, ValueError):
                    unresolved[(key, signature)] = "exact_historical_version_unavailable"
                    memo[signature] = None
                else:
                    memo[signature] = remember(recipe, key, signature)
            if memo[signature]:
                bind(key, memo[signature])
            else:
                unresolved.setdefault((key, signature), "exact_historical_source_unavailable")
        for key in record.get("recipe_keys", []):
            if key not in slotted_keys and key not in covered_records.get(menu_id, set()):
                unresolved[(key, "usage:" + menu_id)] = "historical_source_not_retained"
    return {"aliases": {key: sorted(values) for key, values in aliases.items()},
            "key_aliases": {key: sorted(values) for key, values in key_aliases.items()},
            "unresolved": [{"recipe_key": key, "context": context, "reason": reason}
                           for (key, context), reason in sorted(unresolved.items())],
            "status": "history_work_limit" if limited else "partial" if unresolved else "complete",
            "work": {"documents": documents, "exact_reads": reads,
                     "maximum_documents": MAX_HISTORY_DOCUMENTS, "maximum_exact_reads": MAX_HISTORY_READS}}


def family_history_usage(candidate: Mapping[str, Any], index: Mapping[str, Any], usage_for_key: Callable) -> dict[str, Any]:
    """Evaluate each alias separately so a retired alias cannot clear another."""
    keys = {candidate["recipe_key"]}
    pending = source_identities(candidate["recipe"]) | set(index["key_aliases"].get(candidate["recipe_key"], []))
    seen = set()
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        for key in index["aliases"].get(identity, []):
            keys.add(key)
            pending.update(set(index["key_aliases"].get(key, [])) - seen)
    usage = merge_family_usage([{"usage": usage_for_key(key)} for key in sorted(keys)])
    usage["family_recipe_keys"] = sorted(keys)
    usage["history_coverage"] = index["status"]
    usage["unresolved_history_count"] = len(index["unresolved"])
    return usage


def context_queries(profile: Mapping[str, Any], provider: str) -> list[str]:
    cuisine = profile.get("cuisine") or {}
    diet = profile.get("diet") or {}
    values = [*cuisine.get("wanted", []), *cuisine.get("flavours", []), *diet.get("prioritise", [])]
    values += ["middag", "fisk", "kyckling" if provider == "mathem" else "kylling", "vegetar", "pasta", "suppe"]
    result = []
    for value in values:
        query = " ".join(str(value).split())[:200]
        if query and query.casefold() not in {item.casefold() for item in result}:
            result.append(query)
        if len(result) == MAX_SOURCE_PAGES:
            break
    return result


def shortlist_capacity(days: int) -> int:
    from planner import MAX_CANDIDATES, MAX_DAYS, MAX_EXPLORED_STATES, PlannerError
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise PlannerError("selection days must be from one to seven")
    return max(count for count in range(days, min(MAX_CANDIDATES, MAX_SHORTLIST_CANDIDATES) + 1)
               if math.perm(count, days) <= MAX_EXPLORED_STATES)


def shortlist(candidates: list[Mapping[str, Any]], request: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    from planner import prepare_candidate, _slot_reasons
    prepared = []
    for group in candidate_groups(candidates):
        usage = merge_family_usage(group)
        prepared.extend(prepare_candidate({**item, "usage": usage}, profile, request.get("cooldown_overrides", {}), request.get("portions")) for item in group)
    representatives = []
    unknown = []
    rejected = []
    for group in candidate_groups(prepared):
        # Evaluate readiness before preferring a saved version. Never overwrite it.
        ordered = sorted(group, key=lambda item: (
            0 if item["hard_constraints"]["status"] == "pass" else 1 if candidate_needs_input(item) else 2,
            "recipe_ref" not in item["reference"],
            not item.get("locally_modified", False),
            not item.get("is_favorite", False), item["reference_key"],
        ))
        chosen = ordered[0]
        status = chosen["hard_constraints"]["status"]
        (representatives if status == "pass" else unknown if candidate_needs_input(chosen) else rejected).append(chosen)
    dates = request["dates"]
    def score(item):
        return sum(reason["weight"] for index, day in enumerate(dates)
                   for reason in _slot_reasons(item, day, index, len(dates), profile))
    representatives.sort(key=lambda item: (-score(item), item["reference_key"]))
    capacity = shortlist_capacity(len(dates))
    return {"candidates": representatives[:capacity], "suitable_count": len(representatives),
            "unknown": unknown, "rejected": rejected, "capacity": capacity,
            "shortlisted_out": max(0, len(representatives) - capacity)}


def collect_candidates(*, source_queries: Mapping[str, list[str] | None], fetch_page: Callable,
                       resolve: Callable, request: Mapping[str, Any], profile: Mapping[str, Any],
                       clock: Callable = time.monotonic) -> dict[str, Any]:
    """Bounded internal orchestration, not an external provider API contract.

    Application adapters supply candidates/next_cursor/exhausted from verified
    source semantics. Only an explicit exhausted=True establishes query end.
    Both callbacks receive one absolute deadline and must bound their own I/O.
    """
    from planner import canonical
    start = clock()
    deadline = start + SEARCH_SECONDS
    states = {source: {"source": source, "enabled": queries is not None,
                       "status": "searching" if queries is not None else "disabled",
                       "pages": 0, "discovered": 0, "details": 0, "rejected_details": 0,
                       "queries": [], "query_index": 0, "cursor": None, "limited": False}
              for source, queries in source_queries.items()}
    resolved = []
    seen = set()
    cursors = {source: set() for source in states}
    detail_count = 0
    selection = shortlist([], request, profile)
    while any(state["status"] == "searching" for state in states.values()):
        for source, state in states.items():
            if state["status"] != "searching":
                continue
            queries = source_queries[source] or []
            if clock() >= deadline:
                state["status"] = "timeout"
                continue
            if state["query_index"] >= len(queries):
                state["status"] = "search_limit" if state["limited"] or not state["pages"] else "unsuitable" if state["discovered"] else "empty"
                continue
            if state["pages"] >= MAX_SOURCE_PAGES or detail_count >= MAX_DETAILS:
                state["status"] = "search_limit"
                continue
            query = queries[state["query_index"]]
            if query not in state["queries"]:
                state["queries"].append(query)
            state["pages"] += 1
            try:
                page = fetch_page(source, query, state["cursor"], PAGE_SIZE, deadline)
                if not isinstance(page, Mapping) or not isinstance(page.get("candidates"), list) or len(page["candidates"]) > PAGE_SIZE:
                    raise ValueError("invalid source page")
                if page.get("status") in {"unavailable", "timeout", "rate_limited", "search_limit"}:
                    state["status"] = page["status"]
                for summary in page["candidates"]:
                    state["discovered"] += 1
                    if detail_count >= MAX_DETAILS or clock() >= deadline:
                        state["status"] = "timeout" if clock() >= deadline else "search_limit"
                        break
                    reference = {key: summary[key] for key in ("recipe_ref", "discovery_ref") if key in summary}
                    signature = canonical(reference) if reference else canonical(summary)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    detail_count += 1
                    state["details"] += 1
                    try:
                        resolved.append({**resolve(summary, deadline), "discovery_source": source})
                    except TimeoutError:
                        state["status"] = "timeout"
                        break
                    except (LookupError, ValueError, HouseholdError):
                        # An exact stale version is not silently replaced with latest.
                        state["rejected_details"] += 1
                cursor = page.get("next_cursor")
                if cursor is not None:
                    signature = canonical(cursor)
                    if signature in cursors[source]:
                        state["status"] = "search_limit"
                    cursors[source].add(signature)
                    state["cursor"] = cursor
                else:
                    state["limited"] |= page.get("exhausted") is not True
                    state["query_index"] += 1
                    state["cursor"] = None
                    cursors[source].clear()
            except TimeoutError:
                state["status"] = "timeout"
            except HTTPError as exc:
                state["status"] = "rate_limited" if exc.code == 429 else "unavailable"
            except (OSError, ValueError, TypeError, HouseholdError):
                state["status"] = "unavailable"
        selection = shortlist(resolved, request, profile)
        # Every enabled source had an opportunity; no fixed source quotas.
        if len(selection["candidates"]) >= selection["capacity"]:
            for state in states.values():
                if state["status"] == "searching":
                    state["status"] = "search_limit"
            break
    for state in states.values():
        from planner import prepare_candidate
        evaluated = [prepare_candidate(item, profile, request.get("cooldown_overrides", {}), request.get("portions"))
                     for item in resolved if item.get("discovery_source") == state["source"]]
        state["suitable"] = sum(item["hard_constraints"]["status"] == "pass" for item in evaluated)
        state["needs_input"] = sum(candidate_needs_input(item) for item in evaluated)
        if state["status"] == "unsuitable" and state["suitable"]:
            state["status"] = "ready"
        for key in ("query_index", "cursor", "limited"):
            state.pop(key)
    enabled = [state for state in states.values() if state["enabled"]]
    ai_eligible = (bool(enabled) and selection["suitable_count"] == 0 and not selection["unknown"]
                   and all(state["status"] in {"empty", "unsuitable"} and state["rejected_details"] == 0 and state["needs_input"] == 0 for state in enabled))
    status = "ready" if len(selection["candidates"]) >= len(request["dates"]) else "needs_input" if selection["unknown"] else "shortfall" if selection["suitable_count"] else "no_candidates"
    return {**selection, "status": status, "sources": list(states.values()), "ai_fallback_eligible": ai_eligible,
            "shortfall": max(0, len(request["dates"]) - len(selection["candidates"])),
            "work": {"detail_calls": detail_count, "elapsed_seconds": clock() - start,
                     "maximum_details": MAX_DETAILS, "maximum_pages_per_source": MAX_SOURCE_PAGES,
                     "search_seconds": SEARCH_SECONDS}}
