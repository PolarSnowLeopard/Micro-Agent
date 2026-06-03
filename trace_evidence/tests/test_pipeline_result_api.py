"""Tests for PipelineResult programmatic API (to_dict, to_json, save_to_dir, evidence_events)."""
import json
import os
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from trace_evidence import run_pipeline

TRACE_PATH = os.path.join(
    os.path.dirname(__file__), 'fixtures', 'minimal_v1_trace.json'
)


class TestPipelineResultAPI(unittest.TestCase):
    """Test the programmatic consumer API on PipelineResult."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_pipeline(TRACE_PATH)

    def test_evidence_events_has_event_type(self):
        """Every evidence event must have an event_type field."""
        events = self.result.evidence_events
        self.assertGreater(len(events), 0)
        valid_types = {'tool_call', 'service', 'planner_thought', 'phase', 'iteration', 'verification', 'completion'}
        for ev in events:
            self.assertIn('event_type', ev, f"Missing event_type in {list(ev.keys())[:5]}")
            self.assertIn(ev['event_type'], valid_types)

    def test_evidence_events_count_matches_bundle(self):
        """evidence_events length should match sum of bundle collections."""
        bundle = self.result.bundle
        expected = (
            len(bundle.tool_calls) + len(bundle.services)
            + len(bundle.planner_thoughts) + len(bundle.phases)
            + len(bundle.iterations)
            + (1 if bundle.verification else 0)
            + (1 if bundle.completion else 0)
        )
        self.assertEqual(len(self.result.evidence_events), expected)

    def test_to_dict_structure(self):
        """to_dict returns a well-structured dict."""
        d = self.result.to_dict()
        self.assertIsInstance(d, dict)
        for key in ('evidence_id', 'session_id', 'card', 'report', 'evidence_events'):
            self.assertIn(key, d, f"Missing key: {key}")
        self.assertIsInstance(d['card'], dict)
        self.assertIsInstance(d['report'], dict)
        self.assertIsInstance(d['evidence_events'], list)
        self.assertEqual(d['evidence_id'], self.result.card.evidence_id)
        self.assertEqual(d['session_id'], self.result.card.session_id)

    def test_to_json_valid(self):
        """to_json produces valid JSON that round-trips."""
        j = self.result.to_json()
        self.assertIsInstance(j, str)
        parsed = json.loads(j)
        self.assertEqual(parsed['evidence_id'], self.result.card.evidence_id)

    def test_to_json_indent(self):
        """to_json accepts indent parameter."""
        compact = self.result.to_json(indent=None)
        pretty = self.result.to_json(indent=2)
        # Pretty version should be longer due to whitespace
        self.assertGreater(len(pretty), len(compact))

    def test_save_to_dir_creates_files(self):
        """save_to_dir creates all expected artifact files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = self.result.save_to_dir(tmpdir)
            self.assertIsInstance(files, dict)
            expected_keys = {'card_md', 'card_json', 'report_md', 'report_json', 'config_draft', 'pipeline_result'}
            self.assertEqual(set(files.keys()), expected_keys)
            for name, path in files.items():
                self.assertTrue(os.path.exists(path), f"{name} file missing: {path}")
                self.assertGreater(os.path.getsize(path), 0, f"{name} file empty")

    def test_save_to_dir_json_files_valid(self):
        """All JSON files from save_to_dir are parseable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files = self.result.save_to_dir(tmpdir)
            for name in ('card_json', 'report_json', 'config_draft', 'pipeline_result'):
                with open(files[name], 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.assertIsInstance(data, dict, f"{name} did not parse to dict")

    def test_save_to_dir_creates_subdir(self):
        """save_to_dir creates the output directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = os.path.join(tmpdir, 'sub', 'dir')
            files = self.result.save_to_dir(nested)
            self.assertTrue(os.path.isdir(nested))
            self.assertGreater(len(files), 0)


if __name__ == '__main__':
    unittest.main()
