"""Bank search, rotation, save intent and source-history policy."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from core import StateStore
from planner import PLANNER_VERSION, plan_week, PlannerError
from recipes import RecipeStore, RecipeError
from recipe_selection import family_history_usage, history_source_index
from service import Application
from test_meal_concierge import CONFIG
from test_meal_concierge_recipe_selection import candidate, request, NoProviderCalls


class BankPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), {**CONFIG, "provider": "meny"})
        self.app = Application(self.store, NoProviderCalls(), object())
        self.bank = self.app.recipes
        self.recipe = candidate(1)["recipe"]
        self.recipe.update(name="Kyllingsuppe")
        self.recipe["source"] = {"kind": "user", "relationship": "user_supplied"}

    def write(self, **args):
        return self.app.handle({"operation": "recipes", **args})

    def test_keyless_intent_replays_across_restart_and_explicit_new_key_creates(self):
        first = self.write(action="save", recipe=self.recipe)
        self.app = Application(self.store, NoProviderCalls(), object())
        again = self.write(action="save", recipe=self.recipe)
        another = self.write(action="save", recipe=self.recipe, idempotency_key="independent")
        self.assertEqual(first["recipe"]["id"], again["recipe"]["id"])
        self.assertTrue(again["recipe"]["idempotent"])
        self.assertNotEqual(another["recipe"]["id"], again["recipe"]["id"])

    def test_archived_discovery_replay_reports_restore_bound_to_current_revision(self):
        ref = self.bank.persist_discovery(candidate(1)["recipe"])["discovery_ref"]
        saved = self.write(action="save", discovery_ref=ref, status="draft")["recipe"]
        self.write(action="archive", recipe_id=saved["id"], expected_revision=1)
        repeat = self.write(action="save", discovery_ref=ref, status="draft")
        self.assertEqual((repeat["saved"], repeat["status"]), (False, "archived"))
        restore = repeat["restore_arguments"]
        self.assertEqual(restore["expected_revision"], 2)
        self.assertEqual(self.write(**restore, idempotency_key="undo")["recipe"]["status"], "draft")
        self.assertTrue(self.write(**restore, idempotency_key="undo")["recipe"]["idempotent"])
        with self.assertRaises(RecipeError):
            self.write(**restore)

    def test_schema_backfill_and_search_follow_edits_and_literal_queries(self):
        saved = self.bank.save(self.recipe)
        with sqlite3.connect(self.bank.path) as db:
            for kind, name in [('TRIGGER', 'recipe_search_' + v) for v in ('insert','update','delete')] + [('TABLE','recipe_search')]:
                db.execute(f'DROP {kind} {name}')
            db.execute("DELETE FROM metadata WHERE key='search_index'")
            db.execute("UPDATE recipes SET search_text='kyllingsuppe'")
        reopened = RecipeStore(self.bank.path, self.bank.household)
        with patch.object(reopened, "_search_text", side_effect=ValueError("interrupted scan")):
            with self.assertRaisesRegex(ValueError, "interrupted"):
                reopened.search("gulrot")
        with sqlite3.connect(self.bank.path) as db:
            self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='search_index'").fetchone())
            self.assertEqual(db.execute("SELECT name FROM recipes").fetchone(), ("Kyllingsuppe",))
        for query in ("kylling suppe", "GULROT", "ＳＵＰＰＥ gulrot"):
            self.assertEqual([r["id"] for r in reopened.search(query)], [saved["id"]])
        self.assertEqual(reopened.search("gulrot OR missing"), [])
        self.assertEqual(reopened.search("%%%"), [])
        with self.assertRaisesRegex(RecipeError, "twelve"):
            reopened.search(" ".join(str(i) for i in range(13)))
        revision = reopened.update(saved["id"], 1, {**self.recipe, "name": "Fuglegryte"})
        self.assertEqual(revision["revision"], 2)
        self.assertEqual(reopened.search("suppe"), [])
        self.assertEqual(reopened.search("fugl gryte")[0]["id"], saved["id"])

    def test_rotated_pages_cover_bank_and_distribute_source_tiers(self):
        for i in range(15):
            doc = candidate(i + 1)["recipe"]
            self.bank.save(doc)
            self.bank.import_pack_record({**doc, "source": self.recipe["source"]}, pack_id="favorite", recipe_id=str(i), version="1",entry_origin="collection")
            self.bank.import_pack_record({**doc, "source": self.recipe["source"]}, pack_id="other", recipe_id=str(i),version="1",entry_origin="collection")
        ranks = {"collection:favorite": 0, "collection:other": 2}
        def page(pivot, offset=0):
            return self.bank.search(limit=6,offset=offset,group_ranks=ranks,rotation_pivot=pivot,status_filter="active")
        start = "rec_" + "0" * 24
        first = page(start)
        self.assertEqual([(r.get("pack") or {}).get("pack_id") for r in first],
                         ["favorite"]*3 + [None]*2 + ["other"])
        self.assertEqual(first,page(start))
        self.assertTrue({r["id"] for r in first}.isdisjoint(r["id"] for r in page(start,6)))
        self.assertNotEqual([r["id"] for r in first], [r["id"] for r in page(first[1]["id"])])


class RankingPolicyTests(unittest.TestCase):
    def test_exact_tie_prefers_saved_and_version_invalidates_old_proposals(self):
        from core import DEFAULT_PROFILE
        saved, remote = candidate(2, saved=True), candidate(1)
        result = plan_week({**request(1), "candidates":[remote["reference"],saved["reference"]]},
                           candidates=[remote,saved], profile=DEFAULT_PROFILE, history={})
        self.assertEqual(result['planner_version'], 'weekly-menu-v7')
        self.assertEqual(result['selection']['slots'][0]['reference'], saved['reference'])
        with self.assertRaisesRegex(PlannerError,"regenerate"):
            Application._resolve_planner_ref(None, {'planner_version':'weekly-menu-v6','input_digest':'a'*64,
                'selection_digest':'c'*64,'request':{}})

    def test_only_retained_disjoint_native_originals_narrow_coverage(self):
        def native(provider, with_identity):
            src={"kind":provider,"publisher":provider.upper(),"relationship":"original"}
            if with_identity:
                src["external_id"]="123"
            return {"source":src,"source_provider":provider}
        def usage(recipe, old):
            idx=history_source_index({"menu":{"dishes":[{"recipe_key":"bank:old",**old}]}}, lambda ref: self.fail())
            return family_history_usage({"recipe_key":"bank:new","recipe":recipe},idx,lambda k:{"eligible":True})
        current=native('oda',True)
        self.assertEqual(usage(current,native('meny',False))['history_coverage'],'complete')
        self.assertEqual(usage(current,native('oda',False))['history_coverage'],'partial')
        self.assertEqual(usage(current,{"source":{"kind":"user"}})['history_coverage'],'partial')
        copied=deepcopy(current)
        copied['source']['original']={'publisher':'unresolved'}
        self.assertEqual(usage(copied,native('meny',False))['history_coverage'],'partial')

    def test_alias_group_keeps_least_complete_history(self):
        from recipe_selection import merge_family_usage
        complete = {"eligible": True, "history_coverage": "complete", "disjoint_history_count": 1}
        partial = {"eligible": True, "history_coverage": "partial", "unresolved_history_count": 1}
        for one, two in [(complete, partial), (partial, complete)]:
            result = merge_family_usage([{"usage": one}, {"usage": two}])
            self.assertEqual((result["history_coverage"], result["unresolved_history_count"]), ("partial", 1))
