"""V3 integration regressions using fake structured providers only."""
import copy
import json

from tools.fresh_24h import fresh_24h_scan as scan, two_pass_score as scorer
from tools.fresh_24h.cross_source_enrich import Enricher, Limits
from tools.fresh_24h.v3_sources import PublicProvider, parse_job_postings
from tools.workflow.adapters.push import _standard_entry_rows
from tests.test_v3_enrichment import Provider, complete, listing
from scripts.v3_fixture_pilot import evaluate, FIXTURE_PATH


PROFILE = {"core_keywords": ["operations", "project"], "adjacent_keywords": ["support"],
           "evidence_keywords": ["coordination"], "preferred_industry_keywords": ["technology"],
           "track_mapping": {"A": "Operations"}}


def test_public_v3_integrates_before_pass2_without_legacy_fetch(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy browser/detail fetch must not run")
    monkeypatch.setattr(scorer, "_run_deep_enrich", forbidden)
    monkeypatch.setattr(scorer, "fetch_linkedin_details_batch", forbidden)
    hits = [listing(), listing(source="freehire", url="https://freehire.hk/job/1"), listing(title="Senior Manager", url="https://www.linkedin.com/jobs/view/22222222")]
    raw = copy.deepcopy(hits)
    rows, meta = scorer.run_two_pass(hits, repo=tmp_path, profile=PROFILE, max_deep=2, sleep_s=0,
        drop_below_final=False, v3_config={"enabled": True}, v3_provider=Provider([complete()]))
    enriched = next(row for row in rows if row["职位"] == "Operations Graduate")
    assert enriched["v3_evidence_status"] == "verified_cross_source"
    assert enriched["JD深度"] == "full"
    assert enriched["v3_decision"] == "needs_manual_review"  # undeclared language/work authorization
    assert all("初评分数" in row and "深评分数" in row for row in rows)
    assert meta["v3"]["decisions"]["exclude"] == 1
    assert len(meta["prohibited_source_rows"]) == 1
    evidence = json.loads(next((tmp_path / "JobSearch_2026/02_Tracker/v3_evidence").glob("preview-*.json")).read_text())
    assert evidence["original_scan"] in raw
    cache_dir = tmp_path / "JobSearch_2026/02_Tracker/jds/cache"
    before = {p.name: p.read_bytes() for p in cache_dir.glob("*.json")}
    scorer._persist_deep_jds(rows, tmp_path)
    assert {p.name: p.read_bytes() for p in cache_dir.glob("*.json")} == before
    selected, rejected = _standard_entry_rows(rows, final_gate=1.0)
    assert not selected
    assert len(rejected) == len(rows)


def test_score_cannot_override_v3_gate():
    rows = [{"JD深度": "full", "深评分数": "5.0", "v3_decision": decision, "评估状态": "ready"} for decision in ("exclude", "needs_manual_review", "priority_a")]
    accepted, rejected = _standard_entry_rows(rows, final_gate=3.3)
    assert [r["v3_decision"] for r in accepted] == ["priority_a"]
    assert len(rejected) == 2
    retained, _ = scorer.select_rows_for_retention(rows, final_gate=3.3)
    assert [r["v3_decision"] for r in retained] == ["needs_manual_review", "priority_a"]


def test_all_entry_policies_preserve_v3_review_boundary():
    from tools.workflow.adapters.push import _entry_preview
    import pytest
    with pytest.raises(ValueError, match="v3_eligibility_review_required"):
        _entry_preview(workspace=None, title="synthetic", run={}, rows=[{"v3_decision": "exclude"}], store=None, entry_policy="all")


def test_synthetic_pilot_is_read_only_and_matches_ground_truth(tmp_path):
    raw = FIXTURE_PATH.read_bytes()
    result = evaluate(json.loads(raw)["cases"], tmp_path)
    assert FIXTURE_PATH.read_bytes() == raw
    assert result["sample_size"] == 15
    assert result["metrics"]["initially_incomplete"] == 14
    assert result["metrics"]["incorrect_verified_matches_in_fixture"] == 0
    assert result["live_network_requests"] == 0


def test_v3_review_only_has_no_requests(tmp_path):
    provider = Provider([complete()])
    rows, meta = scorer.run_two_pass([listing()], repo=tmp_path, profile=PROFILE,
        defer_deep=True, v3_config={"enabled": True}, v3_provider=provider)
    assert not provider.calls
    assert rows[0]["v3_decision"] == "needs_manual_review"
    assert meta["v3"]["requests"] == {"searches": 0, "pages": 0}


def test_old_freehire_config_cannot_request_search(monkeypatch, tmp_path):
    cfg = {"setup_required": False, "portals": {"freehire": {"enabled": True, "cli": "fake.ts"}},
           "queries": [{"id": f"q{i}", "terms": {"freehire": "graduate", "linkedin": "graduate"}} for i in range(9)]}
    queries = tmp_path / "queries.json"
    queries.write_text(json.dumps(cfg))
    tracker = tmp_path / "JobSearch_2026/02_Tracker"
    tracker.mkdir(parents=True)
    csv = tracker / "hk_apply_list_2026-10-08.csv"
    csv.write_text("岗位编号,职位,公司,链接\n")
    monkeypatch.setattr(scan, "run_portal_requests", lambda *a, **k: (_ for _ in ()).throw(AssertionError("FreeHire request")))
    assert scan.main(["--repo", str(tmp_path), "--queries", str(queries), "--tracker", str(csv), "--no-record"]) == 0
    assert json.loads(queries.read_text()) == cfg


def test_missing_location_never_defaults_hk_and_raw_card_survives(tmp_path):
    card = {"title": "Graduate", "url": "https://www.linkedin.com/jobs/view/12345678", "teaser": "x" * 800, "requisition_id": "R1"}
    hit = scan.card_to_hit(card, source="linkedin", query_id="q", track_hint="A")
    assert hit.location == ""
    assert hit.raw_listing == card
    scan.write_candidates_csv(tmp_path / "raw.csv", [hit])
    assert json.loads(scorer.load_hits(tmp_path / "raw.csv")[0]["raw_listing"]) == card


def test_freehire_direct_enrichment_is_prohibited(monkeypatch, tmp_path):
    monkeypatch.setattr(scorer, "_load_cache", lambda *a: (_ for _ in ()).throw(AssertionError("cache consulted")))
    text, depth = scorer.deep_enrich_hit(listing(source="freehire"), repo=tmp_path)
    assert text == ""
    assert depth == "teaser_unavailable"


def test_original_fetch_shares_page_budget_and_full_jd_status(tmp_path):
    provider = Provider()
    result = Enricher(provider, cache_dir=tmp_path, limits=Limits(max_pages=1)).enrich(listing(), try_original=True)
    assert result["requests"]["pages"] == 1
    assert result["requests"]["searches"] == 0
    assert len(provider.calls) == 1
    result = Enricher(Provider(), cache_dir=tmp_path).enrich(complete(), try_original=True)
    assert result["status"] == "verified_original"


def test_public_jsonld_does_not_copy_missing_identity_from_seed():
    body = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "Graduate", "description": "Coordinate projects", "identifier": {"value": "REQ1"}, "jobLocation": {"address": {"addressLocality": "Ngau Tau Kok", "addressCountry": "HK"}}}) + '</script>'
    rows = parse_job_postings(body, url="https://careers.example.test/jobs/1", source="employer_official_careers")
    assert rows[0]["company"] == ""
    assert rows[0]["requisition_id"] == "REQ1"
    assert rows[0]["location"] == "Ngau Tau Kok, HK"


