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


APPLY_WORDS = (r"\bapply\b", r"filter|search|applied|clear|reset|criteria")
SUBMIT_WORDS = (r"\bsubmit\b", r"search|filter")

# Marks visible, short-labelled clickables whose label matches; returns their labels. Labels are
# matched as words rather than exactly, because WaterlooWorks buttons often include an icon's
# ligature text (e.g. "send Apply").
_MARK_JS = """([inc, exc, tag]) => {
    const re = new RegExp(inc, 'i'), ex = new RegExp(exc, 'i');
    document.querySelectorAll('[data-coop]').forEach(e => e.removeAttribute('data-coop'));
    const hits = [];
    for (const el of document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]')) {
        const r = el.getBoundingClientRect(), st = getComputedStyle(el);
        if (!r.width || !r.height || st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
        const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').replace(/\\s+/g, ' ').trim();
        if (!label || label.length > 40 || !re.test(label) || ex.test(label)) continue;
        const inPanel = !!el.closest('[role=dialog], [aria-modal=true], .modal, [class*=modal], [class*=drawer], [class*=posting]');
        hits.push({el, label, inPanel});
    }
    // a posting's own Apply lives in its pop-up panel; a bare "Apply" elsewhere is often a filter
    const best = hits.some(h => h.inPanel) ? hits.filter(h => h.inPanel) : hits;
    best.forEach(h => h.el.setAttribute('data-coop', tag));
    return best.map(h => h.label);
}"""

_LABELS_JS = """() => [...document.querySelectorAll('button, a, [role=button], input[type=submit]')]
    .filter(el => { const r = el.getBoundingClientRect(); return r.width && r.height; })
    .map(el => (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim())
    .filter(t => t && t.length <= 40)"""


def find_action(ctx, page, words: tuple[str, str], css: str = ""):
    """Find a visible button by label words, in this page, its frames, or other open tabs.

    Returns (page, locator) or (None, None). ``css`` (from config) is tried first.
    """
    pages = [page, *[x for x in reversed(ctx.pages) if x is not page]]
    for p in pages:
        for frame in p.frames:
            try:
                if frame.evaluate(_MARK_JS, [words[0], words[1], "hit"]):
                    return p, frame.locator("[data-coop=hit]").first
                if css:
                    loc = frame.locator(css).filter(visible=True)
                    if loc.count():
                        return p, loc.first
            except Exception:  # frame navigated away / detached
                continue
    return None, None


def visible_labels(page) -> list[str]:
    try:
        return list(dict.fromkeys(page.evaluate(_LABELS_JS)))[:30]
    except Exception:
        return []


def _page_with_apply_button(ctx, page, sel):
    """The open page (or tab) currently showing a posting's Apply button, if any."""
    found, _ = find_action(ctx, page, APPLY_WORDS, sel.apply_button)
    return found


def _matching_rows(page, sel, job: Job):
    """Rows for this posting: by WaterlooWorks ID when we have the real one, else by title (+ company)."""
    rows = page.locator(sel.row).filter(has=page.locator(sel.link))
    pid = job.id.split(":", 1)[1]
    if pid.isdigit():
        by_id = rows.filter(has_text=pid)
        if by_id.count():
            return by_id
    by_title = rows.filter(has_text=job.title)
    if by_title.count() > 1:
        narrowed = by_title.filter(has_text=job.company)
        if narrowed.count():
            return narrowed
    return by_title


