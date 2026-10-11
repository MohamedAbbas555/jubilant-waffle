"""Apply to a WaterlooWorks posting from your logged-in browser session.

Flow (WaterlooWorks "application package"):
1. Upload the generated cover letter PDF to your WaterlooWorks Documents.
2. Open the posting and click Apply.
3. Create a new application package, choosing your resume, the new cover letter and
   any other required documents (grade report, work history) by name.
4. Stop for your confirmation before clicking the final Submit, unless auto_submit is on.

Postings whose Application Method is "Through Employer Website" can't be applied to
inside WaterlooWorks; those are handed to the external applier instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from ..config import WaterlooWorksConfig
from ..models import Job

Confirm = Callable[[str], bool]


class ApplyError(RuntimeError):
    pass


def needs_employer_website(job: Job) -> bool:
    method = job.extra.get("Application Method", "").lower()
    return "employer website" in method or (bool(job.apply_url) and "waterlooworks" not in job.apply_url)


def required_documents(job: Job) -> list[str]:
    raw = job.extra.get("Application Documents Required", "")
    return [d.strip() for d in re.split(r"[,\n]", raw) if d.strip()]


def document_choices(job: Job, cfg: WaterlooWorksConfig, cover_letter_name: str | None) -> dict[str, str]:
    """Map a document-type keyword (as it appears in a select's label) to the doc name to choose."""
    required = " ".join(required_documents(job)).lower()
    choices = {}
    if cfg.resume_document:
        choices["r[ée]sum[ée]"] = cfg.resume_document
    if cover_letter_name and ("cover" in required or not required):
        choices["cover letter"] = cover_letter_name
    if cfg.transcript_document and ("grade" in required or "transcript" in required):
        choices["grade report|transcript"] = cfg.transcript_document
    if cfg.work_history_document and "work history" in required:
        choices["work history"] = cfg.work_history_document
    return choices


def upload_cover_letter(page, cfg: WaterlooWorksConfig, pdf_path: Path, doc_name: str, log=print):
    """Upload a PDF to WaterlooWorks Documents as a Cover Letter."""
    sel = cfg.selectors
    page.goto(cfg.documents_url)
    page.wait_for_load_state("networkidle")
    page.locator(sel.upload_button).first.click()
    page.wait_for_timeout(800)
    # Document type dropdown -> "Cover Letter"
    for select in page.locator("select").all():
        options = [o.strip() for o in select.locator("option").all_inner_texts()]
        match = next((o for o in options if "cover letter" in o.lower()), None)
        if match:
            select.select_option(label=match)
            break
    name_input = page.locator(
        "input[name*='docName' i], input[name*='name' i]:not([type=hidden]), input[placeholder*='name' i]"
    ).first
    name_input.fill(doc_name)
    page.locator("input[type=file]").first.set_input_files(str(pdf_path))
    page.locator(
        "button:has-text('Upload'):visible, button:has-text('Save'):visible, input[type=submit]:visible"
    ).last.click()
    page.wait_for_load_state("networkidle")
    log(f"  uploaded cover letter to WaterlooWorks as '{doc_name}'")


def _select_by_label(page, pattern: str, wanted: str) -> bool:
    """Choose `wanted` in the <select> whose label/nearby text matches `pattern`."""
    rx = re.compile(pattern, re.I)
    for select in page.locator("select:visible").all():
        label = select.evaluate(
            """el => {
                const byFor = el.id && document.querySelector(`label[for="${el.id}"]`);
                if (byFor) return byFor.innerText;
                const row = el.closest('tr, .form-group, .field, div');
                return row ? row.innerText : (el.name || '');
            }"""
        )
        if not rx.search(label or ""):
            continue
        options = [o.strip() for o in select.locator("option").all_inner_texts()]
        match = next((o for o in options if wanted.lower() in o.lower()), None)
        if match:
            select.select_option(label=match)
            return True
    return False


def _open_posting_if_listed(ctx, page, job: Job, cfg: WaterlooWorksConfig):
    """Postings that open in a modal have no URL of their own; job.url is then the list
    page, so find the row with the posting ID (searching for it if needed) and open it."""
    sel = cfg.selectors
    if page.locator(sel.apply_button).filter(visible=True).count():
        return page
    pid = job.id.split(":", 1)[1]
    rows = page.locator(sel.row).filter(has_text=pid)
    if not rows.count():
        search = page.locator("input[type=search]:visible, input[placeholder*='search' i]:visible").first
        if search.count():
            search.fill(pid)
            search.press("Enter")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            rows = page.locator(sel.row).filter(has_text=pid)
    if not rows.count():
        raise ApplyError(f"Couldn't find posting {pid} on {page.url}.")
    before = len(ctx.pages)
    rows.first.locator(sel.link).first.click()
    page.wait_for_timeout(1500)
    if len(ctx.pages) > before:
        page = ctx.pages[-1]
        page.wait_for_load_state()
    return page


def apply_to_posting(
    ctx,
    page,
    job: Job,
    cfg: WaterlooWorksConfig,
    cover_letter_name: str | None,
    confirm: Confirm,
    auto_submit: bool = False,
    log=print,
) -> bool:
    """Returns True if the application was submitted."""
    if not job.url:
        raise ApplyError("Posting has no URL recorded; re-run `coop-apply fetch --ww`.")
    sel = cfg.selectors
    page.goto(job.url)
    page.wait_for_load_state("networkidle")
    page = _open_posting_if_listed(ctx, page, job, cfg)

    apply_btn = page.locator(sel.apply_button).filter(visible=True).first
    if not apply_btn.count():
        raise ApplyError("No Apply button found - already applied, closed, or selectors need updating.")
    apply_btn.click()
    page.wait_for_load_state("networkidle")

    # Prefer building a fresh package so the tailored cover letter is used.
    new_pkg = page.get_by_label(re.compile("create a new application package|new package", re.I))
    if new_pkg.count():
        new_pkg.first.check()
    pkg_name = page.locator("input[name*='package' i]:visible, input[name*='pkg' i]:visible").first
    if pkg_name.count():
        pkg_name.fill(f"{job.company} - {job.title}"[:80])

    missing = []
    for pattern, doc in document_choices(job, cfg, cover_letter_name).items():
        if not _select_by_label(page, pattern, doc):
            missing.append(f"{pattern} -> {doc}")
    if missing:
        log("  ! could not auto-select: " + "; ".join(missing) + " (select them in the browser)")

    if not auto_submit and not confirm(
        f"Review the application for '{job.title}' at {job.company} in the browser. Submit it now?"
    ):
        log("  not submitted")
        return False

    page.locator(sel.submit_button).first.click()
    page.wait_for_load_state("networkidle")
    body = page.inner_text("body").lower()
    if any(s in body for s in ("successfully", "submitted", "application has been")):
        return True
    return confirm("Couldn't confirm success from the page. Did the application go through?")
