#!/usr/bin/env python3
"""Finite commands for actual Muse native observations and existing custody guards.

Input is the literal automatically delivered browser JSON, with actual native
completion time supplied separately. These remain trusted host observations,
not independently authenticated DOM evidence. Use closed stdin and a bounded
supported executor: a byte limit does not impose an elapsed-time limit.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clients.muse import MAX_RESPONSE, private_directory
from core import HouseholdError
from muse_browser import (claim_request, consume_request, digest,
                          durable_publish, end_request, request_record,
                          respond_request, validate_claim, validate_response)
from service_common import strict_json_loads, validate_request_value


class ProducerError(Exception):
    def __init__(self, stage, category):
        self.stage, self.category = stage, category
        super().__init__(category)


def checked(stage, category, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (HouseholdError, OSError, ValueError, UnicodeError, RecursionError):
        raise ProducerError(stage, category) from None


def parse_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    text = checked("input", "invalid_json", bytes.decode, data, "utf-8")
    value = checked("input", "invalid_json", json.loads, text,
                    object_pairs_hook=unique, parse_float=strict_json_loads,
                    parse_constant=strict_json_loads)
    checked("input", "invalid_json", validate_request_value, value)
    if not isinstance(value, dict):
        raise ProducerError("input", "facts_object_required")
    return value


def publication_fence(directory, request_id):
    # One canonical request fence; a caller cannot select another output root.
    root = directory / "publications"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise ProducerError("publish", "private_output_required") from None
    checked("publish", "private_output_required", private_directory, root)
    checked("publish", "invocation_consumed", durable_publish,
            root / (request_id + ".json"),
            {"attempted_at": datetime.now(timezone.utc).isoformat()})


def publish(directory, request_id, task_id, data, observed_at, ending_state, *, ending=False):
    directory, record = checked("request", "custody_rejected", request_record,
                                directory, request_id, task_id, active=not ending)
    if not ending:
        publication_fence(directory, request_id)
    facts = parse_json(data)
    response = {"request_id": request_id, "request_digest": digest(record), "task_id": task_id,
                "observed_at": observed_at, "task_state": ending_state, "facts": facts}
    checked("request", "claim_rejected", validate_claim, directory, record, response=response)
    checked("publish", "invalid_handoff", validate_response, record, response, fresh=not ending)
    checked("publish", "response_write_uncertain", end_request if ending else respond_request,
            directory, request_id, task_id, response)
    return {"ended" if ending else "published": True}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProducerError("input", "arguments")


def main():
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("claim", "consume", "respond", "end"):
        command = commands.add_parser(name)
        command.add_argument("--directory", required=True)
        command.add_argument("--request-id", required=True)
        command.add_argument("--task-id", required=True)
        if name in {"respond", "end"}:
            command.add_argument("--observed-at", required=True)
            command.add_argument("--ending-state", required=True, choices=["completed"])
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", args.request_id) is None:
        raise ProducerError("input", "request_identity")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", args.task_id) is None:
        raise ProducerError("input", "task_identity")
    common = (args.directory, args.request_id, args.task_id)
    if args.command in {"claim", "consume"}:
        checked("request", "custody_rejected", claim_request if args.command == "claim" else consume_request, *common)
        result = {"claimed" if args.command == "claim" else "consumed": True}
    else:
        data = sys.stdin.buffer.read(MAX_RESPONSE + 1)
        if not data or len(data) > MAX_RESPONSE:
            raise ProducerError("input", "bounded_closed_stdin_required")
        result = publish(*common, data, args.observed_at, args.ending_state, ending=args.command == "end")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProducerError as error:
        print(json.dumps({"error": {"stage": error.stage, "category": error.category}}), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print(json.dumps({"error": {"stage": "input", "category": "unexpected_failure"}}), file=sys.stderr)
        raise SystemExit(1)