def test_public_provider_never_calls_cdp_for_jobsdb(monkeypatch, tmp_path):
    provider = PublicProvider(tmp_path, {})
    monkeypatch.setattr(provider, "_validate_url", lambda *a: "hk.jobsdb.com")
    monkeypatch.setattr(provider, "_get", lambda *a: (_ for _ in ()).throw(AssertionError("JobsDB page request")))
    row = provider.fetch(listing(source="jobsdb", url="https://hk.jobsdb.com/job/12345678"), timeout=25)
    assert not row["description"]


def test_official_host_registration_and_robots_fail_closed(monkeypatch, tmp_path):
    provider = PublicProvider(tmp_path, {"official_employers": {"Example Holdings": {"hosts": ["careers.example.test"]}}})
    try:
        provider._validate_url("https://unregistered.test/jobs/1", "employer_official_careers", "Example Holdings")
    except ValueError as exc:
        assert "registered" in str(exc)
    else:
        raise AssertionError("unknown official host accepted")
    monkeypatch.setattr(provider, "_validate_url", lambda *a: "careers.example.test")
    calls = []
    def robots(url, timeout):
        calls.append(url)
        return "User-agent: *\nDisallow: /"
    monkeypatch.setattr(provider, "_get", robots)
    import pytest
    with pytest.raises(PermissionError):
        provider.fetch(complete(), timeout=25)
    assert len(calls) == 1
