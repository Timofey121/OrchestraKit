"""Local evidence and route identity boundaries for duplicate-failure detection."""
import tempfile
import unittest
from pathlib import Path

from orchestra_kit.retry import failure_state, repeated_failure


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        (self.project / 'source.py').write_text('value = 0\n')
        (self.project / 'check.log').write_text('AssertionError: expected 3\n')
        self.checks = [{'argv': ['python3', 'test.py'], 'returncode': 1,
                        'failure_cause': 'implementation', 'log': str(self.project / 'check.log')}]

    def test_signature_uses_contents_and_exact_outcomes_not_attempt_log_names(self):
        first = failure_state(self.project, ['source.py'], self.checks)
        self.assertIsNotNone(first)
        (self.project / 'other.log').write_text((self.project / 'check.log').read_text())
        checks = [{**self.checks[0], 'log': str(self.project / 'other.log')}]
        self.assertEqual(first, failure_state(self.project, ['source.py'], checks))
        for key, value in [('argv', ['python3', 'other.py']), ('returncode', 2)]:
            self.assertNotEqual(first, failure_state(self.project, ['source.py'], [{**checks[0], key: value}]))
        (self.project / 'source.py').write_text('value = 1\n')
        self.assertNotEqual(first, failure_state(self.project, ['source.py'], checks))

    def test_passing_environment_precheck_does_not_hide_implementation_stall(self):
        precheck = {**self.checks[0], 'failure_cause': 'environment', 'returncode': 0}
        state = failure_state(self.project, ['source.py'], [precheck, self.checks[0]])
        self.assertIsNotNone(state)
        self.assertNotEqual(state, failure_state(self.project, ['source.py'], self.checks))

    def test_incomplete_or_unsafe_evidence_cannot_stop_a_retry(self):
        for updates in [{'log': str(self.project/'missing.log')}, {'returncode': None},
                        {'output_truncated': True}, {'failure_cause': 'environment'},
                        {'reason': 'timeout'}, {'log': 'check.log'}]:
            with self.subTest(updates=updates):
                self.assertIsNone(failure_state(self.project, ['source.py'], [{**self.checks[0], **updates}]))
        self.assertIsNone(failure_state(self.project, [], self.checks))
        self.assertIsNone(failure_state(self.project, ['source.py'], []))
        self.assertIsNone(failure_state(self.project, ['source.py'], [{**self.checks[0], 'returncode': 0}]))
        (self.project/'link.log').symlink_to(self.project/'check.log')
        self.assertIsNone(failure_state(self.project, ['source.py'], [{**self.checks[0], 'log': str(self.project/'link.log') }]))

    def test_two_failed_states_and_same_provider_model_effort_are_required(self):
        route = {'model': 'model-a', 'effort': 'medium', 'provider': None, 'profile': 'cheap'}
        attempts = [{'number': n, 'requested_model': 'model-a', 'requested_effort': 'medium',
                     'requested_provider': None, 'retry_state': 'same'} for n in (1, 2)]
        self.assertIsNotNone(repeated_failure(attempts, route, 'same'))
        self.assertIsNone(repeated_failure(attempts[:1], route, 'same'))
        self.assertIsNone(repeated_failure(attempts, route, None))
        self.assertIsNone(repeated_failure(attempts, route, 'new'))
        for key, value in [('model', 'model-b'), ('effort', 'high'), ('provider', 'other')]:
            with self.subTest(key=key):
                self.assertIsNone(repeated_failure(attempts, {**route, key: value}, 'same'))

    def test_missing_large_or_changed_logs_abstain_or_change_the_state(self):
        first = failure_state(self.project, ['source.py'], self.checks)
        (self.project/'check.log').write_text('different error\n')
        self.assertNotEqual(first, failure_state(self.project, ['source.py'], self.checks))
        (self.project/'check.log').write_text('x' * 1_000_001)
        self.assertIsNone(failure_state(self.project, ['source.py'], self.checks))
