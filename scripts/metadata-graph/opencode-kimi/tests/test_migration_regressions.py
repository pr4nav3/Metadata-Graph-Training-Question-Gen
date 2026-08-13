from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import global_supervisor  # noqa: E402
from global_supervisor import extract_json_object  # noqa: E402
from run_frontier_batch import has_pending_review_rows  # noqa: E402


class MigrationRegressionTests(unittest.TestCase):
    def test_nested_json_fragment_is_not_a_pending_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.jsonl"
            path.write_text('{"doc_id":"nested-supporting-doc"}\n', encoding="utf-8")
            self.assertFalse(has_pending_review_rows(path))

    def test_valid_pending_question_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.jsonl"
            path.write_text(
                '{"id":"run_q01","manual_review":{"status":"pending"}}\n',
                encoding="utf-8",
            )
            self.assertTrue(has_pending_review_rows(path))

    def test_snapshot_counts_only_rows_with_question_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "malformed.jsonl").write_text(
                '{"doc_id":"nested-supporting-doc"}\n'
                '{"id":"run_q01","manual_review":{"status":"pending"}}\n',
                encoding="utf-8",
            )
            with patch.object(global_supervisor, "QUESTIONS_DIR", root):
                state = global_supervisor.collect_pending_questions()
            self.assertEqual(state["totals"]["rows"], 1)
            self.assertEqual(state["totals"]["pending"], 1)

    def test_supervisor_parser_skips_incidental_json(self) -> None:
        output = (
            'Reasoning mentions {"max_reviews": 1} before the decision.\n'
            '{"action":"review_questions","reason":"clear backlog",'
            '"params":{"max_reviews":1}}'
        )
        decision = extract_json_object(output)
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "review_questions")


if __name__ == "__main__":
    unittest.main()
