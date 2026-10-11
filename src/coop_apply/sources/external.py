"""External job boards via their public, documented JSON APIs.

- Greenhouse: https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
- Lever:      https://api.lever.co/v0/postings/{company}?mode=json
- Ashby:      https://api.ashbyhq.com/posting-api/job-board/{board}

Configure the board tokens of companies you care about in ``config.toml``
(the token is the slug in a company's careers URL, e.g. ``boards.greenhouse.io/stripe``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Iterable

from ..config import ExternalConfig
from ..models import Job
from ..utils import contains_any, html_to_text, normalize_whitespace

USER_AGENT = "coop-apply/0.1 (personal job search tool)"

Fetcher = Callable[[str], object]


def http_get_json(url: str, timeout: float = 30) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_greenhouse(board: str, data: dict) -> list[Job]:
    jobs = []
    for item in data.get("jobs", []):
        company = (item.get("company_name") or board).strip()
        jobs.append(
            Job(
                id=f"gh:{board}:{item['id']}",
                source="greenhouse",
                title=item.get("title", "").strip(),
                company=company,
                location=(item.get("location") or {}).get("name", ""),
                description=html_to_text(item.get("content", "")),
                url=item.get("absolute_url", ""),
                apply_url=item.get("absolute_url", ""),
                extra={"updated": item.get("updated_at", "")} if item.get("updated_at") else {},
            )
        )
    return jobs


def parse_lever(company: str, data: list) -> list[Job]:
    jobs = []
    for item in data:
        sections = [item.get("descriptionPlain", "")]
        for lst in item.get("lists", []):
            sections.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
        sections.append(item.get("additionalPlain", ""))
        cats = item.get("categories") or {}
        jobs.append(
            Job(
                id=f"lever:{company}:{item['id']}",
                source="lever",
                title=item.get("text", "").strip(),
                company=company,
                location=cats.get("location", "") or "",
                description=normalize_whitespace("\n\n".join(s for s in sections if s)),
                url=item.get("hostedUrl", ""),
                apply_url=item.get("applyUrl") or item.get("hostedUrl", ""),
                extra={k: v for k, v in {"team": cats.get("team"), "commitment": cats.get("commitment")}.items() if v},
            )
        )
    return jobs


def parse_ashby(board: str, data: dict) -> list[Job]:
    jobs = []
    for item in data.get("jobs", []):
        if item.get("isListed") is False:
            continue
        description = item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml", ""))
        jobs.append(
            Job(
                id=f"ashby:{board}:{item['id']}",
                source="ashby",
                title=item.get("title", "").strip(),
                company=board,
                location=item.get("location", "") or "",
                description=normalize_whitespace(description),
                url=item.get("jobUrl", ""),
                apply_url=item.get("applyUrl") or item.get("jobUrl", ""),
                extra={k: v for k, v in {"team": item.get("team"), "employment": item.get("employmentType")}.items() if v},
            )
        )
    return jobs


def _relevant(job: Job, cfg: ExternalConfig) -> bool:
    return not cfg.title_keywords or contains_any(job.title, cfg.title_keywords)


def fetch_external(
    cfg: ExternalConfig,
    fetch: Fetcher = http_get_json,
    log: Callable[[str], None] = print,
) -> Iterable[Job]:
    """Yield student/intern postings from every configured board."""
    boards = (
        [("greenhouse", b, f"https://boards-api.greenhouse.io/v1/boards/{b}/jobs?content=true", parse_greenhouse) for b in cfg.greenhouse]
        + [("lever", b, f"https://api.lever.co/v0/postings/{b}?mode=json", parse_lever) for b in cfg.lever]
        + [("ashby", b, f"https://api.ashbyhq.com/posting-api/job-board/{b}", parse_ashby) for b in cfg.ashby]
    )
    for kind, board, url, parser in boards:
        try:
            data = fetch(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            log(f"  ! {kind}/{board}: {e}")
            continue
        all_jobs = parser(board, data)
        kept = [j for j in all_jobs if _relevant(j, cfg)]
        log(f"  {kind}/{board}: {len(kept)} relevant of {len(all_jobs)} postings")
        yield from kept
