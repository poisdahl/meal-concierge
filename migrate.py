#!/usr/bin/env python3
"""Copy only durable household values into the small state format."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from core import HouseholdError, _atomic_json, _validate_product_items, due_recurring, initial_state, recurring_schedule


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not an object")
    return value


def migrate(config: dict[str, Any], planning: dict[str, Any], schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    config, planning, schedule = deepcopy((config, planning, schedule))
    result = initial_state(config)
    documents = planning.get("documents") if isinstance(planning.get("documents"), dict) else {}
    product_favorites = (documents.get("favorites") or {}).get("items", [])
    recurring = (documents.get("recurring_items") or {}).get("items", [])
    if isinstance(product_favorites, list):
        _validate_product_items(product_favorites, "product_favorites")
        result["product_favorites"] = product_favorites
    _validate_product_items(recurring, "recurring_items")
    result["recurring_items"] = recurring
    preferences = (documents.get("preferences") or {}).get("content", "")
    if isinstance(preferences, str):
        email = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", preferences, re.IGNORECASE)
        if email:
            result["email_recipient"] = email.group(0)
    if schedule:
        records = schedule.get("schedules", schedule.get("records", []))
        if isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict):
            source = records[0]
            for key in ("enabled", "weekday", "time", "timezone", "mode", "delivery", "maximum_total", "auto_checkout"):
                if key in source:
                    result["schedule"][key] = source[key]
            if isinstance(result["schedule"].get("delivery"), dict):
                result["schedule"]["delivery"].setdefault("strategy", "cheapest")
    today = datetime.now(ZoneInfo(result["schedule"]["timezone"])).date()
    for index, item in enumerate(result["recurring_items"]):
        source_schedule = item.get("schedule")
        try:
            # An interval must come from the source, never a fabricated default.
            # An unspecified anchor may be normalized to the migration date.
            if not isinstance(source_schedule, dict) or not {"every", "unit"} <= set(source_schedule):
                raise HouseholdError("missing explicit interval")
            normalized = recurring_schedule(source_schedule, today)
            if normalized["unit"] == "days":
                normalized["anchor"] = date.fromisoformat(normalized["anchor"]).isoformat()
            elif normalized["unit"] == "months":
                date.fromisoformat(normalized["anchor"] + "-01")
            item["schedule"] = normalized
            due_recurring(item, today)
        except (HouseholdError, TypeError, ValueError, OverflowError) as exc:
            raise HouseholdError(
                f"recurring_items[{index}].schedule is missing or invalid; supply an explicit "
                "positive integer every and unit=days|weeks|months with a valid anchor "
                "(or anchor=null to use the migration date) before importing"
            ) from exc
    # Deliberately omit menu/history/checkpoints/leases/attempts/generations.
    return result


def write_migration(output_directory: Path, config: dict[str, Any], planning: dict[str, Any],
                    schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write a validated copy only to a new private directory owned by this attempt."""
    result = migrate(config, planning, schedule)
    output_directory = Path(output_directory)
    try:
        # mkdir is the exclusive destination claim, including empty directories,
        # files and dangling symlinks. Never open an existing household store.
        output_directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise HouseholdError("output destination already exists; choose a new, unused directory") from exc
    try:
        _atomic_json(output_directory / "state.json", result)
    except Exception as exc:
        # A failed write may already be visible. Preserve it for inspection;
        # an ordinary rerun must not replace or resume that attempt.
        raise HouseholdError(
            "legacy import did not complete; retain the fresh output directory for recovery "
            "and use a different unused destination for any new attempt"
        ) from exc
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--old-planning", type=Path, required=True)
    parser.add_argument("--old-schedule", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    config = load(args.config)
    planning = load(args.old_planning)
    schedule = load(args.old_schedule) if args.old_schedule and args.old_schedule.exists() else None
    try:
        result = write_migration(args.output_directory, config, planning, schedule)
    except HouseholdError as exc:
        parser.exit(2, f"migration: {exc}\n")
    print(json.dumps({"ok": True, "product_favorites_count": len(result["product_favorites"]), "recurring_items": len(result["recurring_items"]), "schedule_enabled": result["schedule"]["enabled"], "menu_migrated": False, "checkout_migrated": False, "email_recipient_configured": bool(result["email_recipient"])}))


if __name__ == "__main__":
    main()
