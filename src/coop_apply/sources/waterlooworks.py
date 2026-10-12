"""WaterlooWorks co-op postings, scraped from your own logged-in browser session.

WaterlooWorks has no public API, so this drives a real browser with Playwright.
Parsing is deliberately text-based (label/value pairs from the posting's visible
text) so it survives most markup changes; the few CSS selectors used to find rows,
open postings and paginate live in config.toml under [waterlooworks.selectors].
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Iterator

from ..config import WaterlooWorksConfig
from ..models import Job
from ..utils import normalize_whitespace, short_hash

# Labels that appear on WaterlooWorks posting pages. Matched case-insensitively at the
# start of a line, with or without a trailing colon; a value may follow on the same line
# or on the following lines.
LABELS = [
    "Work Term",
    "Job Type",
    "Job Title",
    "Number of Job Openings",
    "Level",
    "Region",
    "Job - City",
    "Job - Province/State",
    "Job - Country",
    "Job - Postal/Zip Code",
    "Job - Address Line One",
    "Job - Address Line Two",
    "Job Location (If Exact Address Unknown or Multiple Locations)",
    "Employment Location Arrangement",
    "Work Schedule",
    "Job Summary",
    "Job Responsibilities",
    "Required Skills",
    "Targeted Degrees and Disciplines",
    "Compensation and Benefits",
    "Compensation and Benefits Information",
    "Application Deadline",
    "Application Documents Required",
    "Additional Application Information",
    "Application Method",
    "If by Website, go to",
    "Organization",
    "Division",
    "Job Posting Status",
    "Posting ID",
    "Job ID",
]
DESCRIPTION_LABELS = [
    "Job Summary",
    "Job Responsibilities",
    "Required Skills",
    "Targeted Degrees and Disciplines",
    "Compensation and Benefits",
    "Compensation and Benefits Information",
    "Additional Application Information",
]
EXTRA_LABELS = [
    "Work Term",
    "Job Type",
    "Level",
    "Number of Job Openings",
    "Employment Location Arrangement",
    "Work Schedule",
    "Application Documents Required",
    "Application Method",
    "If by Website, go to",
    "Division",
]

_LABEL_RE = re.compile(
    r"^(" + "|".join(re.escape(l) for l in sorted(LABELS, key=len, reverse=True)) + r")\s*:?\s*(.*)$",
    re.IGNORECASE,
)
_CANON = {l.lower(): l for l in LABELS}
_ID_RE = re.compile(r"\b(\d{5,7})\b")


def parse_label_values(text: str) -> dict[str, str]:
    """Split a posting's visible text into {label: value}."""
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _LABEL_RE.match(line)
        if m:
            current = _CANON[m.group(1).lower()]
            fields.setdefault(current, [])
            if m.group(2):
                fields[current].append(m.group(2))
        elif current and line:
            fields[current].append(line)
    return {k: normalize_whitespace("\n".join(v)) for k, v in fields.items() if v}


_ID_IN_MARKUP = re.compile(r"(?:posting|job)[_-]?id\D{0,4}(\d{5,7})", re.I)
_ID_IN_TEXT = re.compile(r"\b(?:job|posting)\s*(?:id|#|number)\s*[:#]?\s*(\d{5,7})\b", re.I)
_ID_PREFIX = re.compile(r"^\s*(\d{5,7})\s*[-–:|]\s*\S")  # "123456 - Software Developer"


def posting_id_from(cells: list[str], href: str = "", text: str = "") -> str | None:
    """Find WaterlooWorks' numeric posting ID in the link markup, a table cell, or the posting text."""
    if m := _ID_IN_MARKUP.search(href or ""):
        return m.group(1)
    for cell in cells:  # an "ID" column holds just the number
        if m := _ID_RE.fullmatch((cell or "").strip()):
            return m.group(1)
    for cell in cells:
        if m := _ID_PREFIX.match(cell or ""):
            return m.group(1)
    fields = parse_label_values(text) if text else {}
    for key in ("Posting ID", "Job ID"):
        if key in fields and (m := _ID_RE.search(fields[key])):
            return m.group(1)
    head = "\n".join((text or "").splitlines()[:5])
    if m := (_ID_IN_TEXT.search(text or "") or _ID_PREFIX.match(head)):
        return m.group(1)
    return None


def job_from_posting(text: str, cells: list[str] | None = None, url: str = "", posting_id: str | None = None) -> Job:
    cells = cells or []
    fields = parse_label_values(text)
    title = fields.get("Job Title") or (cells[1] if len(cells) > 1 else "") or "Untitled posting"
    company = fields.get("Organization") or (cells[2] if len(cells) > 2 else "") or "Unknown organization"
    location = ", ".join(
        x for x in (fields.get("Job - City"), fields.get("Job - Province/State"), fields.get("Job - Country")) if x
    ) or fields.get("Job Location (If Exact Address Unknown or Multiple Locations)", "") or fields.get("Region", "")
    description = "\n\n".join(f"{label}:\n{fields[label]}" for label in DESCRIPTION_LABELS if label in fields)
    if not description:
        description = normalize_whitespace(text)  # unknown layout: keep everything
    pid = posting_id or posting_id_from(cells, url, text) or short_hash(title, company)
    return Job(
        id=f"ww:{pid}",
        source="waterlooworks",
        title=title.splitlines()[0],
        company=company.splitlines()[0],
        location=location,
        description=description,
        url=url,
        apply_url=fields.get("If by Website, go to", ""),
        deadline=fields.get("Application Deadline", ""),
        extra={k: fields[k] for k in EXTRA_LABELS if k in fields},
    )


