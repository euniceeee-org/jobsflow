#!/usr/bin/env python3
"""15-job synthetic dry run. No live network, private data or tracker writes."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.fresh_24h.cross_source_enrich import Enricher, evidence_status, fields_of, screen_job
from tools.io_utils import atomic_write_json

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests/fixtures/v3/jobs.json"


class FixtureProvider:
    def __init__(self, case):
        self.case = case
        self.calls = 0

    def search(self, original, source, *, timeout, limit):
        self.calls += 1
        if self.case.get("simulate_failure"):
            raise TimeoutError("synthetic upstream failure")
        return [copy.deepcopy(row) for row in self.case["candidates"] if row["source"] == source][:limit]

    def fetch(self, candidate, *, timeout):
        self.calls += 1
        return copy.deepcopy(candidate)


def evaluate(fixtures, cache_dir):
    rows = []
    metrics = Counter()
    for index, case in enumerate(fixtures):
        original = case["original"]
        before = screen_job(original, verified_text=original.get("description", ""))
        metrics["initially_incomplete"] += evidence_status(original) != "verified_original"
        provider = FixtureProvider(case)
        if before["decision"] == "exclude":
            result = {"status": evidence_status(original), "matches": [], "field_provenance": {}, "errors": [], "conflicts": [], "scoring_text": original.get("description", "")}
            after = before
        else:
            result = Enricher(provider, cache_dir=cache_dir / str(index)).enrich(original)
            verified = result["status"] in {"verified_original", "verified_cross_source"}
            after = screen_job(original, verified_text=result["scoring_text"] if verified else "")
            if result["conflicts"] or not verified:
                after["decision"] = "needs_manual_review"
                after["reasons"].append("jd_evidence_requires_review")
        metrics["successfully_enriched"] += result["status"] == "verified_cross_source"
        matches = result["matches"]
        metrics["jobs_with_verified_same_job_matches"] += any(m["verdict"] == "verified_same_job" for m in matches)
        metrics["verified_same_job_matches"] += sum(m["verdict"] == "verified_same_job" for m in matches)
        metrics["rejected_or_conflicting_matches"] += sum(m["verdict"] in {"rejected", "conflicting_sources", "market_reference_only"} for m in matches)
        labels = {row["url"]: row.get("fixture_same_job", False) for row in case["candidates"]}
        metrics["incorrect_verified_matches_in_fixture"] += sum(m["verdict"] == "verified_same_job" and not labels.get(m["url"], False) for m in matches)
        metrics["remaining_uncertain"] += after["decision"] == "needs_manual_review"
        metrics["classification_changes"] += after["decision"] != before["decision"]
        metrics["simulated_provider_calls"] += provider.calls
        metrics["simulated_failures"] += len(result["errors"])
        metrics["critical_fields_recovered"] += sum(field in result["field_provenance"] and not fields_of(original).get(field) for field in ("education", "required_years_experience", "graduation_year_eligibility", "student_status_requirement", "hong_kong_work_authorization"))
        rows.append({"fixture_id": case["id"], "original": original, "before": before, "after": after,
                     "evidence_status": result["status"], "matches": matches, "field_provenance": result["field_provenance"],
                     "conflicts": result["conflicts"], "errors": result["errors"],
                     "outstanding_unknowns": result.get("outstanding_unknowns", [])})
    return {"dataset": "synthetic fixtures; no production accuracy claim", "sample_size": len(fixtures),
            "metrics": dict(metrics), "live_network_requests": 0,
            "before_classifications": dict(Counter(row["before"]["decision"] for row in rows)),
            "after_classifications": dict(Counter(row["after"]["decision"] for row in rows)), "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw = FIXTURE_PATH.read_bytes()
    with tempfile.TemporaryDirectory(prefix="jobsflow-v3-pilot-") as directory:
        result = evaluate(json.loads(raw)["cases"], Path(directory))
    assert FIXTURE_PATH.read_bytes() == raw, "fixture input was modified"
    result["input_sha256"] = hashlib.sha256(raw).hexdigest()
    atomic_write_json(args.out, result)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
