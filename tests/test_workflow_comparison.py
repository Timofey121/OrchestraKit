import importlib
import unittest


class WorkflowComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compare = importlib.import_module("orchestra_kit.calibration").compare_workflows

    def sample(self, profile="cheap", fixture="fixture-a", *, passed=True, status="verified",
               tokens=100, complete=True, attempts=1, model=None, effort="medium"):
        model = model or f"{profile}-model"
        input_tokens = max(tokens - 1, 0)
        output_tokens = tokens - input_tokens
        cached_tokens = min(10, input_tokens)
        uncached_tokens = input_tokens - cached_tokens
        attempt_rows = []
        for number in range(1, attempts + 1):
            attempt_rows.append({"number": number, "profile": profile,
                                 "requested_model": model, "requested_effort": effort,
                                 "usage": {"complete": complete, "totals": {
                                     "input_tokens": input_tokens, "cached_input_tokens": cached_tokens,
                                     "uncached_input_tokens": uncached_tokens, "output_tokens": output_tokens,
                                     "total_tokens": tokens}, "files": [], "models": []}})
        return {"fixture_fingerprint": fixture,
                "receipt": {"status": status, "profile": profile,
                            "requested_model": model, "requested_effort": effort,
                            "attempts": attempt_rows,
                            "usage": {"complete": complete, "totals": {
                                "input_tokens": input_tokens * attempts,
                                "cached_input_tokens": cached_tokens * attempts,
                                "uncached_input_tokens": uncached_tokens * attempts,
                                "output_tokens": output_tokens * attempts, "total_tokens": tokens * attempts},
                                      "files": [], "models": [], "warnings": []}},
                "quality": {"checks_passed": passed, "acceptance_passed": passed,
                            "review_verdict": "PASS" if passed else "FAIL"},
                "elapsed_seconds": 1.5}

    def rows(self, variant, *samples):
        return [{"variant": variant, "sample": item} for item in samples]

    def paired(self, baseline_samples, candidate_samples):
        return self.compare(self.rows("old", *baseline_samples) + self.rows("new", *candidate_samples),
                            ["cheap", "strong"], ["old", "new"], baseline="old")

    def test_reports_variants_and_recommends_true_complete_improvement(self):
        old = [self.sample("cheap", tokens=100), self.sample("strong", tokens=200)]
        new = [self.sample("cheap", tokens=50), self.sample("strong", tokens=100)]
        result = self.paired(old, new)
        comparison = result["comparisons"]["new"]
        self.assertTrue(comparison["quality_preserved"])
        self.assertEqual(comparison["baseline_observed_total_tokens"], 300)
        self.assertEqual(comparison["candidate_observed_total_tokens"], 150)
        self.assertEqual(comparison["observed_total_tokens_delta"], -150)
        self.assertEqual(comparison["observed_total_tokens_percent_change"], -50.0)
        self.assertEqual(result["recommendation"]["variant"], "new")
        self.assertFalse(result["variants"]["new"]["phase_breakdown"]["available"])

    def test_quality_comparison_is_per_profile_and_fixture_not_overall_rate(self):
        old = [self.sample("cheap", "a", passed=True), self.sample("cheap", "b", passed=False),
               self.sample("strong", "a", passed=True), self.sample("strong", "b", passed=True)]
        new = [self.sample("cheap", "a", passed=False), self.sample("cheap", "b", passed=True),
               self.sample("strong", "a", passed=True), self.sample("strong", "b", passed=True)]
        result = self.paired(old, new)
        comparison = result["comparisons"]["new"]
        self.assertEqual(result["variants"]["old"]["profiles"]["cheap"]["quality_pass_rate"],
                         result["variants"]["new"]["profiles"]["cheap"]["quality_pass_rate"])
        self.assertFalse(comparison["quality_preserved"])
        self.assertEqual(comparison["quality_regressions"][0]["fixture_fingerprint"], '"a"')
        self.assertIsNone(result["recommendation"])

    def test_counts_failures_retries_and_unknown_usage_without_savings_claim(self):
        old = [self.sample("cheap", attempts=2, status="failed", passed=False),
               self.sample("strong", complete=False)]
        new = [self.sample("cheap", attempts=3, status="failed", passed=False),
               self.sample("strong", complete=False)]
        result = self.paired(old, new)
        self.assertEqual(result["variants"]["new"]["profiles"]["cheap"]["attempt_count"], 3)
        self.assertEqual(result["variants"]["new"]["profiles"]["cheap"]["retries"], 2)
        self.assertTrue(result["comparisons"]["new"]["usage_unknown"])
        self.assertIsNone(result["comparisons"]["new"]["observed_total_tokens_delta"])
        self.assertIsNone(result["recommendation"])

    def test_baseline_zero_has_null_percentage(self):
        old = [self.sample("cheap", tokens=0), self.sample("strong", tokens=0)]
        new = [self.sample("cheap", tokens=1), self.sample("strong", tokens=1)]
        result = self.paired(old, new)
        self.assertEqual(result["comparisons"]["new"]["observed_total_tokens_delta"], 2)
        self.assertIsNone(result["comparisons"]["new"]["observed_total_tokens_percent_change"])

    def test_escalation_model_does_not_change_initial_setting_comparison(self):
        old = [self.sample("cheap"), self.sample("strong")]
        escalated = self.sample("cheap", attempts=2)
        escalated["receipt"]["attempts"][1].update(profile="strong", requested_model="repair-model")
        escalated["receipt"].update(profile="strong", requested_model="repair-model")
        result = self.paired(old, [escalated, self.sample("strong")])
        self.assertTrue(result["comparisons"]["new"]["quality_preserved"])
        self.assertEqual(result["variants"]["new"]["profiles"]["cheap"]["attempt_count"], 2)

    def test_rejects_invalid_labels_fixture_sets_and_changed_initial_settings(self):
        old = [self.sample("cheap"), self.sample("strong")]
        new = [self.sample("cheap"), self.sample("strong")]
        with self.assertRaisesRegex(ValueError, "unique nonempty"):
            self.compare(self.rows("old", *old), ["cheap", "strong"], ["old", "old"], baseline="old")
        with self.assertRaisesRegex(ValueError, "variant labels"):
            self.compare(self.rows("cheap", *old), ["cheap", "strong"], ["cheap"], baseline="cheap")
        with self.assertRaisesRegex(ValueError, "fixture multiset"):
            self.paired(old, [self.sample("cheap", fixture="other"), self.sample("strong")])
        changed = [self.sample("cheap", model="different"), self.sample("strong")]
        with self.assertRaisesRegex(ValueError, "initial requested settings"):
            self.paired(old, changed)
        changed_effort = [self.sample("cheap", effort="high"), self.sample("strong")]
        with self.assertRaisesRegex(ValueError, "initial requested settings"):
            self.paired(old, changed_effort)
