from __future__ import annotations

import importlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

from orchestra_kit.config import load_config


KIT = Path(__file__).resolve().parents[1]


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / '.orchestra').mkdir()
        self.path = self.project / '.orchestra/project.toml'
        self.path.write_text((KIT / 'templates/project.toml').read_text())
        self.config = load_config(self.project, KIT)
        self.assertIsNotNone(importlib.util.find_spec('orchestra_kit.routing'), 'routing feature is missing')
        self.routing = importlib.import_module('orchestra_kit.routing')

    def route(self, **kwargs):
        return self.routing.route_task(self.config, role='worker', **kwargs)

    def test_small_simple_work_stays_with_root_and_has_a_light_route(self) -> None:
        result = self.route(complexity='simple', risk='low', size='small')
        self.assertEqual((result['execution'], result['profile'], result['effort']), ('root', 'cheap', 'medium'))
        self.assertEqual(result['verification'], 'light')
        self.assertEqual(result['review_levels'], ['final'])

    def test_complex_independent_work_uses_a_stronger_named_leaf(self) -> None:
        result = self.route(complexity='complex', risk='medium', independent=True)
        self.assertEqual(result['execution'], 'leaf')
        self.assertEqual(result['profile'], 'strong')
        self.assertEqual(result['agent_type'], 'orchestra_worker__strong')
        self.assertEqual(result['verification'], 'focused')

    def test_critical_risk_raises_profile_even_for_a_simple_task(self) -> None:
        result = self.route(complexity='simple', risk='critical')
        self.assertEqual(result['profile'], 'critical')
        self.assertEqual(result['verification'], 'full')

    def test_reviewer_never_drops_below_its_configured_profile(self) -> None:
        result = self.routing.route_task(self.config, role='reviewer', complexity='simple', risk='low')
        self.assertEqual(result['profile'], 'strong')
        self.assertEqual(result['sandbox'], 'read-only')

    def test_strict_mode_preserves_all_review_gates_and_full_verification(self) -> None:
        self.path.write_text(self.path.read_text().replace('mode = "adaptive"', 'mode = "strict"').replace('review_levels = ["final"]', 'review_levels = ["leaf", "final"]'))
        self.config = load_config(self.project, KIT)
        result = self.route(complexity='simple', risk='low')
        self.assertEqual(result['review_levels'], ['leaf', 'final'])
        self.assertEqual(result['verification'], 'full')

    def test_missing_context_is_gathered_without_model_escalation(self) -> None:
        result = self.route(complexity='simple', risk='low', failure='context', previous_profile='cheap')
        self.assertEqual((result['action'], result['profile']), ('gather-context', 'cheap'))

    def test_implementation_failure_repairs_once_then_escalates(self) -> None:
        first = self.route(complexity='simple', risk='low', failure='implementation', previous_profile='cheap', attempt=0)
        second = self.route(complexity='simple', risk='low', failure='implementation', previous_profile='cheap', attempt=1)
        self.assertEqual((first['action'], first['profile']), ('repair', 'cheap'))
        self.assertEqual((second['action'], second['profile']), ('escalate', 'balanced'))

    def test_capability_failure_returns_to_root_without_stronger_model(self) -> None:
        unavailable = self.route(failure='capability', previous_profile='balanced', independent=True)
        stopped = self.route(failure='environment', previous_profile='balanced')
        self.assertEqual((unavailable['action'], unavailable['execution'], unavailable['profile']),
                         ('keep-root', 'root', 'balanced'))
        self.assertEqual(stopped['action'], 'stop')

    def test_exhausted_repair_budget_never_launches_another_attempt(self) -> None:
        result = self.route(failure='implementation', previous_profile='cheap', attempt=2)
        self.assertEqual(result['action'], 'stop')

    def test_provider_without_required_capability_is_skipped(self) -> None:
        self.path.write_text(self.path.read_text().replace('[profiles.cheap]', '[providers.limited]\nname = "Limited"\nbase_url = "https://example.com"\ncapabilities = ["function"]\n\n[profiles.cheap]\nprovider = "limited"'))
        self.config = load_config(self.project, KIT)
        result = self.route(complexity='simple', risk='low', required_capabilities=['apply_patch'])
        self.assertEqual(result['profile'], 'balanced')

    def test_disabled_routing_keeps_the_role_profile(self) -> None:
        self.path.write_text(self.path.read_text() + '\n[routing]\nenabled = false\n')
        self.config = load_config(self.project, KIT)
        result = self.route(complexity='critical', risk='critical')
        self.assertEqual(result['profile'], 'balanced')

    def test_disabled_routing_never_recommends_an_ungenerated_variant(self) -> None:
        text = self.path.read_text().replace('[profiles.balanced]', '[providers.limited]\nname = "Limited"\nbase_url = "https://example.com"\ncapabilities = ["function"]\n\n[profiles.balanced]\nprovider = "limited"')
        self.path.write_text(text + '\n[routing]\nenabled = false\n')
        self.config = load_config(self.project, KIT)
        result = self.route(required_capabilities=['apply_patch'], independent=True)
        self.assertEqual(result['action'], 'keep-root')
        self.assertIsNone(result['agent_type'])

    def test_blank_or_scalar_brief_sections_are_rejected(self) -> None:
        for invalid in ['   ', True, 4]:
            data = {name: 'valid' for name in self.routing.BRIEF_SECTIONS}
            data['SCOPE'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.routing.build_brief(self.config, data)

    def test_invalid_facts_are_rejected_before_routing(self) -> None:
        for values in [{'risk': 'unknown'}, {'attempt': -1}, {'attempt': True}, {'previous_profile': 'missing'}, {'failure': 'guess'}, {'independent': 'yes'}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.route(**values)

    def test_brief_preserves_all_six_sections_and_refuses_missing_scope(self) -> None:
        data = {'GOAL': 'Fix login', 'SOURCES OF TRUTH': ['src/login.py'], 'SCOPE': {'allowed': ['src/login.py'], 'forbidden': ['legacy/']}, 'ACCEPTANCE CRITERIA': ['Valid credentials work'], 'VERIFICATION': ['python3 -m unittest'], 'CONTEXT': {'known': 'Failure reproduced'}}
        brief = self.routing.build_brief(self.config, data)
        self.assertIn('legacy/', brief)
        self.assertIn('Valid credentials work', brief)
        del data['SCOPE']
        with self.assertRaisesRegex(ValueError, 'SCOPE'):
            self.routing.build_brief(self.config, data)

    def test_oversized_brief_is_refused_without_dropping_constraints(self) -> None:
        data = {name: 'x' for name in ['GOAL', 'SOURCES OF TRUTH', 'SCOPE', 'ACCEPTANCE CRITERIA', 'VERIFICATION', 'CONTEXT']}
        data['CONTEXT'] = 'x' * 12001
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.routing.build_brief(self.config, data)
