"""Compare observed leaf receipts without rewriting routing configuration."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any, Iterable


_TERMINAL = frozenset({"verified", "failed", "cancelled"})
_COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "uncached_input_tokens", "total_tokens")


def _usage(value: Any, location: str) -> tuple[bool, dict[str, int | None]]:
    """Read the stable subset of summarize_usage output; absent evidence is unknown."""
    if value is None:
        return False, {name: None for name in _COUNTERS}
    if not isinstance(value, dict) or not {"complete", "totals"} <= set(value):
        raise ValueError(f"{location} usage must contain complete and totals")
    if type(value["complete"]) is not bool or not isinstance(value["totals"], dict):
        raise ValueError(f"{location} usage is invalid")
    totals = {}
    for name in _COUNTERS:
        number = value["totals"].get(name)
        if number is not None and (type(number) is not int or number < 0):
            raise ValueError(f"{location} {name} is invalid")
        totals[name] = number
    if not value["complete"]:
        return False, {name: None for name in _COUNTERS}
    inp, out, total = (totals[name] for name in ("input_tokens", "output_tokens", "total_tokens"))
    if inp is None or out is None or total is None:
        raise ValueError(f"{location} complete usage requires input, output and total counters")
    if total != inp + out:
        raise ValueError(f"{location} inconsistent total_tokens")
    cached, uncached = totals["cached_input_tokens"], totals["uncached_input_tokens"]
    if cached is not None and cached > inp or uncached is not None and uncached > inp:
        raise ValueError(f"{location} inconsistent input subset")
    if cached is not None and uncached is not None and cached + uncached != inp:
        raise ValueError(f"{location} inconsistent cached and uncached input")
    return True, totals


def _settings(value: Any, location: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    model, effort = value.get("requested_model"), value.get("requested_effort")
    if not isinstance(model, str) or not model or not isinstance(effort, str) or not effort:
        raise ValueError(f"{location} requires requested model and effort")
    return model, effort


def _sum_known(rows: list[dict[str, int | None]]) -> dict[str, int | None]:
    return {name: None if any(row[name] is None for row in rows) else sum(row[name] for row in rows) for name in _COUNTERS}


def _sample(value: Any, configured: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"fixture_fingerprint", "receipt", "quality", "elapsed_seconds"}:
        raise ValueError("sample contains unsupported or missing fields")
    try:
        fixture = json.dumps(value["fixture_fingerprint"], ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("fixture_fingerprint must be serializable") from exc
    receipt = value["receipt"]
    if not isinstance(receipt, dict) or receipt.get("status") not in _TERMINAL:
        raise ValueError("receipt must have a terminal status")
    receipt_profile = receipt.get("profile")
    if not isinstance(receipt_profile, str) or receipt_profile not in configured:
        raise ValueError("receipt profile must be configured")
    _settings(receipt, "receipt")
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("receipt requires attempts")
    parsed = []
    for expected, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict) or attempt.get("number") != expected:
            raise ValueError("attempt numbers must be consecutive")
        profile = attempt.get("profile")
        if not isinstance(profile, str) or profile not in configured:
            raise ValueError("attempt profile must be configured")
        attempt_model, attempt_effort = _settings(attempt, "attempt")
        complete, totals = _usage(attempt.get("usage"), "attempt")
        parsed.append({"profile": profile, "model": attempt_model, "effort": attempt_effort,
                       "complete": complete, "totals": totals})
    receipt_complete, receipt_totals = _usage(receipt.get("usage"), "receipt")
    attempts_complete = all(item["complete"] for item in parsed)
    attempt_totals = _sum_known([item["totals"] for item in parsed])
    if receipt_complete and attempts_complete:
        for name in _COUNTERS:
            if attempt_totals[name] is not None and receipt_totals[name] is not None and attempt_totals[name] != receipt_totals[name]:
                raise ValueError(f"receipt usage contradicts attempts for {name}")
    quality = value["quality"]
    if not isinstance(quality, dict) or set(quality) != {"checks_passed", "acceptance_passed", "review_verdict"}:
        raise ValueError("quality contains unsupported or missing fields")
    if type(quality["checks_passed"]) is not bool or type(quality["acceptance_passed"]) is not bool or quality["review_verdict"] not in {"PASS", "FAIL"}:
        raise ValueError("quality is invalid")
    elapsed = value["elapsed_seconds"]
    if elapsed is not None and (type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0):
        raise ValueError("elapsed_seconds must be a finite nonnegative number or null")
    passed = receipt["status"] == "verified" and quality["checks_passed"] and quality["acceptance_passed"] and quality["review_verdict"] == "PASS"
    return {"profile": parsed[0]["profile"], "fixture": fixture,
            "model": parsed[0]["model"], "effort": parsed[0]["effort"],
            "attempts": parsed, "attempt_totals": attempt_totals, "attempts_complete": attempts_complete,
            "passed": passed, "elapsed_seconds": elapsed}


def _metric(values: list[int | float | None]) -> int | float | None:
    return None if any(value is None for value in values) else sum(values)


def calibrate_receipts(samples: list[dict], configured_profiles: Iterable[str]) -> dict:
    """Account for every run and issue a non-binding, evidence-limited recommendation."""
    if not isinstance(samples, list) or not samples:
        raise ValueError("samples must be a nonempty list")
    if isinstance(configured_profiles, (str, bytes)):
        raise ValueError("configured_profiles must be an iterable of profile names")
    profiles = tuple(configured_profiles)
    if not profiles or len(set(profiles)) != len(profiles) or not all(isinstance(name, str) and name for name in profiles):
        raise ValueError("configured_profiles must contain unique nonempty names")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in (_sample(sample, set(profiles)) for sample in samples):
        grouped[row["profile"]].append(row)
    if set(grouped) != set(profiles):
        raise ValueError("each configured profile must cover the same fixture multiset")
    fixture_sets = {name: Counter(row["fixture"] for row in rows) for name, rows in grouped.items()}
    if len({tuple(sorted(value.items())) for value in fixture_sets.values()}) != 1:
        raise ValueError("each configured profile must cover the same fixture multiset")
    report: dict[str, dict[str, Any]] = {}
    eligible: list[tuple[float, str]] = []
    for name in profiles:
        rows = grouped[name]
        attempts = [attempt for row in rows for attempt in row["attempts"]]
        complete = all(row["attempts_complete"] for row in rows)
        totals = _sum_known([row["attempt_totals"] for row in rows])
        passed = sum(1 for row in rows if row["passed"])
        rate = passed / len(rows)
        settings = {(row["model"], row["effort"]) for row in rows}
        requested = next(iter(settings)) if len(settings) == 1 else (None, None)
        report[name] = {"sample_count": len(rows), "attempt_count": len(attempts),
                        "fixture_fingerprints": sorted(fixture_sets[name].elements()),
                        "quality_pass_count": passed, "quality_pass_rate": rate,
                        "all_attempt_usage_complete": complete, "elapsed_seconds": _metric([row["elapsed_seconds"] for row in rows]),
                        "input_tokens": totals["input_tokens"], "cached_input_tokens": totals["cached_input_tokens"],
                        "uncached_input_tokens": totals["uncached_input_tokens"], "output_tokens": totals["output_tokens"],
                        "observed_total_tokens": totals["total_tokens"], "retries": len(attempts) - len(rows),
                        "requested_settings": {"model": requested[0], "effort": requested[1], "attested": False}}
        if rate == 1.0 and complete and totals["total_tokens"] is not None:
            eligible.append((totals["total_tokens"] / len(rows), name))
    eligible.sort(key=lambda item: (item[0], profiles.index(item[1])))
    recommendation = None if not eligible else {"profile": eligible[0][1], "mean_observed_tokens": eligible[0][0],
                                                 "basis": "quality pass rate 1.0 and complete attempt usage; lowest comparable mean observed tokens"}
    return {"profiles": report, "recommendation": recommendation,
            "automatic_config_rewrite": False, "root_model_switch": False,
            "model_attestation": "requested settings are receipt metadata, not runtime attestation"}


def _names(value: Any, location: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{location} must be an iterable of labels")
    try:
        names = tuple(value)
    except TypeError as exc:
        raise ValueError(f"{location} must be an iterable of labels") from exc
    if not names or not all(isinstance(name, str) and name for name in names) or len(set(names)) != len(names):
        raise ValueError(f"{location} must contain unique nonempty labels")
    return names


def _comparison_metrics(report: dict[str, Any], profiles: tuple[str, ...]) -> dict[str, Any]:
    rows = [report["profiles"][name] for name in profiles]
    counters = {name: _metric([row["observed_total_tokens" if name == "total_tokens" else name] for row in rows])
                for name in _COUNTERS}
    return {"sample_count": sum(row["sample_count"] for row in rows),
            "attempt_count": sum(row["attempt_count"] for row in rows),
            "quality_pass_count": sum(row["quality_pass_count"] for row in rows),
            "retries": sum(row["retries"] for row in rows),
            "elapsed_seconds": _metric([row["elapsed_seconds"] for row in rows]),
            "all_attempt_usage_complete": all(row["all_attempt_usage_complete"] for row in rows),
            **counters}


def _initial_settings(rows: list[dict[str, Any]], profiles: tuple[str, ...], variant: str) -> dict[str, tuple[str, str]]:
    settings: dict[str, tuple[str, str]] = {}
    for profile in profiles:
        found = {(row["model"], row["effort"]) for row in rows if row["profile"] == profile}
        if len(found) != 1:
            raise ValueError(f"variant {variant} must use one initial requested settings pair per profile")
        settings[profile] = next(iter(found))
    return settings


def compare_workflows(samples: list[dict], configured_profiles: Iterable[str], variants: Iterable[str], *, baseline: str) -> dict:
    """Compare finite workflow variants with equal fixtures and initial routing settings.

    This is descriptive corpus accounting.  It cannot establish a statistical
    guarantee about future runs, and it never estimates currency costs.
    """
    if not isinstance(samples, list) or not samples:
        raise ValueError("samples must be a nonempty list")
    profiles = _names(configured_profiles, "configured_profiles")
    labels = _names(variants, "variants")
    if set(labels) & set(profiles):
        raise ValueError("variant labels must not be configured profile names")
    if not isinstance(baseline, str) or baseline not in labels:
        raise ValueError("baseline must be a configured variant label")

    configured = set(profiles)
    wrapped: dict[str, list[dict[str, Any]]] = {label: [] for label in labels}
    raw_samples: dict[str, list[dict[str, Any]]] = {label: [] for label in labels}
    for wrapper in samples:
        if not isinstance(wrapper, dict) or set(wrapper) != {"variant", "sample"}:
            raise ValueError("workflow sample must contain only variant and sample")
        label = wrapper["variant"]
        if not isinstance(label, str) or label not in wrapped:
            raise ValueError("workflow sample variant must be configured")
        if not isinstance(wrapper["sample"], dict):
            raise ValueError("workflow sample must contain a calibration sample")
        wrapped[label].append(wrapper["sample"])
        raw_samples[label].append(_sample(wrapper["sample"], configured))
    if any(not rows for rows in wrapped.values()):
        raise ValueError("each variant must contain samples")

    reports: dict[str, dict[str, Any]] = {}
    initial_settings: dict[str, dict[str, tuple[str, str]]] = {}
    fixtures: dict[str, dict[str, Counter[str]]] = {}
    pass_counts: dict[str, dict[str, Counter[str]]] = {}
    for label in labels:
        report = calibrate_receipts(wrapped[label], profiles)
        reports[label] = {**report, "phase_breakdown": {
            "available": False,
            "reason": "workflow samples contain no validated runtime events for phase accounting"}}
        initial_settings[label] = _initial_settings(raw_samples[label], profiles, label)
        fixtures[label] = {profile: Counter(row["fixture"] for row in raw_samples[label] if row["profile"] == profile)
                           for profile in profiles}
        pass_counts[label] = {profile: Counter(row["fixture"] for row in raw_samples[label]
                                               if row["profile"] == profile and row["passed"])
                              for profile in profiles}

    for label in labels:
        for profile in profiles:
            if fixtures[label][profile] != fixtures[baseline][profile]:
                raise ValueError("each variant and profile must cover the same fixture multiset")
            if initial_settings[label][profile] != initial_settings[baseline][profile]:
                raise ValueError("initial requested settings must match between variants for each profile")

    baseline_metrics = _comparison_metrics(reports[baseline], profiles)
    comparisons: dict[str, dict[str, Any]] = {}
    eligible: list[tuple[int, str]] = []
    for candidate in labels:
        if candidate == baseline:
            continue
        candidate_metrics = _comparison_metrics(reports[candidate], profiles)
        fixture_rows: dict[str, list[dict[str, Any]]] = {}
        regressions: list[dict[str, Any]] = []
        for profile in profiles:
            fixture_rows[profile] = []
            for fixture in sorted(fixtures[baseline][profile]):
                baseline_passes = pass_counts[baseline][profile][fixture]
                candidate_passes = pass_counts[candidate][profile][fixture]
                item = {"fixture_fingerprint": fixture, "baseline_pass_count": baseline_passes,
                        "candidate_pass_count": candidate_passes,
                        "quality_regression": candidate_passes < baseline_passes}
                fixture_rows[profile].append(item)
                if item["quality_regression"]:
                    regressions.append({"profile": profile, **item})
        quality_preserved = not regressions
        usage_unknown = (not baseline_metrics["all_attempt_usage_complete"] or
                         not candidate_metrics["all_attempt_usage_complete"] or
                         any(baseline_metrics[name] is None or candidate_metrics[name] is None for name in _COUNTERS))
        baseline_total = baseline_metrics["total_tokens"]
        candidate_total = candidate_metrics["total_tokens"]
        delta = None if baseline_total is None or candidate_total is None else candidate_total - baseline_total
        percent = None if delta is None or baseline_total == 0 else 100 * delta / baseline_total
        all_candidate_passes = candidate_metrics["quality_pass_count"] == candidate_metrics["sample_count"]
        comparison = {"baseline": baseline, "candidate": candidate,
                      "baseline_metrics": baseline_metrics, "candidate_metrics": candidate_metrics,
                      "per_profile_fixture": fixture_rows, "quality_regressions": regressions,
                      "quality_preserved": quality_preserved, "all_candidate_passes": all_candidate_passes,
                      "usage_unknown": usage_unknown,
                      "baseline_observed_total_tokens": baseline_total,
                      "candidate_observed_total_tokens": candidate_total,
                      "observed_total_tokens_delta": delta,
                      "observed_total_tokens_percent_change": percent,
                      "savings_declared": False}
        if quality_preserved and all_candidate_passes and not usage_unknown and delta is not None and delta < 0:
            comparison["savings_declared"] = True
            eligible.append((candidate_total, candidate))
        comparisons[candidate] = comparison
    eligible.sort(key=lambda item: (item[0], labels.index(item[1])))
    recommendation = None if not eligible else {
        "variant": eligible[0][1], "baseline": baseline,
        "basis": "all candidate samples passed, no per-profile fixture quality regression, and complete observed usage showed lower total tokens",
        "non_binding": True}
    return {"baseline": baseline, "variants": reports, "comparisons": comparisons,
            "recommendation": recommendation, "automatic_config_rewrite": False,
            "root_model_switch": False,
            "comparison_scope": "descriptive finite-corpus comparison; no statistical guarantee"}
