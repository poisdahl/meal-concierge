"""Bind retailer checkout rows to the supplied product fields.

Only text visibly associated with an individual checkout row, or a product ID
proven to belong to that row, may establish its identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Mapping
from typing import Any


_UNIT = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(kg|mg|g|ml|cl|dl|l)(?!\w)", re.I)
_FIELD_SEPARATOR = re.compile(r"(?<!\d),|,(?!\d)")


def _display(value: str) -> str:
    """Normalize presentation only, retaining punctuation and word order."""
    value = " ".join(unicodedata.normalize("NFC", value).casefold().split())
    return _UNIT.sub(lambda match: f"{match[1]} {match[2]}", value)


def _fields(description: str) -> list[str]:
    """Return complete comma-delimited fields and their contiguous sequences."""
    cuts = [match.start() for match in _FIELD_SEPARATOR.finditer(description)]
    starts, ends = [0, *(cut + 1 for cut in cuts)], [*cuts, len(description)]
    return [description[starts[start]:ends[end - 1]].strip()
            for start in range(len(starts)) for end in range(start + 1, len(starts) + 1)
            if all(description[starts[index]:ends[index]].strip() for index in range(start, end))]


def _title_candidates(name: str, description: str, brand: str) -> set[str]:
    name, description, brand = map(_display, (name, description, brand))
    candidates = {name}
    if brand and name.startswith(brand + " "):
        candidates.add(name[len(brand) + 1:])
    for original in tuple(candidates):
        for phrase in _fields(description):
            if not original.endswith(phrase):
                continue
            prefix = original[:-len(phrase)]
            if prefix and prefix[-1] in " ,":
                trimmed = prefix.rstrip(" ,")
                if trimmed:
                    candidates.add(trimmed)
    return candidates


def _subtitle_candidates(description: str, brand: str) -> set[str]:
    description, brand = _display(description), _display(brand)
    if not brand:
        return {description}
    if not description:
        return {brand}
    return {description + ", " + brand, description + " " + brand}


def _quantity(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value <= 1_000_000
    return (isinstance(value, float) and math.isfinite(value)
            and 0 < value <= 1_000_000 and value.is_integer())


def _product_id(row: Mapping[str, Any]) -> bool:
    value = row.get("product_id")
    return value is None or isinstance(value, str) and bool(value.strip())


def _valid_rows(expected: Any, actual: Any) -> bool:
    if not isinstance(expected, list) or not isinstance(actual, list):
        return False
    if len(expected) != len(actual):
        return False
    for row, fields in ((row, ("name", "description", "brand")) for row in expected):
        if not isinstance(row, Mapping) or not _quantity(row.get("quantity")):
            return False
        if any(not isinstance(row.get(field), str) for field in fields) or not row["name"].strip():
            return False
        if not _product_id(row):
            return False
    for row in actual:
        if not isinstance(row, Mapping) or not _quantity(row.get("quantity")):
            return False
        if row.get("identity_conflict") is not None and row.get("identity_conflict") is not False:
            return False
        if any(not isinstance(row.get(field), str) for field in ("title", "subtitle")) or not row["title"].strip():
            return False
        if not _product_id(row):
            return False
    return True


def _candidate_rows(expected: list[Mapping[str, Any]], actual: list[Mapping[str, Any]]) -> list[list[int]]:
    candidates = []
    for line in expected:
        titles = _title_candidates(line["name"], line["description"], line["brand"])
        subtitles = _subtitle_candidates(line["description"], line["brand"])
        matches = []
        for index, row in enumerate(actual):
            if row["quantity"] != line["quantity"]:
                continue
            expected_id, actual_id = line.get("product_id"), row.get("product_id")
            if expected_id and actual_id:
                if expected_id == actual_id:
                    matches.append(index)
                continue
            if _display(row["title"]) in titles and _display(row["subtitle"]) in subtitles:
                matches.append(index)
        candidates.append(matches)
    return candidates


def _assignment(candidates: list[list[int]]) -> list[int] | None:
    """Find one complete assignment with an augmenting path for each row."""
    owner = [-1] * len(candidates)
    assigned = [-1] * len(candidates)
    for start in range(len(candidates)):
        queue = deque([start])
        seen_lines = {start}
        previous = {}
        free = None
        while queue and free is None:
            line = queue.popleft()
            for index in candidates[line]:
                if index in previous:
                    continue
                previous[index] = line
                if owner[index] < 0:
                    free = index
                    break
                if owner[index] not in seen_lines:
                    seen_lines.add(owner[index])
                    queue.append(owner[index])
        if free is None:
            return None
        index = free
        while index >= 0:
            line = previous[index]
            old = assigned[line]
            assigned[line] = index
            owner[index] = line
            index = old
    return assigned


def _ambiguous(candidates: list[list[int]], assigned: list[int]) -> bool:
    """A directed alternating cycle means a second complete assignment."""
    owner = {index: line for line, index in enumerate(assigned)}
    incoming = [0] * len(candidates)
    for line in range(len(candidates)):
        for index in candidates[line]:
            if index != assigned[line]:
                incoming[owner[index]] += 1
    queue = deque(line for line, degree in enumerate(incoming) if degree == 0)
    visited = 0
    while queue:
        line = queue.popleft()
        visited += 1
        for index in candidates[line]:
            if index != assigned[line]:
                other = owner[index]
                incoming[other] -= 1
                if incoming[other] == 0:
                    queue.append(other)
    return visited != len(candidates)


def checkout_line_mismatch(expected: Any, actual: Any) -> str | None:
    """Return a compact, text-free reason, or ``None`` for a unique match."""
    if not _valid_rows(expected, actual):
        return "invalid checkout rows or row count"
    candidates = _candidate_rows(expected, actual)
    missing = [index for index, matches in enumerate(candidates) if not matches]
    if missing:
        shown = ",".join(map(str, missing[:8]))
        more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
        return "checkout row identity or quantity differs at expected rows " + shown + more
    assigned = _assignment(candidates)
    if assigned is None:
        return "checkout rows cannot be assigned one to one"
    if _ambiguous(candidates, assigned):
        return "checkout row assignment is ambiguous"
    return None


def checkout_lines_match(expected: Any, actual: Any) -> bool:
    return checkout_line_mismatch(expected, actual) is None


def _review_digest(expected: Any, actual: Any, binding: Any) -> str:
    if binding is None:
        raise ValueError("Checkout identity binding is missing")
    try:
        encoded = json.dumps({"expected": expected, "actual": actual, "binding": binding},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Checkout identity binding is invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _review_issue(expected: Any, actual: Any, digest: str, remaining_expected: set[int],
                  remaining_actual: set[int], next_step: str) -> dict[str, Any]:
    def project(rows: Any, indices: set[int], fields: tuple[str, ...],
                *, include_missing: bool = False) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        return [{"index": index, **{field: rows[index].get(field) for field in fields
                                   if include_missing or field in rows[index]}}
                if isinstance(rows[index], Mapping) else {"index": index}
                for index in sorted(indices)]

    return {"matched": False, "issue": {
        "digest": digest,
        "expected": project(expected, remaining_expected,
                            ("product_id", "name", "description", "brand", "quantity"),
                            include_missing=True),
        "actual": project(actual, remaining_actual,
                          ("title", "subtitle", "quantity", "product_id")),
        "next": next_step,
    }}


def _same_display(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return (_display(actual["title"]) in _title_candidates(expected["name"], expected["description"], expected["brand"])
            and _display(actual["subtitle"]) in _subtitle_candidates(expected["description"], expected["brand"]))


def review_checkout_lines(expected: Any, actual: Any, *, binding: Any,
                          review: Any = None) -> dict[str, Any]:
    """Bind rows automatically, then permit a digest-bound review of display differences.

    A review can resolve only the unmatched display pairs. It cannot change
    quantity, a proven row ID, a native identity conflict, or a locked match.
    """
    digest = _review_digest(expected, actual, binding)
    if review is not None:
        if not isinstance(review, Mapping) or review.get("digest") != digest:
            raise ValueError("Checkout identity review is stale or malformed")
    expected_indices = set(range(len(expected))) if isinstance(expected, list) else set()
    actual_indices = set(range(len(actual))) if isinstance(actual, list) else set()

    def blocked(message: str) -> dict[str, Any]:
        if review is not None:
            raise ValueError("Checkout identity review cannot override " + message)
        # These failures cannot be resolved by a model display decision. Keep
        # invalid or arbitrarily large retailer values out of the diagnostic.
        return {"matched": False, "issue": {"digest": digest,
                "next": message + "; refresh the checkout"}}

    if not _valid_rows(expected, actual):
        return blocked("invalid rows, row count, quantity, or native identity conflict")
    expected_ids = [row.get("product_id") for row in expected if row.get("product_id")]
    actual_ids = [row.get("product_id") for row in actual if row.get("product_id")]
    if any(count > 1 for count in Counter(expected_ids).values()) or any(
            count > 1 for count in Counter(actual_ids).values()):
        return blocked("ambiguous product IDs")

    locked: dict[int, int] = {}
    actual_by_id = {row["product_id"]: index for index, row in enumerate(actual)
                    if row.get("product_id")}
    for index, line in enumerate(expected):
        product_id = line.get("product_id")
        if product_id and product_id in actual_by_id:
            actual_index = actual_by_id[product_id]
            if actual[actual_index]["quantity"] != line["quantity"]:
                return blocked("proven product ID quantity mismatch")
            locked[index] = actual_index

    remaining_expected = expected_indices - locked.keys()
    remaining_actual = actual_indices - set(locked.values())

    def compatible(line_index: int, row_index: int) -> bool:
        line, row = expected[line_index], actual[row_index]
        return (line["quantity"] == row["quantity"]
                and not (line.get("product_id") and row.get("product_id")
                         and line["product_id"] != row["product_id"]))

    if Counter(expected[index]["quantity"] for index in remaining_expected) != Counter(
            actual[index]["quantity"] for index in remaining_actual):
        return blocked("checkout row quantities differ")

    left, right = sorted(remaining_expected), sorted(remaining_actual)
    compatible_rows = [[offset for offset, other in enumerate(right) if compatible(index, other)]
                       for index in left]
    if _assignment(compatible_rows) is None:
        return blocked("contradictory product IDs or row quantities")

    while remaining_expected:
        edges = {index: [other for other in sorted(remaining_actual)
                         if compatible(index, other) and _same_display(expected[index], actual[other])]
                 for index in sorted(remaining_expected)}
        reverse = Counter(other for matches in edges.values() for other in matches)
        pairs = [(index, matches[0]) for index, matches in edges.items()
                 if len(matches) == 1 and reverse[matches[0]] == 1]
        if not pairs:
            break
        for index, other in pairs:
            locked[index] = other
            remaining_expected.remove(index)
            remaining_actual.remove(other)

    if remaining_expected:
        left, right = sorted(remaining_expected), sorted(remaining_actual)
        offsets = {index: offset for offset, index in enumerate(right)}
        candidates = [[offsets[other] for other in right
                       if compatible(index, other) and _same_display(expected[index], actual[other])]
                      for index in left]
        if all(candidates):
            assigned = _assignment(candidates)
            if assigned is not None and not _ambiguous(candidates, assigned):
                for index, offset in zip(left, assigned, strict=True):
                    locked[index] = right[offset]
                remaining_expected.clear()
                remaining_actual.clear()

    if remaining_expected:
        # Cosmetic reasoning cannot distinguish two rows that expose the
        # same evidence. Matching IDs above may resolve them; assigning row
        # indexes alone must not invent which remaining SKU is present.
        expected_evidence = Counter(
            (*(_display(expected[index][field]) for field in ("name", "description", "brand")),
             expected[index]["quantity"])
            for index in remaining_expected)
        actual_evidence = Counter(
            (actual[index].get("product_id"), _display(actual[index]["title"]),
             _display(actual[index]["subtitle"]), actual[index]["quantity"])
            for index in remaining_actual)
        if any(count > 1 for count in (*expected_evidence.values(), *actual_evidence.values())):
            return blocked("indistinguishable unresolved product identities")

    if review is None:
        if not remaining_expected:
            return {"matched": True}
        return _review_issue(
            expected, actual, digest, remaining_expected, remaining_actual,
            "Review whether each remaining checkout row is the same product; supply one "
            "reasoned expected_index/actual_index decision per row with this digest")

    decisions = review.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(remaining_expected):
        raise ValueError("Checkout identity review does not cover every unresolved row")
    used_expected, used_actual = set(), set()
    accepted = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise ValueError("Checkout identity review decision is invalid")
        index, other, reason = (decision.get("expected_index"), decision.get("actual_index"),
                                decision.get("reason"))
        if (type(index) is not int or type(other) is not int or index not in remaining_expected
                or other not in remaining_actual or index in used_expected or other in used_actual):
            raise ValueError("Checkout identity review remaps or reuses a row")
        if not isinstance(reason, str) or not 0 < len(reason.strip()) <= 300:
            raise ValueError("Checkout identity review reason is missing or too long")
        if not compatible(index, other):
            raise ValueError("Checkout identity review contradicts quantity or product ID")
        used_expected.add(index)
        used_actual.add(other)
        accepted.append({"expected_index": index, "actual_index": other,
                         "reason": reason.strip()})
    if used_expected != remaining_expected or used_actual != remaining_actual:
        raise ValueError("Checkout identity review is not a complete one-to-one assignment")
    return {"matched": True, "review": {"digest": digest,
                                        "decisions": sorted(accepted, key=lambda value: value["expected_index"])}}
