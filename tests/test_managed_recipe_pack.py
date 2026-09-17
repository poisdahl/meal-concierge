"""Managed local collection inboxes keep local packs separate from publisher packs."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE))

from core import HouseholdError, StateStore  # noqa: E402
from recipe_portable import FORMAT, canonical_bytes, write_archive  # noqa: E402
from recipes import RecipeError, RecipeStore, normalize_recipe  # noqa: E402
from service import Application  # noqa: E402


class Provider:
    def probe(self, **_kwargs):
        return {"protocol_version": "synthetic", "server": {"name": "synthetic"}, "tool_count": 0}


class ManagedRecipePackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.archive_count = 0
        self.store = StateStore(self.state, {"household": "managed-pack-test", "provider": "oda"})
        self.app = Application(
            self.store, Provider(), None, recipe_pack_inbox=self.inbox,
        )

    @staticmethod
    def recipe(name: str) -> dict:
        return normalize_recipe({
            "name": name,
            "portions": 2,
            "ingredients": ["200 g lentils"],
            "steps": ["Simmer."],
            "source": {"kind": "user", "relationship": "user_supplied"},
            "rights": {"storage": "full"},
        })

    def stage(
        self, records: list[dict], *, version: str, revision: int,
        pack_id: str = "family-recipes",
    ) -> tuple[str, str]:
        self.archive_count += 1
        record_file = self.root / f"records-{revision}-{self.archive_count}.jsonl"
        record_file.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in records))
        archive = self.root / f"collection-{revision}-{version}-{self.archive_count}.zip"
        write_archive(archive, {
            "format": FORMAT,
            "format_version": 1,
            "kind": "collection",
            "pack_id": pack_id,
            "pack_version": version,
            "pack_revision": revision,
            "normalizer_version": "test1",
            "recipe_schema_version": 1,
            "membership_mode": "authoritative",
            "records_count": len(records),
        }, {"records.jsonl": record_file})
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive_id = digest + ".zip"
        shutil.copyfile(archive, self.inbox / archive_id)
        return archive_id, digest

    def request(self, action: str, **kwargs):
        return self.app.handle({"operation": "recipe_pack", "action": action, **kwargs})

    def test_authoritative_refresh_and_removal_preserve_other_origins_and_favorites(self):
        first_records = [
            {"recipe_id": key, "status": "draft", "recipe": self.recipe(name)}
            for key, name in (("keep", "Keep"), ("remove", "Remove"))
        ]
        archive_id, digest = self.stage(first_records, version="1", revision=1)
        self.assertTrue(self.request("status")["available"])
        inspected = self.request("inspect", archive_id=archive_id)
        self.assertEqual(
            (inspected["kind"], inspected["pack_id"], inspected["pack_revision"], inspected["sha256"]),
            ("collection", "family-recipes", 1, digest),
        )
        with self.assertRaisesRegex(RecipeError, "explicit removal authorization"):
            self.request(
                "import", archive_id=archive_id, expected_sha256=digest,
                allow_recipe_removals=False,
            )
        first = self.request(
            "import", archive_id=archive_id, expected_sha256=digest,
            allow_recipe_removals=True,
        )
        self.assertEqual((first["created"], first["deleted"]), (2, 0))

        bank = RecipeStore(self.state / "recipes.sqlite3", "managed-pack-test")
        user = bank.save(self.recipe("User recipe"))
        bank.set_favorite(user["library_recipe_ref"], True, idempotency_key="user-favorite")
        publisher = bank.import_pack_record(
            self.recipe("Publisher recipe"), pack_id="wikibooks-themealdb-en",
            recipe_id="publisher", version="1", entry_origin="bundled",
        )["recipe"]
        bank.set_favorite(publisher["library_recipe_ref"], True, idempotency_key="publisher-favorite")

        second_id, second_digest = self.stage(first_records[:1], version="2", revision=2)
        refreshed = self.request(
            "import", archive_id=second_id, expected_sha256=second_digest,
            allow_recipe_removals=True,
        )
        self.assertEqual((refreshed["unchanged"], refreshed["deleted"]), (1, 1))
        remaining_collection = bank.search(entry_origin="collection", limit=10)
        self.assertEqual([entry["pack"]["recipe_id"] for entry in remaining_collection], ["keep"])

        removed = self.request(
            "remove", archive_id=second_id, expected_sha256=second_digest,
        )
        self.assertEqual((removed["deleted"], removed["deleted_favorites"]), (1, 0))
        self.assertEqual(bank.search(entry_origin="collection", limit=10), [])
        self.assertTrue(bank.get(user["id"])["is_favorite"])
        self.assertTrue(bank.get(publisher["id"])["is_favorite"])
        self.assertEqual(bank.get(publisher["id"])["entry_origin"], "bundled")

    def test_import_rechecks_the_staged_archive_and_blocks_active_work(self):
        records = [{"recipe_id": "keep", "status": "draft", "recipe": self.recipe("Keep")}]
        archive_id, digest = self.stage(records, version="1", revision=1)
        self.request("inspect", archive_id=archive_id)
        (self.inbox / archive_id).write_bytes(b"replaced after inspection")
        with self.assertRaisesRegex(HouseholdError, "differs from expected_sha256"):
            self.request(
                "import", archive_id=archive_id, expected_sha256=digest,
                allow_recipe_removals=True,
            )
        bank = RecipeStore(self.state / "recipes.sqlite3", "managed-pack-test")
        self.assertEqual(bank.search(limit=10), [])

        archive_id, digest = self.stage(records, version="1", revision=1)
        with self.store.locked() as state:
            state["pending_checkout"] = {"status": "awaiting_confirmation"}
        with self.assertRaisesRegex(HouseholdError, "finish the active cart"):
            self.request(
                "import", archive_id=archive_id, expected_sha256=digest,
                allow_recipe_removals=True,
            )
        self.assertEqual(bank.search(limit=10), [])

    def test_only_digest_named_direct_inbox_members_are_accepted(self):
        self.assertTrue(self.request("status")["available"])
        with self.assertRaisesRegex(HouseholdError, "archive_id"):
            self.request("inspect", archive_id="../not-a-pack.zip")
        archive_id, _digest = self.stage(
            [{"recipe_id": "publisher", "status": "draft", "recipe": self.recipe("Reserved")}],
            version="1", revision=1, pack_id="wikibooks-themealdb-en",
        )
        with self.assertRaisesRegex(RecipeError, "reserved"):
            self.request("inspect", archive_id=archive_id)

    def test_mcp_stage_binds_a_direct_download_to_its_digest_named_inbox_member(self):
        class FakeMCPServer:
            def __init__(self, *_args, **_kwargs):
                pass

            def tool(self, **_metadata):
                return lambda function: function

        mcp = types.ModuleType("mcp")
        mcp_server_package = types.ModuleType("mcp.server")
        mcp_server_module = types.ModuleType("mcp.server.mcpserver")
        mcp_server_module.MCPServer = FakeMCPServer
        spec = importlib.util.spec_from_file_location(
            "managed_recipe_pack_mcp_test", CORE / "mcp_server.py"
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "mcp": mcp,
            "mcp.server": mcp_server_package,
            "mcp.server.mcpserver": mcp_server_module,
        }):
            assert spec.loader is not None
            spec.loader.exec_module(module)

        downloads = self.root / "downloads"
        downloads.mkdir()
        payload = b"synthetic local recipe collection"
        (downloads / "collection (1).zip").write_bytes(payload)
        module.rpc = mock.Mock(return_value={"available": True})
        self.inbox.chmod(0o2770)
        with mock.patch.dict(os.environ, {
            "MEAL_CONCIERGE_RECIPE_PACK_DOWNLOADS": str(downloads),
            "MEAL_CONCIERGE_RECIPE_PACK_INBOX": str(self.inbox),
        }, clear=False):
            staged = module._stage_local_recipe_pack("collection (1).zip")
            os.chmod(self.inbox / (hashlib.sha256(payload).hexdigest() + ".zip"), 0o600)
            repeated = module._stage_local_recipe_pack("collection (1).zip")
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(
            staged, {"staged": True, "archive_id": digest + ".zip", "sha256": digest, "bytes": len(payload)}
        )
        self.assertEqual(repeated, staged)
        self.assertEqual((self.inbox / staged["archive_id"]).read_bytes(), payload)
        staged_info = (self.inbox / staged["archive_id"]).stat()
        self.assertEqual(staged_info.st_mode & 0o777, 0o640)
        self.assertEqual(staged_info.st_gid, self.inbox.stat().st_gid)
        (self.inbox / staged["archive_id"]).write_bytes(b"conflicting staged content")
        with mock.patch.dict(os.environ, {
            "MEAL_CONCIERGE_RECIPE_PACK_DOWNLOADS": str(downloads),
            "MEAL_CONCIERGE_RECIPE_PACK_INBOX": str(self.inbox),
        }, clear=False):
            conflicting = module._stage_local_recipe_pack("collection (1).zip")
        self.assertFalse(conflicting["ok"])
        self.assertIn("conflicting", conflicting["error"])


if __name__ == "__main__":
    unittest.main()
