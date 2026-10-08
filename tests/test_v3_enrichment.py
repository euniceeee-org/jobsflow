"""Synthetic V3 acceptance cases; these do not validate live portal accuracy."""
import copy
import json

import pytest

from tools.fresh_24h.cross_source_enrich import (
    Enricher, Limits, evidence_status, location_verdict, match_vacancy,
    screen_job, source_allowed,
)


def listing(**changes):
    row = {
        "company": "Example Holdings", "title": "Operations Graduate",
        "location": "Hong Kong", "url": "https://www.linkedin.com/jobs/view/12345678",
        "source": "linkedin", "requisition_id": "REQ-1", "department": "Operations",
        "employment_type": "FULL_TIME", "work_arrangement": "onsite",
        "posted_at": "2026-10-01", "teaser": "Support project coordination.",
    }
    row.update(changes)
    return row


def complete(**changes):
    row = listing(
        url="https://careers.example.test/jobs/REQ-1", source="employer_official_careers",
        description=("Responsibilities: coordinate projects and communicate with stakeholders. "
                     "Maintain operational reports and support process improvement. "
                     "Requirements: bachelor's degree. Fresh graduates welcome. "
                     "Fluent English required; Cantonese preferred. Hong Kong work authorization required."),
        fields={"responsibilities": "Coordinate projects and maintain reports.",
                "education": "Bachelor's degree", "required_years_experience": "Fresh graduates welcome"},
    )
    row.update(changes)
    return row


class Provider:
    """Deterministic fake: no network, browsers, cookies, tracker or models."""
    def __init__(self, candidates=(), fail=False):
        self.candidates = list(candidates)
        self.fail = fail
        self.calls = []

    def search(self, original, source, *, timeout, limit):
        self.calls.append(("search", source))
        if self.fail:
            raise TimeoutError("synthetic timeout")
        return [c for c in self.candidates if c["source"] == source][:limit]

    def fetch(self, candidate, *, timeout):
        self.calls.append(("fetch", candidate["url"]))
        if self.fail:
            raise TimeoutError("synthetic timeout")
        return copy.deepcopy(candidate)


@pytest.mark.parametrize("value", ["Hong Kong SAR", "Central and Western District", "Ngau Tau Kok", "Kwai Chung", "Tai Po", "Kowloon", "Sha Tin", "Tsim Sha Tsui", "Quarry Bay", "Tseung Kwan O", "香港", "葵涌"])
def test_hk_locations(value):
    assert location_verdict(value) == "hong_kong"


@pytest.mark.parametrize("value", ["Düsseldorf", "Singapore", "Mesa AZ", "London, UK", "Shenzhen", "New York"])
def test_overseas(value):
    assert screen_job(listing(location=value))["decision"] == "exclude"


@pytest.mark.parametrize("value", ["", "Central", "Remote", "Hong Kong / Singapore", "Hong Kong (Hybrid)", "Global", "Unknown City"])
def test_ambiguous_location(value):
    assert screen_job(listing(location=value))["decision"] == "needs_manual_review"


@pytest.mark.parametrize("title", ["Insurance Sales Consultant", "Insurance Agent", "Wealth Management Trainee", "Family Wealth Management Sales", "Commission-only Financial Adviser"])
def test_explicit_sales(title):
    assert screen_job(listing(title=title))["decision"] == "exclude"


def test_insurer_is_not_blacklisted():
    job = complete(company="AXA", title="Corporate Communications Assistant")
    assert screen_job(job, verified_text=job["description"])["eligibility"] != "ineligible"


def test_unverified_teaser_does_not_prove_sales():
    job = listing(title="Business Operations Assistant", teaser="Sell insurance products for commission")
    assert screen_job(job)["decision"] != "exclude"
    assert screen_job(job, verified_text=job["teaser"])["decision"] == "exclude"


def test_negative_sales_statement_and_sales_support_are_not_rejected():
    job = listing(title="Insurance Sales Support Assistant")
    assert screen_job(job, verified_text="No selling insurance products is required; maintain administrative records.")["decision"] != "exclude"


@pytest.mark.parametrize("title", ["Associate / Senior Associate", "Consultant / Senior Consultant", "Analyst / Senior Analyst", "Consultant or Senior Consultant"])
def test_mixed_seniority(title):
    assert screen_job(listing(title=title))["decision"] == "needs_manual_review"


@pytest.mark.parametrize("title", ["Director", "Associate Director", "AVP Risk", "Vice President", "Senior Manager", "Head of Operations"])
def test_senior_only(title):
    assert screen_job(listing(title=title))["decision"] == "exclude"


@pytest.mark.parametrize("title", ["Associate", "Consultant", "Assistant Manager", "Management Trainee", "Graduate Trainee"])
def test_no_blanket_manager_or_associate_exclusion(title):
    assert screen_job(listing(title=title))["decision"] != "exclude"


def test_unknown_candidate_requirements_need_review():
    job = complete()
    job["fields"]["graduation_year_eligibility"] = "Graduating in 2027 only"
    assert screen_job(job, verified_text=job["description"])["decision"] == "needs_manual_review"