def _open_posting_if_listed(ctx, page, job: Job, cfg: WaterlooWorksConfig, confirm: Confirm | None = None,
                            debug_dir: Path | None = None, log=print):
    """Get to the posting's own view. Postings that open in a modal have no URL of their own
    (job.url is the list page), so find the posting's row and open it - asking the user to
    bring the list or the posting up in the browser window if we can't see it."""
    from ..sources.waterlooworks import save_snapshot, wait_for_rows

    sel = cfg.selectors
    wait_for_rows(page, sel, timeout_s=5)
    rows = _matching_rows(page, sel, job)
    if not rows.count() and (found := _page_with_apply_button(ctx, page, sel)):
        return found  # the posting itself is already open
    if not rows.count():
        search = page.locator("input[type=search]:visible, input[placeholder*='search' i]:visible").first
        if search.count():
            search.fill(job.title)
            search.press("Enter")
            page.wait_for_load_state("networkidle")
            wait_for_rows(page, sel, timeout_s=8)
            rows = _matching_rows(page, sel, job)
    if not rows.count() and confirm and confirm(
        f"I can't see \"{job.title}\" ({job.company}) on this WaterlooWorks page. In the WaterlooWorks "
        "browser window, open that posting (or a job list that includes it, e.g. your search). "
        "Click Yes once it's showing."
    ):
        page.wait_for_timeout(1000)
        wait_for_rows(page, sel, timeout_s=10)
        rows = _matching_rows(page, sel, job)
        if not rows.count() and (found := _page_with_apply_button(ctx, page, sel)):
            return found  # they opened the posting itself
    if not rows.count():
        if debug_dir:
            save_snapshot(page, debug_dir, "apply-page")
            log(f"  saved a snapshot of the page to {debug_dir}")
        raise ApplyError(f"Couldn't find \"{job.title}\" on {page.url}.")
    before = len(ctx.pages)
    rows.first.locator(sel.link).first.click()
    page.wait_for_timeout(1500)
    if len(ctx.pages) > before:
        page = ctx.pages[-1]
        page.wait_for_load_state()
    return _page_with_apply_button(ctx, page, sel) or page


def apply_to_posting(
    ctx,
    page,
    job: Job,
    cfg: WaterlooWorksConfig,
    cover_letter_name: str | None,
    confirm: Confirm,
    auto_submit: bool = False,
    log=print,
    debug_dir: Path | None = None,
) -> bool:
    """Returns True if the application was submitted."""
    if not job.url:
        raise ApplyError("Posting has no URL recorded; re-run `coop-apply fetch --ww`.")
    sel = cfg.selectors
    page.goto(job.url)
    page.wait_for_load_state("networkidle")
    page = _open_posting_if_listed(ctx, page, job, cfg, confirm, debug_dir, log)

    found_page, apply_btn = find_action(ctx, page, APPLY_WORDS, sel.apply_button)
    if apply_btn is not None:
        page = found_page
        apply_btn.click()
    else:
        log("  ! couldn't spot the Apply button. Buttons I can see: " + " | ".join(visible_labels(page)))
        if debug_dir:
            from ..sources.waterlooworks import save_snapshot

            save_snapshot(page, debug_dir, "no-apply-button")
        if not confirm(
            f"I couldn't find the Apply button for \"{job.title}\". If the posting is open in the WaterlooWorks "
            "window and can be applied to, click its Apply button yourself, then click Yes here. "
            "(Click No if there's no Apply button - e.g. you already applied or it's closed.)"
        ):
            raise ApplyError("No Apply button found - already applied, closed, or it only accepts applications elsewhere.")
        page = ctx.pages[-1] if ctx.pages else page
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

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

    found_page, submit_btn = find_action(ctx, page, SUBMIT_WORDS, sel.submit_button)
    if submit_btn is not None:
        page = found_page
        submit_btn.click()
    else:
        log("  ! couldn't spot the Submit button. Buttons I can see: " + " | ".join(visible_labels(page)))
        if not confirm("I couldn't find the Submit button. Click it yourself in the WaterlooWorks window, "
                       "then click Yes here (or No to leave it unsubmitted)."):
            return False
    page.wait_for_load_state("networkidle")
    body = page.inner_text("body").lower()
    if any(s in body for s in ("successfully", "submitted", "application has been")):
        return True
    return confirm("Couldn't confirm success from the page. Did the application go through?")
