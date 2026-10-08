"""Small V3 seam for the existing two-pass scorer and artifact format."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from tools.fresh_24h.cross_source_enrich import Enricher, Limits, Provider, screen_job
from tools.fresh_24h.v3_sources import PublicProvider
from tools.io_utils import atomic_write_json


class V3Run:
    def __init__(self, repo: Path, config: dict[str, Any], provider: Provider | None = None):
        self.config = config
        root = Path(repo)
        if root.name != "JobSearch_2026":
            root /= "JobSearch_2026"
        self.evidence_dir = root / "02_Tracker" / "v3_evidence"
        limits = Limits(**{k: v for k, v in (config.get("limits") or {}).items() if k in Limits.__dataclass_fields__})
        self.engine = Enricher(provider or PublicProvider(Path(__file__).resolve().parents[2], config), cache_dir=self.evidence_dir / "cache", limits=limits)
        self.records: dict[str, dict[str, Any]] = {}

    def original(self, hit: dict[str, Any]) -> dict[str, Any]:
        # Snapshot before scoring attaches objects and private internal keys.
        original = copy.deepcopy({k: v for k, v in hit.items() if not k.startswith("_")})
        raw = original.get("raw_listing") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        if isinstance(raw, dict):
            # Only fields actually supplied by the portal; never reuse a
            # platform numeric ID as an employer requisition ID.
            for field in ("requisition_id", "department", "employment_type", "work_arrangement", "employer_disclosed"):
                if field in raw and field not in original:
                    original[field] = raw[field]
        return original

    def register(self, hit: dict[str, Any]) -> None:
        original = self.original(hit)
        hit["_v3_original"] = original
        hit["_v3_screen"] = screen_job(original)

    def enrich(self, hit: dict[str, Any], *, cached_text: str | None = None, cache_meta: dict[str, Any] | None = None, allow_requests: bool = True) -> tuple[str, str]:
        original = copy.deepcopy(hit["_v3_original"])
        # Reuse valid original cache reads without altering the shared cache.
        # Ignore known prohibited-source or cross-source entries.
        cache_source = str((cache_meta or {}).get("source") or "").casefold()
        if cached_text and "freehire" not in cache_source and "cross_source" not in cache_source:
            original["description"] = cached_text
        result = self.engine.enrich(original, allow_requests=allow_requests, try_original=True)
        result["original_scan"] = hit["_v3_original"]
        verified = result["status"] in {"verified_original", "verified_cross_source"}
        screening_job = dict(original)
        if result.get("original_detail"):
            screening_job = {**screening_job, **result["original_detail"]}
        if result.get("field_provenance"):
            screening_job["fields"] = {field: entries[0]["value"] for field, entries in result["field_provenance"].items() if len({entry["value"] for entry in entries}) == 1}
        verified_excerpts = "\n".join(entry["value"] for entries in result.get("field_provenance", {}).values() for entry in entries)
        screen = screen_job(screening_job, verified_text=result["scoring_text"] if verified else verified_excerpts, candidate=self.config.get("candidate_confirmations"))
        if result["conflicts"] or not verified:
            if screen["decision"] != "exclude":
                screen["decision"], screen["eligibility"] = "needs_manual_review", "uncertain"
            screen["reasons"].append("jd_evidence_requires_review")
        hit["_v3_screen"] = screen
        result["screening"] = screen
        self.records[str(hit["_preview_key"])] = result
        hit["_v3_evidence"] = result
        if verified:
            hit["_v3_scoring_jd"] = result["scoring_text"]
            hit["_enrich"] = {"mode": "v3_public_evidence", "ok": True, "depth": "full", "evidence_status": result["status"]}
            return result["scoring_text"], "deep"
        return str(hit.get("teaser") or ""), "teaser_fallback"

    def decorate(self, rows: list[dict], hits: list[dict], meta: dict[str, Any]) -> None:
        hits = [hit for hit in hits if "_v3_original" in hit]
        by_key = {str(h.get("_preview_key")): h for h in hits}
        for hit in hits:
            key = str(hit.get("_preview_key") or "")
            if key not in self.records:
                self.records[key] = {"original_scan": hit["_v3_original"], "status": "needs_manual_review",
                    "screening": hit["_v3_screen"], "requests": {"searches": 0, "pages": 0}, "errors": [], "field_provenance": {}, "conflicts": []}
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            key = str(row.get("_preview_key") or "")
            row_hit = by_key.get(key)
            if not row_hit:
                continue
            record = self.records[key]
            screen = record["screening"]
            row.update(v3_decision=screen["decision"], v3_eligibility=screen["eligibility"],
                       v3_evidence_status=record["status"], v3_reasons=";".join(screen["reasons"]),
                       v3_evidence_path=str(self.evidence_dir / f"{key}.json"))
        for key, record in self.records.items():
            hit = by_key[key]
            record["original_scores"] = {
                label: {"score": getattr(hit.get(attr), "score", None), "reason": getattr(hit.get(attr), "reason", "")}
                for label, attr in (("pass1", "_sc1"), ("pass2", "_sc2"))
            }
            atomic_write_json(self.evidence_dir / f"{key}.json", record)
        meta["v3"] = {"enabled": True, "public_only": True, "evidence_dir": str(self.evidence_dir),
            "decisions": {decision: sum(r["screening"]["decision"] == decision for r in self.records.values()) for decision in ("priority_a", "priority_b", "needs_manual_review", "exclude")},
            "evidence_statuses": {status: sum(r["status"] == status for r in self.records.values()) for status in ("verified_original", "verified_cross_source", "partial_verified", "market_reference_only", "needs_manual_review")},
            "requests": {field: sum(r["requests"][field] for r in self.records.values()) for field in ("searches", "pages")}}


def config_fingerprint(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
