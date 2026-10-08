# Hong Kong early-career screening and cross-source evidence (V3)

V3 is an opt-in layer on the existing scanner and two-pass scorer. It separates
eligibility, career fit and evidence quality. Keyword scores remain unchanged;
overseas, senior-only and explicit product-sales roles cannot become recommended
candidates through a high score. Insurer employers are not blacklisted. Mixed
seniority, ambiguous geography, missing requirements and uncertain identity go
to review. Missing descriptions never imply an unsuitable role.

## Configuration and rollout

The implementation specification JSON is **not** a setup schema. Do not pass it
to `setup.py --schema-proposal` or replace `queries.json` with it. No private
workspace, CV, tracker, credentials or prior scans are bundled with this change.

FreeHire is disabled in newly generated configurations and the neutral template.
The scanner and scorer also reject it when an older configuration still enables
it. Existing query terms and other portal settings are retained. The historical
CLI remains in the repository for compatibility; the product no longer invokes
it as a scan/enrichment/fallback/scoring source.

After reviewing the pilot, explicitly add **only** this optional key to an
existing private `00_Profile/queries.json`, preserving every other key:

```json
{
  "v3_enrichment": {
    "enabled": true,
    "limits": {
      "max_jobs": 15,
      "max_searches": 4,
      "max_pages": 8,
      "timeout": 25,
      "cache_ttl_hours": 168
    },
    "official_employers": {},
    "candidate_confirmations": {}
  }
}
```

This key is off by default. Do not enable broader live rollout until the user
approves it. Limits may be reduced but cannot exceed the listed ceilings. The
original detail attempt shares the eight-page budget; requests are serial (one
in flight, below the two-request concurrency ceiling). Existing scan-depth and
review-first settings still control which jobs receive network work. Cached
original JDs are zero-cost reads. Deferred scans make no enrichment requests.

Official source discovery requires an explicit employer-to-host registration
and candidate posting URLs, for example:

```json
{
  "Example Employer": {
    "hosts": ["careers.example.com"],
    "posting_urls": ["https://careers.example.com/jobs/REQ-123"]
  }
}
```

Place this mapping inside `official_employers`. Register actual verified career
hosts; a company name resemblance is insufficient. This initial version does
not discover arbitrary employer domains or crawl an entire careers site.
`candidate_confirmations` accepts explicit `true` for requirements already
checked by the user (e.g. `hong_kong_work_authorization`,
`cantonese_speaking_requirement`, `english_requirement`,
`graduation_year_eligibility`, `student_status_requirement`). Do not invent these
confirmations. Unconfirmed stated requirements remain review items. Experience
requirements of three or more years and unstated experience remain review items.
These confirmations do not approve a scan, tracker push or application.

## Evidence and matching

The original scan card, including full untruncated portal data, is retained in
`raw_listing`. V3 scans also write a `_run_raw.json` sidecar including rejected
cards. Eligibility screening never overwrites the original source/location.

Matching requires the same disclosed employer, normalized title/function and
compatible HK location, with no contrary requisition, department, employment
type or work arrangement. A shared employer requisition or explicit official
link is a strong signal. Without one, department, arrangement, duties and
posting period must corroborate. Platform listing IDs are not employer
requisition IDs. Strict matching intentionally prefers false negatives over
incorrect JD replacement. Unverified recruiters remain ambiguous. A different
employer's similar role can provide market context only.

Each match records the candidate listing, URL, retrieval time, identity reasons
and conflicts. Verified fields retain source URL/host/time. Disagreeing
structured eligibility values cause review; differently worded responsibilities
are preserved together, with explicit sales duties still screened. The matcher
does not claim semantic equivalence between paraphrases or discover every
contradiction in free text.

Evidence states are additive:

| V3 evidence state | Interpretation |
| --- | --- |
| `verified_original` | Original sufficiently complete JD obtained |
| `verified_cross_source` | Fuller JD from a verified same vacancy |
| `partial_verified` | Reliable same-vacancy fields, incomplete JD |
| `market_reference_only` | Other-employer context, never original requirements |
| `needs_manual_review` | Missing, ambiguous or conflicting evidence |

Conflicts are explicit in `conflicts` and match verdict `conflicting_sources`;
the row's evidence state stays `needs_manual_review`. Existing `JD深度`,
`provisional_needs_jd`, scoring explanations, semantic gates and SOP controls
remain in use. A full description can still require eligibility review.