def test_freehire_name_url_and_spoofed_host_exclusion():
    assert not source_allowed("freehire", "https://example.test/job/1")
    assert not source_allowed("linkedin", "https://freehire.hk/job/1")
    assert not source_allowed("linkedin", "https://linkedin.com.evil.test/job/1")


def test_same_vacancy_requires_employer_title_location_and_identity():
    assert match_vacancy(listing(), complete())["verdict"] == "verified_same_job"
    assert match_vacancy(listing(requisition_id=""), complete(requisition_id=""))["verdict"] == "possible_match_review"


@pytest.mark.parametrize("changes", [{"company": "Different Employer"}, {"title": "Marketing Graduate"}, {"location": "Singapore"}, {"department": "Legal"}, {"requisition_id": "REQ-2"}, {"employment_type": "INTERNSHIP"}])
def test_mismatch_never_replaces_original(changes, tmp_path):
    original = listing()
    before = copy.deepcopy(original)
    result = Enricher(Provider([complete(**changes)]), cache_dir=tmp_path).enrich(original)
    assert result["status"] != "verified_cross_source"
    assert result["scoring_text"] == original["teaser"]
    assert original == before


def test_recruiter_identity_and_location_are_not_inferred():
    assert match_vacancy(listing(employer_disclosed=False), complete())["verdict"] != "verified_same_job"
    assert match_vacancy(listing(location=""), complete())["verdict"] != "verified_same_job"


def test_corroboration_without_reference_and_possible_repost():
    original = listing(requisition_id="", fields={"responsibilities": "Coordinate project reports"})
    candidate = complete(requisition_id="", fields={"responsibilities": "Coordinate project reports", "education": "Degree"})
    assert match_vacancy(original, candidate)["verdict"] == "verified_same_job"
    assert match_vacancy(original, dict(candidate, posted_at="2026-01-01"))["verdict"] == "possible_repost_review"


def test_provenance_cache_and_no_original_mutation(tmp_path):
    original = listing()
    provider = Provider([complete()])
    enricher = Enricher(provider, cache_dir=tmp_path)
    result = enricher.enrich(original)
    assert result["status"] == "verified_cross_source"
    field = result["field_provenance"]["education"][0]
    assert field["url"] == complete()["url"]
    assert field["source_host"] == "careers.example.test"
    assert field["retrieved_at"]
    assert result["original"] == original
    assert "description" not in original
    calls = len(provider.calls)
    assert enricher.enrich(original)["cache_hit"] is True
    assert len(provider.calls) == calls
    assert list(tmp_path.glob("*.json"))


def test_conflicting_verified_sources_report_both(tmp_path):
    other = complete(source="ctgoodjobs", url="https://www.ctgoodjobs.hk/job/999", fields={"responsibilities": "Coordinate projects", "education": "Master's degree only"})
    result = Enricher(Provider([complete(), other]), cache_dir=tmp_path).enrich(listing())
    assert result["status"] == "needs_manual_review"
    assert result["conflicts"]
    assert len(result["field_provenance"]["education"]) == 2
    assert result["scoring_text"] == listing()["teaser"]


def test_failures_and_limits_are_bounded(tmp_path):
    provider = Provider(fail=True)
    result = Enricher(provider, cache_dir=tmp_path, limits=Limits(max_searches=2, max_pages=1)).enrich(listing())
    assert len(provider.calls) == 2
    assert result["status"] == "needs_manual_review"
    assert len(result["errors"]) == 2
    assert result["scoring_text"] == listing()["teaser"]


def test_freehire_never_enters_enrichment(tmp_path):
    provider = Provider([complete(source="freehire", url="https://freehire.hk/jobs/1")])
    result = Enricher(provider, cache_dir=tmp_path).enrich(listing(source="freehire"))
    assert not provider.calls
    assert result["status"] == "needs_manual_review"
    assert result["scoring_text"] == ""


def test_missing_jd_is_not_unsuitable_or_verified():
    assert evidence_status(listing()) != "verified_original"
    assert screen_job(listing())["decision"] != "exclude"


def test_complete_original_and_partial_cross_source(tmp_path):
    assert evidence_status(complete()) == "verified_original"
    result = Enricher(Provider([complete(description="A short original-vacancy snippet", fields={"education": "Degree"})]), cache_dir=tmp_path).enrich(listing())
    assert result["status"] == "partial_verified"


def test_cache_invalidates_changed_input_and_bad_timestamp(tmp_path):
    provider = Provider([complete()])
    engine = Enricher(provider, cache_dir=tmp_path)
    engine.enrich(listing())
    engine.enrich(listing(department="Legal"))
    assert len(list(tmp_path.glob("*.json"))) == 2
    for path in tmp_path.glob("*.json"):
        data = json.loads(path.read_text())
        data["cached_at"] = "invalid"
        path.write_text(json.dumps(data))
    before = len(provider.calls)
    engine.enrich(listing())
    assert len(provider.calls) > before
