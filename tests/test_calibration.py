from __future__ import annotations

import importlib
import unittest


class CalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calibrate = importlib.import_module("orchestra_kit.calibration").calibrate_receipts

    def sample(self, profile="cheap", status="verified", fixture="fixture-a", tokens=100,
               complete=True, quality=True, attempts=1, final_profile=None) -> dict:
        rows = []
        for number in range(1, attempts + 1):
            rows.append({"number": number, "profile": profile, "requested_model": f"{profile}-model",
                         "requested_effort": "medium", "usage": {
                             "complete": complete, "totals": {"input_tokens": tokens - 1,
                             "cached_input_tokens": None, "output_tokens": 1, "uncached_input_tokens": None,
                             "total_tokens": tokens}, "files": [], "models": []}})
        if final_profile and attempts > 1:
            rows[-1].update(profile=final_profile, requested_model=f"{final_profile}-model")
        return {"fixture_fingerprint": fixture,
                "receipt": {"status": status, "profile": final_profile or profile, "requested_model": f"{final_profile or profile}-model",
                            "requested_effort": "medium", "attempts": rows,
                            "usage": {"complete": complete, "totals": {"input_tokens": (tokens - 1) * attempts,
                            "cached_input_tokens": None, "output_tokens": attempts, "uncached_input_tokens": None,
                            "total_tokens": tokens * attempts}, "models": [], "files": [], "warnings": []}},
                "quality": {"checks_passed": quality, "acceptance_passed": quality,
                            "review_verdict": "PASS" if quality else "FAIL"},
                "elapsed_seconds": 1.5}

    def test_accounts_for_all_samples_and_recommends_lowest_complete_token_profile_after_quality_gate(self) -> None:
        samples = [self.sample("cheap", tokens=90), self.sample("strong", tokens=50)]
        result = self.calibrate(samples, ["cheap", "strong"])
        self.assertEqual(result["recommendation"]["profile"], "strong")
        self.assertEqual(result["profiles"]["cheap"]["sample_count"], 1)
        self.assertEqual(result["profiles"]["strong"]["quality_pass_rate"], 1.0)
        self.assertFalse(result["automatic_config_rewrite"])
        self.assertFalse(result["root_model_switch"])
        self.assertEqual(result["profiles"]["strong"]["requested_settings"]["model"], "strong-model")
        self.assertEqual(result["profiles"]["strong"]["requested_settings"]["attested"], False)

    def test_failed_samples_and_all_attempts_reduce_quality_rate_and_unknown_counters_remain_null(self) -> None:
        failed = self.sample("cheap", status="failed", quality=False, attempts=2)
        unknown = self.sample("strong", complete=False, tokens=80)
        result = self.calibrate([failed, unknown], ["cheap", "strong"])
        self.assertEqual(result["profiles"]["cheap"]["sample_count"], 1)
        self.assertEqual(result["profiles"]["cheap"]["attempt_count"], 2)
        self.assertEqual(result["profiles"]["cheap"]["quality_pass_rate"], 0.0)
        self.assertIsNone(result["profiles"]["strong"]["observed_total_tokens"])
        self.assertIsNone(result["recommendation"])

    def test_requires_same_fixture_set_for_each_profile_and_actual_receipt_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixture"):
            self.calibrate([self.sample("cheap", fixture="a"), self.sample("strong", fixture="b")], ["cheap", "strong"])
        bad = self.sample()
        del bad["receipt"]["attempts"][0]["requested_model"]
        with self.assertRaisesRegex(ValueError, "attempt"):
            self.calibrate([bad], ["cheap"])

    def test_groups_escalated_receipts_by_initial_attempt_and_rejects_unequal_fixture_multiplicity(self) -> None:
        escalated = self.sample("cheap", attempts=2, final_profile="strong")
        peer = self.sample("strong")
        result = self.calibrate([escalated, peer], ["cheap", "strong"])
        self.assertEqual(result["profiles"]["cheap"]["retries"], 1)
        self.assertEqual(result["profiles"]["cheap"]["attempt_count"], 2)
        self.assertEqual(result["profiles"]["cheap"]["requested_settings"]["model"], "cheap-model")
        with self.assertRaisesRegex(ValueError, "multiset"):
            self.calibrate([self.sample("cheap"), self.sample("cheap"), self.sample("strong")], ["cheap", "strong"])

    def test_real_usage_shape_and_contradictory_complete_aggregate_are_handled(self) -> None:
        result = self.calibrate([self.sample("cheap")], ["cheap"])
        self.assertEqual(result["profiles"]["cheap"]["input_tokens"], 99)
        contradictory = self.sample("cheap")
        contradictory["receipt"]["usage"]["totals"]["total_tokens"] = 999
        contradictory["receipt"]["usage"]["totals"]["input_tokens"] = 998
        with self.assertRaisesRegex(ValueError, "contradicts"):
            self.calibrate([contradictory], ["cheap"])

    def test_internally_inconsistent_complete_usage_is_rejected(self) -> None:
        for changes in ({"total_tokens": 1}, {"cached_input_tokens": 100},
                        {"cached_input_tokens": 20, "uncached_input_tokens": 1}):
            with self.subTest(changes=changes):
                sample = self.sample()
                for usage in [sample["receipt"]["usage"], sample["receipt"]["attempts"][0]["usage"]]:
                    usage["totals"].update(changes)
                with self.assertRaisesRegex(ValueError, "inconsistent"):
                    self.calibrate([sample], ["cheap"])

    def test_complete_usage_requires_observed_input_output_and_total(self) -> None:
        for counter in ("input_tokens", "output_tokens", "total_tokens"):
            with self.subTest(counter=counter):
                sample = self.sample()
                for usage in [sample["receipt"]["usage"], sample["receipt"]["attempts"][0]["usage"]]:
                    usage["totals"][counter] = None
                with self.assertRaisesRegex(ValueError, "complete usage requires"):
                    self.calibrate([sample], ["cheap"])
