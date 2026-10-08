"""Opt-in HK early-career screening and conservative same-vacancy evidence.

No browser, tracker, cookie, model, or legacy JD-cache writes. Providers return
structured listings; similar roles are never used as original-job evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tools.io_utils import atomic_write_json

VERSION = 1
SOURCES = ("employer_official_careers", "jobsdb", "linkedin", "ctgoodjobs")
PORTAL_HOSTS = {"jobsdb": "jobsdb.com", "linkedin": "linkedin.com", "ctgoodjobs": "ctgoodjobs.hk"}
EVIDENCE_FIELDS = (
    "responsibilities", "required_years_experience", "education",
    "graduation_year_eligibility", "student_status_requirement",
    "cantonese_speaking_requirement", "english_requirement",
    "hong_kong_work_authorization", "sales_or_commission_duties",
    "employment_type", "location",
)
HK_PLACES = (
    "hong kong", "hongkong", "hk", "kowloon", "new territories", "central and western",
    "ngau tau kok", "kwai chung", "tai po", "sha tin", "shatin", "tsim sha tsui",
    "quarry bay", "tseung kwan o", "wan chai", "wanchai", "causeway bay", "admiralty",
    "sheung wan", "sai ying pun", "kennedy town", "chung hom kok", "chai wan",
    "north point", "sai wan ho", "aberdeen", "wong chuk hang", "stanley", "repulse bay",
    "kwun tong", "kowloon bay", "san po kong", "wong tai sin", "diamond hill",
    "mong kok", "mongkok", "yau ma tei", "yau tsim mong", "sham shui po", "cheung sha wan",
    "lai chi kok", "hung hom", "to kwa wan", "ho man tin", "kowloon city", "yau tong",
    "lam tin", "tuen mun", "yuen long", "tin shui wai", "tsuen wan", "tsing yi",
    "kwai tsing", "fanling", "sheung shui", "tai wai", "ma on shan", "sai kung",
    "tung chung", "lantau", "chek lap kok", "discovery bay", "islands district",
    "香港", "九龍", "九龙", "新界", "中西區", "中西区", "觀塘", "观塘", "牛頭角",
    "牛头角", "葵涌", "大埔", "沙田", "尖沙咀", "鰂魚涌", "鲗鱼涌", "將軍澳", "将军澳",
    "灣仔", "湾仔", "荃灣", "荃湾", "元朗", "屯門", "屯门", "東涌", "东涌",
)
OVERSEAS = (
    "singapore", "dusseldorf", "düsseldorf", "mesa", "arizona", "london", "united kingdom",
    "uk", "united states", "usa", "new york", "san francisco", "california", "sydney",
    "melbourne", "australia", "toronto", "canada", "dubai", "uae", "tokyo", "japan",
    "shenzhen", "shanghai", "beijing", "guangzhou", "mainland", "macau", "macao",
    "taipei", "taiwan", "germany", "berlin", "paris", "france", "india", "mumbai",
    "bangkok", "thailand", "kuala lumpur", "malaysia", "manila", "philippines",
    "新加坡", "深圳", "上海", "北京", "广州", "廣州", "澳門", "澳门", "台北",
)
SENIOR = r"\b(?:director|(?:vice\s+president)|vp|avp|senior\s+manager|head\s+of)\b|總監|总监|部門主管|部门主管"
MIXED = r"\b(associate|consultant|analyst)\s*(?:/|or|and|&)\s*senior\s+\1\b|\bsenior\s+(associate|consultant|analyst)\s*(?:/|or|and|&)\s*\2\b"
SALES_TITLE = r"\b(?:insurance\s+(?:product\s+)?(?:sales|agent)|wealth\s+management\s+(?:sales|trainee)|family\s+wealth\s+management\s+sales|financial\s+advis[eo]r.{0,25}(?:selling|sales)|commission[- ]only|telemarketing|sales\s+representative)\b|保險銷售|保险销售|保險代理|保险代理"
SALES_DUTIES = r"\b(?:sell(?:ing)?\s+(?:(?:life\s+)?insurance|investment|wealth|financial)\s+(?:products?|policies)|distribut(?:e|ing|ion\s+of)\s+(?:insurance|investment|wealth|financial)\s+products?|commission[- ]only|sales\s+quota|solicit\s+insurance)\b"
LANES = {
    "A": r"graduate|management trainee|trainee programme|graduate program",
    "B": r"client relations|investor relations|stakeholder",
    "C": r"risk advisory|internal control|governance|compliance|risk",
    "D": r"operations|project|coordination|business support",
    "E": r"communications|marketing|public relations",
    "F": r"esg|sustainability",
}


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("_", " ")
    return re.sub(r"[^\w]+", " ", text).strip()


def contains(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.I))


def source_allowed(source: str, url: str = "") -> bool:
    source = source.casefold()
    if source not in SOURCES or "freehire" in url.casefold():
        return False
    if not url:
        return True
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    expected = PORTAL_HOSTS.get(source)
    return bool(host) and (not expected or host == expected or host.endswith("." + expected))


def prohibited_listing(job: dict[str, Any]) -> bool:
    """FreeHire is prohibited even when an old config explicitly enables it."""
    return "freehire" in str(job.get("source") or "").casefold() or "freehire" in str(job.get("url") or "").casefold()


def location_verdict(value: Any) -> str:
    text = str(value or "").strip().casefold()
    hk = any(contains(text, place) for place in HK_PLACES)
    overseas = any(contains(text, place) for place in OVERSEAS)
    if overseas and not hk:
        return "overseas"
    if not text or overseas or re.search(r"\b(remote|hybrid|global|multiple|various|apac|asia|worldwide)\b", text):
        return "needs_verification"
    # A bare district such as Central is shared by many cities. Never infer
    # HK from the portal host, search query, IP or a missing-location default.
    return "hong_kong" if hk else "needs_verification"


def extract_fields(description: str) -> dict[str, str]:
    """Retain verbatim evidence excerpts; do not infer unstated requirements."""
    patterns = {
        "responsibilities": r"responsibilit|duties|coordinate|support|maintain|工作職責|工作职责",
        "required_years_experience": r"\d+\s*(?:[-–]\s*\d+)?\s*(?:years?|年).{0,30}(?:experience|經驗|经验)|fresh graduates?|no experience",
        "education": r"bachelor|master|degree|diploma|學士|学士|學歷|学历",
        "graduation_year_eligibility": r"graduat.{0,35}20\d\d|20\d\d.{0,35}graduat",
        "student_status_requirement": r"current(?:ly)?\s+(?:enrolled|student)|undergraduate students?|full[- ]time students?",
        "cantonese_speaking_requirement": r"cantonese|廣東話|广东话|粵語|粤语",
        "english_requirement": r"english|英語|英语",
        "hong_kong_work_authorization": r"work\s+(?:authorization|authorisation|permit|visa)|right to work|eligible to work|working visa",
        "sales_or_commission_duties": SALES_DUTIES,
    }
    sentences = [s.strip() for s in re.split(r"(?<=[.!?;。；])\s*|\n+", description) if s.strip()]
    return {field: " ".join(s for s in sentences if re.search(pattern, s, re.I)) for field, pattern in patterns.items() if any(re.search(pattern, s, re.I) for s in sentences)}


def fields_of(job: dict[str, Any]) -> dict[str, str]:
    fields = extract_fields(str(job.get("description") or ""))
    fields.update({k: str(v) for k, v in (job.get("fields") or {}).items() if k in EVIDENCE_FIELDS and v})
    for key in ("location", "employment_type"):
        if job.get(key):
            fields[key] = str(job[key])
    return fields


def has_sales_duties(text: str) -> bool:
    for sentence in re.split(r"[.!?;。；\n]", text):
        for match in re.finditer(SALES_DUTIES, sentence, re.I):
            prefix = sentence[max(0, match.start() - 55):match.start()]
            if not re.search(r"\b(?:no|not|never|without|not required to)\b[^.!?;]{0,40}$", prefix, re.I):
                return True
    return False


def evidence_status(job: dict[str, Any]) -> str:
    fields = fields_of(job)
    text = str(job.get("description") or "").strip()
    if len(text) >= 200 and fields.get("responsibilities") and (fields.get("education") or fields.get("required_years_experience")):
        return "verified_original"
    return "partial_verified" if text or job.get("fields") else "needs_manual_review"


def screen_job(job: dict[str, Any], *, verified_text: str = "", candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    exclusions: list[str] = []
    title = str(job.get("title") or "")
    geography = location_verdict(job.get("location"))
    if prohibited_listing(job):
        exclusions.append("prohibited_source_freehire")
    if geography == "overseas":
        exclusions.append("overseas_location")
    elif geography != "hong_kong":
        reasons.append("location_requires_verification")
    if re.search(MIXED, title, re.I):
        reasons.append("mixed_seniority_confirm_junior_opening")
    elif re.search(SENIOR, title, re.I):
        exclusions.append("clearly_senior_title")
    sales_title = bool(re.search(SALES_TITLE, title, re.I))
    if sales_title and re.search(r"\b(?:support|operations|administration|communications)\b", title, re.I):
        sales_title = False
        reasons.append("sales_support_scope_requires_review")
    if sales_title or has_sales_duties(verified_text):
        exclusions.append("direct_product_sales")
    elif re.search(r"wealth management|business development trainee|client relationship manager|private banking|account manag", title, re.I):
        reasons.append("sales_scope_unknown")
    if re.search(r"quantitative|quant trader|quant researcher", title, re.I):
        reasons.append("technical_quantitative_fit_review")
    verified_fields = fields_of(dict(job, description=verified_text)) if verified_text else {}
    candidate = candidate or {}
    for field in ("graduation_year_eligibility", "student_status_requirement", "cantonese_speaking_requirement", "english_requirement", "hong_kong_work_authorization"):
        value = verified_fields.get(field)
        # Candidate facts are optional. Do not invent language, visa or
        # graduation facts, and never treat a preference as a hard mismatch.
        if value and candidate.get(field) is not True:
            reasons.append(field + "_requires_confirmation")
    years = re.search(r"\b(\d+)\+?\s*(?:years?|年).{0,30}(?:experience|經驗|经验)", verified_text, re.I)
    if years and int(years.group(1)) >= 3:
        reasons.append("experience_requirement_review")
    if not verified_text:
        reasons.append("jd_incomplete")
    elif not verified_fields.get("required_years_experience"):
        reasons.append("experience_requirement_unstated")
    career_text = title + "\n" + verified_text
    lane_matches = [lane for lane, pattern in LANES.items() if re.search(pattern, career_text, re.I)]
    if not lane_matches:
        reasons.append("target_lane_unconfirmed")
    decision = "exclude" if exclusions else "needs_manual_review" if reasons else "priority_a" if "A" in lane_matches else "priority_b"
    return {"decision": decision, "eligibility": "ineligible" if exclusions else "uncertain" if reasons else "eligible",
            "career_fit": {"lanes": lane_matches, "technical_review": "technical_quantitative_fit_review" in reasons},
            "geography": geography, "reasons": exclusions + reasons}


def match_vacancy(original: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    conflicts: list[str] = []
    if not source_allowed(str(candidate.get("source") or ""), str(candidate.get("url") or "")):
        return {"verdict": "rejected", "reasons": ["source_not_allowed"], "conflicts": []}
    employer = normalized(original.get("company"))
    if not employer or employer in {"unknown", "confidential", "recruiter", "undisclosed"} or original.get("employer_disclosed") is False or candidate.get("employer_disclosed") is False:
        return {"verdict": "possible_match_review", "reasons": ["employer_identity_unverified"], "conflicts": []}
    if employer != normalized(candidate.get("company")):
        return {"verdict": "market_reference_only", "reasons": ["different_employer"], "conflicts": []}
    if not original.get("title") or normalized(original.get("title")) != normalized(candidate.get("title")):
        return {"verdict": "rejected", "reasons": ["different_title_or_function"], "conflicts": []}
    reasons.extend(["same_employer", "matching_title"])
    locations = [location_verdict(x.get("location")) for x in (original, candidate)]
    if "overseas" in locations:
        conflicts.append("location")
    elif locations != ["hong_kong", "hong_kong"]:
        return {"verdict": "possible_match_review", "reasons": reasons + ["hk_location_unconfirmed"], "conflicts": []}
    for field in ("department", "employment_type", "work_arrangement", "requisition_id"):
        left, right = normalized(original.get(field)), normalized(candidate.get(field))
        if left and right and left != right:
            conflicts.append(field)
    if conflicts:
        return {"verdict": "conflicting_sources", "reasons": reasons, "conflicts": conflicts}
    reasons.append("compatible_hk_location")
    if original.get("requisition_id") and normalized(original["requisition_id"]) == normalized(candidate.get("requisition_id")):
        return {"verdict": "verified_same_job", "reasons": reasons + ["same_requisition_id"], "conflicts": []}
    if candidate.get("source") == "employer_official_careers" and candidate.get("linked_original_url") == original.get("url"):
        return {"verdict": "verified_same_job", "reasons": reasons + ["official_link_to_original"], "conflicts": []}
    dates = [str(x.get("posted_at") or "")[:10] for x in (original, candidate)]
    try:
        days = abs((datetime.fromisoformat(dates[0]) - datetime.fromisoformat(dates[1])).days)
    except ValueError:
        days = None
    if days is not None and days > 14:
        return {"verdict": "possible_repost_review", "reasons": reasons + ["posting_period_disagrees"], "conflicts": []}
    corroboration = all(original.get(f) and normalized(original[f]) == normalized(candidate.get(f)) for f in ("department", "work_arrangement"))
    duties = [normalized(fields_of(x).get("responsibilities")) for x in (original, candidate)]
    if corroboration and days is not None and days <= 14 and duties[0] and duties[0] == duties[1]:
        return {"verdict": "verified_same_job", "reasons": reasons + ["department_arrangement_duties_and_posting_period"], "conflicts": []}
    return {"verdict": "possible_match_review", "reasons": reasons + ["insufficient_identity_signals"], "conflicts": []}


@dataclass(frozen=True)
class Limits:
    max_jobs: int = 15
    max_searches: int = 4
    max_pages: int = 8
    timeout: int = 25
    cache_ttl_hours: int = 168

    def __post_init__(self):
        for field, ceiling in (("max_jobs", 15), ("max_searches", 4), ("max_pages", 8), ("timeout", 25), ("cache_ttl_hours", 168)):
            if not 0 < getattr(self, field) <= ceiling:
                raise ValueError(f"{field} must be within 1..{ceiling}")


class Provider(Protocol):
    def search(self, original: dict[str, Any], source: str, *, timeout: int, limit: int) -> list[dict[str, Any]]: ...
    def fetch(self, candidate: dict[str, Any], *, timeout: int) -> dict[str, Any]: ...


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k not in {"trackingId", "trk"}))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), query, ""))


class Enricher:
    """Serial bounded requests (one in flight), independent expiring cache."""
    def __init__(self, provider: Provider, *, cache_dir: Path, limits: Limits | None = None):
        self.provider = provider
        self.cache_dir = Path(cache_dir)
        self.limits = limits or Limits()
        self.jobs = 0

    def enrich(self, original: dict[str, Any], *, allow_requests: bool = True, try_original: bool = False) -> dict[str, Any]:
        original = copy.deepcopy(original)
        key_data = {"version": VERSION, "limits": asdict(self.limits), "original": original, "try_original": try_original,
                    "provider": getattr(self.provider, "cache_identity", type(self.provider).__name__)}
        key = hashlib.sha256(json.dumps(key_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        result: dict[str, Any] = {"version": VERSION, "original": original, "status": evidence_status(original),
            "matches": [], "field_provenance": {}, "conflicts": [], "errors": [],
            "scoring_text": str(original.get("description") or original.get("teaser") or ""),
            "requests": {"searches": 0, "pages": 0}, "cache_hit": False}
        if prohibited_listing(original):
            result.update(status="needs_manual_review", scoring_text="", errors=["prohibited_source_freehire"])
            return result
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["cached_at"])).total_seconds()
            if 0 <= age < self.limits.cache_ttl_hours * 3600 and cached.get("key") == key:
                return dict(cached["result"], cache_hit=True, requests={"searches": 0, "pages": 0})
        except (OSError, ValueError, KeyError, TypeError):
            pass
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def add_fields(job: dict[str, Any], match: dict[str, Any]):
            for field, value in fields_of(job).items():
                result["field_provenance"].setdefault(field, []).append({"value": value, "url": job.get("url"),
                    "source": job.get("source"), "source_host": urlsplit(str(job.get("url") or "")).hostname,
                    "retrieved_at": job.get("retrieved_at") or now, "match_reasons": match["reasons"]})

        if original.get("description"):
            add_fields(original, {"reasons": ["original_listing"]})
        if result["status"] == "verified_original":
            return result
        if not allow_requests or self.jobs >= self.limits.max_jobs:
            result["errors"].append("requests_deferred_or_pilot_budget_exhausted")
            return result
        self.jobs += 1
        if try_original and source_allowed(str(original.get("source") or ""), str(original.get("url") or "")):
            result["requests"]["pages"] += 1
            try:
                detail = self.provider.fetch(original, timeout=self.limits.timeout)
                if detail.get("source") != original.get("source") or canonical_url(str(detail.get("url") or "")) != canonical_url(str(original.get("url") or "")):
                    raise ValueError("original_detail_source_mismatch")
                if normalized(detail.get("company")) != normalized(original.get("company")) or normalized(detail.get("title")) != normalized(original.get("title")):
                    raise ValueError("original_detail_identity_mismatch")
                if location_verdict(detail.get("location")) == "overseas" and location_verdict(original.get("location")) == "hong_kong":
                    result["conflicts"].append({"url": detail.get("url"), "fields": ["location"]})
                result["original_detail"] = detail
                if detail.get("description"):
                    add_fields(detail, {"reasons": ["original_listing_detail"]})
                if evidence_status(detail) == "verified_original" and not result["conflicts"]:
                    result.update(status="verified_original", scoring_text=detail["description"])
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(path, {"key": key, "cached_at": now, "result": result})
                    return result
            except Exception as exc:
                result["errors"].append(f"original: {type(exc).__name__}")
        verified: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for source in SOURCES[:self.limits.max_searches]:
            if result["requests"]["pages"] >= self.limits.max_pages:
                break
            result["requests"]["searches"] += 1
            try:
                candidates = self.provider.search(original, source, timeout=self.limits.timeout, limit=self.limits.max_pages)
            except Exception as exc:
                result["errors"].append(f"{source}: {type(exc).__name__}")
                continue
            for candidate in candidates:
                if result["requests"]["pages"] >= self.limits.max_pages:
                    break
                if candidate.get("source") != source or not source_allowed(source, str(candidate.get("url") or "")):
                    continue
                if canonical_url(str(candidate["url"])) == canonical_url(str(original.get("url") or "")):
                    continue
                identity = (canonical_url(str(candidate["url"])), normalized(candidate.get("requisition_id")))
                if identity in seen:
                    continue
                seen.add(identity)
                result["requests"]["pages"] += 1
                try:
                    fetched = self.provider.fetch(candidate, timeout=self.limits.timeout)
                    if not source_allowed(str(fetched.get("source") or ""), str(fetched.get("url") or "")):
                        raise ValueError("untrusted_fetched_source")
                    match = match_vacancy(original, fetched)
                    result["matches"].append({"url": fetched.get("url"), "source": fetched.get("source"),
                        "retrieved_at": fetched.get("retrieved_at") or now, "candidate_listing": fetched, **match})
                    if match["verdict"] == "verified_same_job":
                        verified.append(fetched)
                        add_fields(fetched, match)
                    elif match["conflicts"]:
                        result["conflicts"].append({"url": fetched.get("url"), "fields": match["conflicts"]})
                except Exception as exc:
                    result["errors"].append(f"{source}: {type(exc).__name__}")
        for field, values in result["field_provenance"].items():
            # Descriptions can paraphrase duties. Only structured eligibility
            # disagreements are deterministic conflicts; responsibilities are
            # all preserved and never silently overwritten.
            if field != "responsibilities" and len({normalized(v["value"]) for v in values}) > 1:
                if field == "location" and all(location_verdict(v["value"]) == "hong_kong" for v in values):
                    continue
                result["conflicts"].append({"field": field, "evidence": values})
        if result["conflicts"]:
            result["status"] = "needs_manual_review"
        elif any(evidence_status(v) == "verified_original" for v in verified):
            result["status"] = "verified_cross_source"
            result["scoring_text"] = "\n\n".join(str(v.get("description") or "") for v in verified)
        elif verified:
            result["status"] = "partial_verified"
        elif result["matches"] and all(m["verdict"] == "market_reference_only" for m in result["matches"]):
            result["status"] = "market_reference_only"
        else:
            result["status"] = "needs_manual_review"
        result["outstanding_unknowns"] = [f for f in EVIDENCE_FIELDS if f not in result["field_provenance"]]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"key": key, "cached_at": now, "result": result})
        return result


def load_v3_config(repo: Path) -> dict[str, Any]:
    root = Path(os.environ.get("JOBSEARCH_ROOT") or repo)
    if root.name != "JobSearch_2026":
        root /= "JobSearch_2026"
    try:
        config = json.loads((root / "00_Profile" / "queries.json").read_text(encoding="utf-8"))
        value = config.get("v3_enrichment") or {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
