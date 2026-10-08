"""Public-only V3 sources. No CDP, browser launch, cookies or WAF solver.

Official employer hosts and seed posting URLs must be explicitly configured;
an arbitrary host is never labeled official from a title/company resemblance.
Public detail extraction accepts JobPosting JSON-LD only, and fails gracefully
on blocked pages, robots denial, redirects or unsupported markup.
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from tools.fresh_24h.cross_source_enrich import PORTAL_HOSTS, normalized, source_allowed

USER_AGENT = "JobsFlow/3 (+public job evidence; no authentication)"
MAX_BYTES = 1_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JobPostingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_json = False
        self.current: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").casefold() == "application/ld+json":
            self.in_json = True
            self.current = []

    def handle_data(self, data):
        if self.in_json:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json:
            self.blocks.append("".join(self.current))
            self.in_json = False


def clean_html(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def parse_job_postings(body: str, *, url: str, source: str) -> list[dict[str, Any]]:
    parser = JobPostingParser()
    parser.feed(body)
    postings: list[dict[str, Any]] = []

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            types = value.get("@type") or []
            if "JobPosting" in ([types] if isinstance(types, str) else types):
                organization = value.get("hiringOrganization") or {}
                if not isinstance(organization, dict):
                    organization = {}
                locations = value.get("jobLocation") or []
                if isinstance(locations, dict):
                    locations = [locations]
                location_parts = []
                for loc in locations:
                    address = loc.get("address") or {} if isinstance(loc, dict) else {}
                    if isinstance(address, dict):
                        location_parts.append(", ".join(str(address[k]) for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k)))
                identifier = value.get("identifier") or {}
                ref = identifier.get("value", "") if isinstance(identifier, dict) else ""
                identifier_name = str(identifier.get("name") or "") if isinstance(identifier, dict) else ""
                if source != "employer_official_careers" and not re.search(r"requisition|employer|reference", identifier_name, re.I):
                    ref = ""  # A platform listing ID is not an employer requisition ID.
                fields = {key: clean_html(value[attr]) for key, attr in (
                    ("responsibilities", "responsibilities"), ("education", "educationRequirements"),
                    ("required_years_experience", "experienceRequirements"),
                ) if isinstance(value.get(attr), str) and value[attr]}
                postings.append({"source": source, "url": url, "company": organization.get("name") or "",
                    "title": value.get("title") or "", "description": clean_html(value.get("description")),
                    "location": " / ".join(location_parts), "requisition_id": str(ref),
                    "department": value.get("department") or "", "fields": fields,
                    "employment_type": value.get("employmentType") or "", "posted_at": value.get("datePosted") or "",
                    "work_arrangement": "remote" if value.get("jobLocationType") == "TELECOMMUTE" else "",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            for key in ("@graph", "mainEntity"):
                if key in value:
                    walk(value[key])
    for block in parser.blocks:
        try:
            walk(json.loads(block))
        except (ValueError, TypeError):
            continue
    return postings


class PublicProvider:
    def __init__(self, repo: Path, config: dict[str, Any]):
        self.repo = Path(repo)
        self.official = config.get("official_employers") or {}
        self.cache_identity = {"official_employers": self.official, "transport_version": 1}
        self.robots: dict[str, RobotFileParser] = {}
        self.http_requests = 0
        self.blocked_hosts: set[str] = set()

    def _official_config(self, company: str) -> dict[str, Any]:
        return next((value for key, value in self.official.items() if normalized(key) == normalized(company)), {})

    def search(self, original: dict[str, Any], source: str, *, timeout: int, limit: int) -> list[dict[str, Any]]:
        if source == "employer_official_careers":
            return [{"url": url, "source": source, "company": original.get("company")} for url in self._official_config(str(original.get("company") or "")).get("posting_urls", [])][:limit]
        if source not in PORTAL_HOSTS:
            return []
        cli = self.repo / f".agents/skills/{source}-search/cli/src/cli.ts"
        env = dict(os.environ)
        env["JOBSFLOW_PUBLIC_ONLY"] = "1"
        env["JOBSFLOW_SCAN_MAX_RETRIES"] = "0"
        env["JOBSFLOW_SCAN_REQUEST_TIMEOUT_MS"] = str(timeout * 1000)
        query = f'{original.get("company") or ""} {original.get("title") or ""}'
        cmd = ["bun", "run", str(cli), "search", "-q", query, "--jobage", "30", "--limit", str(limit), "--format", "json"]
        if source == "linkedin":
            cmd += ["-l", "Hong Kong"]
        proc = subprocess.run(cmd, cwd=self.repo, env=env, capture_output=True, text=True, timeout=timeout, check=True)
        data = json.loads(proc.stdout[proc.stdout.find("{"):])
        return [dict(row, source=source) for row in (data.get("results") or []) if isinstance(row, dict)][:limit]

    def _validate_url(self, url: str, source: str, company: str) -> str:
        if not source_allowed(source, url):
            raise ValueError("source_not_allowed")
        host = urlsplit(url).hostname or ""
        if source == "employer_official_careers" and host not in self._official_config(company).get("hosts", []):
            raise ValueError("official_employer_host_not_registered")
        # Do not allow configured official URLs to access local/internal hosts.
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
            raise ValueError("nonpublic_destination")
        return host

    def _get(self, url: str, timeout: int) -> str:
        host = urlsplit(url).hostname or ""
        if host in self.blocked_hosts:
            raise PermissionError("host_deferred_after_access_or_rate_limit_failure")
        context = ssl.create_default_context(cafile=os.environ.get("REQUESTS_CA_BUNDLE") or None)
        opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        self.http_requests += 1
        try:
            with opener.open(Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"}), timeout=timeout) as response:
                payload = response.read(MAX_BYTES + 1)
                if len(payload) > MAX_BYTES:
                    raise ValueError("response_too_large")
                return payload.decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code in {401, 403, 429}:
                self.blocked_hosts.add(host)
            raise

    def fetch(self, candidate: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        source, url = str(candidate.get("source") or ""), str(candidate.get("url") or "")
        host = self._validate_url(url, source, str(candidate.get("company") or ""))
        if source == "jobsdb":
            # Public search fields are explicitly partial. Never upgrade the
            # SPA shell/snippet to a full JD or call the CDP gateway here.
            return dict(candidate, description="", fields={})
        if host not in self.robots:
            parser = RobotFileParser()
            parser.parse(self._get(f"https://{host}/robots.txt", timeout).splitlines())
            self.robots[host] = parser
        if not self.robots[host].can_fetch(USER_AGENT, url):
            raise PermissionError("robots_disallowed")
        body = self._get(url, timeout)
        if re.search(r"verify you are human|captcha|access denied|just a moment", body, re.I):
            raise PermissionError("access_challenge")
        rows = parse_job_postings(body, url=url, source=source)
        if len(rows) != 1:
            raise ValueError("single_jobposting_required")
        # Detail fields must come from the actual page, not the search seed.
        # A missing employer/location remains unknown. Do not copy them in.
        return rows[0]