Detailed records and an independent input-keyed, seven-day cache live under
`02_Tracker/v3_evidence/`. They never overwrite the original URL-keyed JD cache.
Scored CSV artifacts add `v3_decision`, `v3_eligibility`, `v3_evidence_status`,
`v3_reasons` and `v3_evidence_path`; tracker projections exclude these internal
columns. Standard entry rejects review/excluded rows. The explicit all-entry
policy also cannot bypass the V3 evidence gate. No scan writes a tracker,
enables Sheets, creates materials or submits an application.

Artifact schema 5 invalidates old results that could contain FreeHire evidence.
V3 scored snapshots re-enter the independent evidence cache to check TTL and
configuration; they are not blindly reused. Official run hashes continue to be
registered through the existing scan adapter after final output is written.

## Public access boundaries

The provider uses bounded public portal search CLIs and public HTTP detail
pages with JobPosting JSON-LD. Official URLs must be registered and publicly
routable. TLS validation remains enabled. Redirects, unsupported markup,
robots denial, login challenges, CAPTCHA and access failures fall back to
review. An HTTP 401/403/429 defers further detail requests to that host for the
run. No CDP, browser launch, cookie-file extraction, solver or personal session
is used. JobsDB search results stay partial because its full-detail gateway
requires personal CDP. CT's public homepage bootstrap may use cookies issued
to that anonymous HTTP request; it ignores personal session bindings in V3.

Public source access depends on environment network policy. Potential hosts
include `www.linkedin.com`, `hk.jobsdb.com`, `www.ctgoodjobs.hk`,
`api01.ctgoodjobs.hk`, and explicitly registered official careers hosts. No
network policy, browser settings or credentials were changed by this feature.
No live cross-platform accuracy or personal browser integration is claimed.

## Validation and synthetic pilot

```bash
source .venv/bin/activate
export PATH=/workspace/.tools/node_modules/.bin:$PATH
python -m pytest -q tests/test_v3_enrichment.py tests/test_v3_pipeline.py
python scripts/v3_fixture_pilot.py --out /tmp/jobsflow-v3-pilot.json
python tools/check_types.py
```

The 15-job fixture input is `tests/fixtures/v3/jobs.json`. It contains explicitly
fictional listings, URL placeholders and identity labels. The pilot makes zero
live requests, uses a temporary independent cache, checks its input hash and
never reads/writes private scans or trackers. Detailed results are in
[v3_pilot_results.json](v3_pilot_results.json).

| Measurement | Observed fixture result |
| --- | ---: |
| Initially incomplete JDs | 14 |
| Successfully enriched jobs | 3 |
| Jobs with verified same-job matches | 4 |
| Verified same-job matches | 5 |
| Rejected/conflicting proposed matches | 2 |
| Incorrect verified matches against fixture labels | 0 |
| Remaining uncertain jobs | 8 |
| Critical eligibility fields recovered | 8 |
| Classification changes | 2 |
| Simulated provider calls / failures | 50 / 4 |
| Actual live requests | 0 |

Classifications changed from 1 priority / 10 review / 4 excluded to 3 priority /
8 review / 4 excluded. The fourth job with verified identity remains uncertain
because its two verified sources disagree on education; verified identity is
not the same as successful enrichment. Synthetic success does not demonstrate
production accuracy. Live portal, browser fixture and personal-runtime checks
require separate validation with accessible sources and approved runtime data.

For the full suite in this cloud machine, retain the onboarding test helper:

```bash
PYTHONPATH=/workspace/.tools/jobsflow_test_support \
  python -m pytest -q -p jobsflow_cloud_tests
```

It redirects only two existing fake-CDP tests' lock storage to test temporary
directories. It neither changes production home paths nor enables debugging.

Final cloud validation: **1,017 Python tests passed, 7 opt-in browser tests
skipped, 42 retired/legacy tests deselected**. The 78 V3 unit/integration tests
are included in that total. All four Bun CLI type checks passed, with **67 Bun
tests passed and 4 live tests skipped**. The 16-module Python type gate,
security guards, source-release check, skill/scope checks and SOP gate passed.
Live cross-platform enrichment and personal browser/runtime integration remain
unvalidated. No production accuracy metric is inferred from these checks.
