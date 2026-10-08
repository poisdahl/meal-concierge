"""Offline checks for honest benchmark outcomes; no native client is launched."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
import household_conversation_probe as journey
from measure_agent_catalog import catalog_metrics


class BenchmarkTests(unittest.TestCase):
    def test_catalog_does_not_infer_model_context(self):
        tool = Mock()
        tool.model_dump.return_value = {'name': 'synthetic', 'description': 'å'}
        result = catalog_metrics([tool])
        self.assertEqual(result['tool_count'], 1)
        self.assertEqual(result['advertised_catalog_chars'], len(json.dumps([tool.model_dump.return_value])))
        self.assertIsNone(result['observed_model_context_tokens'])
        tool.model_dump.assert_called_once_with(by_alias=True, exclude_none=True)

    def test_run_records_pass_only_after_behavioral_assertions(self):
        for failure in (None, AssertionError('synthetic outcome mismatch')):
            with self.subTest(failure=bool(failure)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(journey, 'fixture', return_value=({}, [], [])), \
                     patch.object(journey, 'measure', return_value={'observed_model_context_tokens': None}), \
                     patch.object(journey, 'journeys', side_effect=failure):
                    if failure:
                        with self.assertRaises(AssertionError):
                            journey.run(root)
                    else:
                        journey.run(root)
                context = json.loads((root / 'benchmark-context.json').read_text())
                self.assertEqual(context['tool_loading'], 'unknown')
                self.assertIsNone(context['model'])
                verdict = json.loads((root / 'journey-metrics.jsonl').read_text())
                self.assertEqual(verdict['task_and_safety_assertions'], 'not_completed' if failure else 'passed')
                self.assertNotIn('synthetic outcome mismatch', json.dumps(verdict))


if __name__ == '__main__':
    unittest.main()
