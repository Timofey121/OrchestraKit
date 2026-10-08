from __future__ import annotations

import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        (self.project / '.orchestra').mkdir(parents=True)
        self.config_path = self.project / '.orchestra/project.toml'
        self.config_path.write_text((KIT / 'templates/project.toml').read_text())
        self.assertIsNotNone(importlib.util.find_spec('orchestra_kit.state'), 'durable task state is missing')
        self.state = importlib.import_module('orchestra_kit.state')

    def start(self):
        return self.state.start_task(self.project, KIT, 'Implement a bounded feature')

    def evidence(self, status='completed'):
        return {'status': status, 'summary': 'Feature checked', 'completed_steps': ['Implementation'], 'next_steps': [], 'changed_files': ['src/app.py'], 'checks': [{'command': 'python3 -m unittest', 'outcome': 'pass', 'evidence': 'tests pass'}], 'acceptance': [{'criterion': 'Requested behavior works', 'satisfied': True, 'evidence': 'Focused regression check'}], 'reviews': [{'level': 'final', 'verdict': 'PASS', 'evidence': 'Independent final review'}], 'metrics': {'input_tokens': 20, 'output_tokens': 10, 'elapsed_seconds': 1.5, 'cost_usd': 0.001}}

    def record(self, run, update, revision=0):
        return self.state.record_task(self.project, KIT, run['task_id'], update, expected_revision=revision)

    def test_separate_chats_get_separate_runs_and_no_current_task_pointer(self) -> None:
        first, second = self.start(), self.start()
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertEqual(first['revision'], 0)
        self.assertEqual(len(self.state.list_tasks(self.project)), 2)
        self.assertFalse((self.project / '.orchestra/current-task.json').exists())

    def test_task_chat_binding_is_explicit_validated_and_preserved(self) -> None:
        chat_id = '01a10742-1409-7882-a0f7-ca5ec3c8325f'
        run = self.state.start_task(self.project, KIT, 'Build dashboard', chat_id=chat_id)
        self.assertEqual(run['chat_id'], chat_id)
        self.record(run, self.evidence('running'))
        self.assertEqual(self.state.show_task(self.project, run['task_id'])['chat_id'], chat_id)
        self.assertIsNone(self.start().get('chat_id'))
        before = len(self.state.list_tasks(self.project))
        for value in ['../../other', '', True, 'not-a-chat']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.state.start_task(self.project, KIT, 'Invalid binding', chat_id=value)
        self.assertEqual(len(self.state.list_tasks(self.project)), before)

    def test_recorded_evidence_is_durable_and_summary_is_compact(self) -> None:
        run = self.start()
        self.record(run, self.evidence())
        shown = self.state.show_task(self.project, run['task_id'])
        self.assertEqual(shown['status'], 'completed')
        self.assertEqual(shown['revision'], 1)
        self.assertEqual(shown['last_result']['changed_files'], ['src/app.py'])
        self.assertNotIn('history', shown)

    def test_stale_revision_never_overwrites_newer_evidence(self) -> None:
        run = self.start()
        self.record(run, self.evidence('running'))
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.record(run, self.evidence('blocked'))
        self.assertEqual(self.state.show_task(self.project, run['task_id'])['status'], 'running')

    def test_long_task_keeps_latest_state_visible_and_totals_complete(self) -> None:
        from orchestra_kit.dashboard import Dashboard
        run = self.start()
        for revision in range(80):
            update = self.evidence('running')
            update['summary'] = str(revision) + 'я' * 5000
            self.record(run, update, revision=revision)
        path = self.project / '.orchestra/runs' / (run['task_id'] + '.json')
        self.assertLessEqual(path.stat().st_size, 256 * 1024)
        saved = json.loads(path.read_text())
        self.assertEqual(saved['last_result']['summary'], '79' + 'я' * 5000)
        self.assertEqual(saved['revision'], 80)
        self.assertGreater(saved['history_dropped'], 0)
        self.assertEqual(len(saved['history']) + saved['history_dropped'], 80)
        self.assertEqual(saved['metrics']['input_tokens'], 1600)
        warnings = []
        tasks = Dashboard([self.project], Path(self.temp.name) / 'codex', KIT)._task_records(self.project, warnings)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['revision'], 80)
        self.assertFalse(warnings)

    def test_large_single_result_refuses_atomically_with_artifact_hint(self) -> None:
        self.config_path.write_text(self.config_path.read_text().replace('max_result_chars = 6000', 'max_result_chars = 1000000'))
        run = self.start()
        path = self.project / '.orchestra/runs' / (run['task_id'] + '.json')
        before = path.read_bytes()
        update = self.evidence('running')
        update['summary'] = 'я' * 140000
        with self.assertRaisesRegex(ValueError, 'artifacts'):
            self.record(run, update)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_suffix('.lock').exists())

    def test_trimmed_history_does_not_reset_unknown_cumulative_metrics(self) -> None:
        self.config_path.write_text(self.config_path.read_text().replace('max_result_chars = 6000', 'max_result_chars = 1000000'))
        run = self.start()
        update = self.evidence('running')
        update['summary'] = 'x' * 180000
        update['metrics'] = {}
        self.record(run, update)
        saved = json.loads((self.project / '.orchestra/runs' / (run['task_id'] + '.json')).read_text())
        self.assertFalse(saved['history'])
        self.assertEqual(saved['history_dropped'], 1)
        self.record(run, self.evidence('running'), revision=1)
        self.assertIsNone(self.state.show_task(self.project, run['task_id'])['metrics']['input_tokens'])

    def test_completion_requires_checks_acceptance_and_review_evidence(self) -> None:
        for key in ['checks', 'acceptance', 'reviews']:
            with self.subTest(key=key):
                run = self.start()
                update = self.evidence()
                update[key] = []
                with self.assertRaises(ValueError):
                    self.record(run, update)
                self.assertEqual(self.state.show_task(self.project, run['task_id'])['revision'], 0)

    def test_failed_required_check_or_unsatisfied_criterion_blocks_completion(self) -> None:
        for key in ['check', 'criterion', 'review']:
            with self.subTest(key=key):
                run = self.start()
                update = self.evidence()
                if key == 'check':
                    update['checks'][0]['outcome'] = 'fail'
                elif key == 'criterion':
                    update['acceptance'][0]['satisfied'] = False
                else:
                    update['reviews'][0]['verdict'] = 'FAIL'
                with self.assertRaises(ValueError):
                    self.record(run, update)

    def test_policy_tightening_during_load_refuses_completion(self):
        from unittest.mock import patch
        run=self.start();original=self.state.load_config
        def changed(*args):
            loaded=original(*args)
            self.config_path.write_text(self.config_path.read_text().replace('mode = "adaptive"','mode = "strict"').replace('review_levels = ["final"]','review_levels = ["leaf", "final"]'))
            return loaded
        with patch.object(self.state,'load_config',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'configuration changed'):
                self.record(run,self.evidence())
        self.assertEqual(self.state.show_task(self.project,run['task_id'])['status'],'planned')

    def test_strict_completion_requires_leaf_and_final_gates(self) -> None:
        self.config_path.write_text(self.config_path.read_text().replace('mode = "adaptive"', 'mode = "strict"').replace('review_levels = ["final"]', 'review_levels = ["leaf", "final"]'))
        run = self.start()
        with self.assertRaisesRegex(ValueError, 'leaf'):
            self.record(run, self.evidence())

    def test_optional_skipped_check_is_visible_without_inventing_a_pass(self) -> None:
        run = self.start()
        update = self.evidence()
        update['checks'].append({'command': 'optional browser check', 'outcome': 'skipped', 'required': False, 'evidence': 'Unavailable on this host'})
        self.record(run, update)
        self.assertEqual(self.state.show_task(self.project, run['task_id'])['last_result']['checks'][1]['outcome'], 'skipped')

    def test_invalid_or_nonfinite_metrics_are_rejected(self) -> None:
        for value in [-1, True, float('nan'), float('inf')]:
            with self.subTest(value=value):
                run = self.start()
                update = self.evidence('running')
                update['metrics']['elapsed_seconds'] = value
                with self.assertRaises(ValueError):
                    self.record(run, update)

    def test_unknown_cost_remains_unknown_when_summing_attempts(self) -> None:
        run = self.start()
        update = self.evidence('running')
        del update['metrics']['cost_usd']
        self.record(run, update)
        self.record(run, self.evidence(), revision=1)
        shown = self.state.show_task(self.project, run['task_id'])
        self.assertIsNone(shown['metrics']['cost_usd'])
        self.assertEqual(shown['metrics']['input_tokens'], 40)

    def test_completed_run_cannot_be_reopened_by_a_later_writer(self) -> None:
        run = self.start()
        self.record(run, self.evidence())
        with self.assertRaisesRegex(ValueError, 'completed'):
            self.record(run, self.evidence('running'), revision=1)

    def test_unknown_fields_and_oversized_results_are_refused(self) -> None:
        run = self.start()
        update = self.evidence('running')
        update['project_config'] = {'model': 'override'}
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.record(run, update)
        del update['project_config']
        update['summary'] = 'x' * 6001
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.record(run, update)

    def test_task_paths_cannot_escape_project(self) -> None:
        for task_id in ['../outside', '/tmp/outside', 'not-an-id']:
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.state.show_task(self.project, task_id)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.project / '.orchestra/runs').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_lock_prevents_overlapping_update(self) -> None:
        run = self.start()
        lock = self.project / '.orchestra/runs' / (run['task_id'] + '.lock')
        lock.write_text('another writer')
        with self.assertRaisesRegex(ValueError, 'locked'):
            self.record(run, self.evidence())
        self.assertEqual(lock.read_text(), 'another writer')