def is_logged_in(page) -> bool:
    return "waterlooworks" in page.url and "/myAccount" in page.url


def wait_for_login(page, cfg: WaterlooWorksConfig, timeout_s: int = 300, log: Callable[[str], None] = print) -> bool:
    page.goto(cfg.postings_url)
    if is_logged_in(page):
        return True
    log("Log in to WaterlooWorks in the browser window (SSO + Duo). Waiting up to 5 minutes...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_logged_in(page):
            return True
        page.wait_for_timeout(1000)
    return False


def first_visible(page, selectors: str, timeout_ms: int = 10_000):
    """First visible match, trying comma-separated selectors in priority order.

    (A plain comma selector returns matches in document order, so a broad fallback
    like ``main`` could win over the posting dialog.)
    """
    candidates = [s.strip() for s in selectors.split(",") if s.strip()]
    deadline = time.time() + timeout_ms / 1000
    while True:
        for css in candidates:
            loc = page.locator(css)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        if time.time() > deadline:
            raise TimeoutError(f"none of {selectors!r} became visible")
        page.wait_for_timeout(250)


def _open_detail(ctx, page, link, sel):
    """Click a posting link; return (detail_page, detail_text, close_fn)."""
    before, url_before = len(ctx.pages), page.url
    link.click()
    for _ in range(6):  # ~1.5s to see whether the posting opened in a new tab
        if len(ctx.pages) > before:
            popup = ctx.pages[-1]
            popup.wait_for_load_state()
            return popup, popup.inner_text("body"), popup.close
        page.wait_for_timeout(250)
    # Otherwise it opened in a modal/panel or navigated this tab.
    text = first_visible(page, sel.detail).inner_text()

    def close():
        if page.url != url_before:  # navigated in place
            page.go_back()
            page.wait_for_load_state("networkidle")
            return
        closer = page.locator(sel.close_detail).first
        if closer.count() and closer.is_visible():
            closer.click()
        else:
            page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    return page, text, close


def count_posting_rows(page, sel) -> int:
    """Rows that contain a posting link (header and filler rows don't count)."""
    return page.locator(sel.row).filter(has=page.locator(sel.link)).count()


def wait_for_rows(page, sel, timeout_s: float = 15) -> int:
    deadline = time.time() + timeout_s
    while True:
        n = count_posting_rows(page, sel)
        if n or time.time() > deadline:
            return n
        page.wait_for_timeout(500)


def save_snapshot(page, debug_dir: Path, name: str = "postings-page") -> Path:
    """Save the page's HTML and a screenshot so selectors can be fixed."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
    return debug_dir


def scrape_postings(ctx, page, cfg: WaterlooWorksConfig, known_ids: set[str] = frozenset(),
                    log: Callable[[str], None] = print, ask: Callable[[str], bool] | None = None,
                    debug_dir: Path | None = None) -> Iterator[Job]:
    """Walk the postings table page by page and yield a Job per posting.

    If the configured page shows no job list (WaterlooWorks often lands on a dashboard
    where you first pick "For My Program" or run a search), ``ask`` lets the user open
    the list in the browser window, and we read whatever list they bring up.
    """
    sel = cfg.selectors
    page.goto(cfg.postings_url)
    page.wait_for_load_state("networkidle")
    if not wait_for_rows(page, sel):
        if debug_dir:
            log(f"  no job list found on {page.url}; saved a snapshot to {save_snapshot(page, debug_dir)}")
        if not ask or not ask(
            "I couldn't find a list of job postings on this WaterlooWorks page. In the WaterlooWorks "
            "browser window, open the list of postings you want (for example click \"For My Program\", "
            "or run a search) so the job table is showing. Click Yes once it's showing, or No to stop."
        ):
            return
        if not wait_for_rows(page, sel, timeout_s=20):
            if debug_dir:
                save_snapshot(page, debug_dir, "postings-page-after-you-navigated")
                log(f"  still no job rows found on {page.url}. Snapshots saved in {debug_dir}")
            return
        log(f"  reading the list at {page.url}")
    for page_no in range(1, cfg.max_pages + 1):
        rows = page.locator(sel.row).filter(has=page.locator(sel.link))
        n = rows.count()
        log(f"  page {page_no}: {n} postings")
        for i in range(n):
            row = rows.nth(i)
            link = row.locator(sel.link).first
            if not link.count():
                continue
            cells = [c.strip() for c in row.locator("td, th").all_inner_texts()]
            # the row's markup often carries the posting ID in a link, onclick or data-* attribute
            markup = row.evaluate("el => el.outerHTML.slice(0, 4000)") or ""
            pid = posting_id_from(cells, markup)
            if pid and f"ww:{pid}" in known_ids:
                continue
            try:
                detail_page, text, close = _open_detail(ctx, page, link, sel)
            except Exception as e:  # keep going; one broken row shouldn't stop the crawl
                log(f"  ! could not open row {i}: {e}")
                continue
            try:
                job = job_from_posting(text, cells, url=detail_page.url, posting_id=pid)
            finally:
                close()
            yield job
        nxt = page.locator(sel.next_page).first
        if not nxt.count() or not nxt.is_visible() or not nxt.is_enabled():
            break
        nxt.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
