"""Apply to a WaterlooWorks posting from your logged-in browser session.

The real WaterlooWorks flow (2026 UI):
1. A posting opens in a pop-up panel. Its Apply control is an icon button in the bottom
   toolbar (no visible text, just an "Apply" tooltip).
2. Apply opens a new tab, "Submit Application", a three-step wizard:
   a. Pre-Screening Questions (optional): dropdowns, then "Next". Questions asking for an
      8-month term are answered "No"; anything else is left for you to answer.
   b. Application Options: tick "Create Custom Application Package", then choose documents:
      Work History and Grade Report are usually preselected; the Résumé is picked by the name
      in settings; the tailored cover letter is uploaded with "Upload New Cover Letter".
   c. Submit -> Confirmation.
3. We always stop and ask before the final Submit unless auto_submit is on.

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


# ---- finding buttons -------------------------------------------------------------

# (label regex, exclude regex, icon ligatures that mean this action)
APPLY_WORDS = (r"\bapply\b", r"filter|search|applied|clear|reset|criteria", ["playlist_add"])
SUBMIT_WORDS = (r"^\s*submit(\s+application)?\s*$", r"search|filter", [])
NEXT_WORDS = (r"^\s*next\s*$", r"page", [])

# Marks visible clickables whose label (text, aria-label, title or tooltip attribute) matches,
# preferring ones inside a pop-up panel; returns their labels. Icon-only buttons are matched by
# their Material icon ligature (e.g. the posting toolbar's "playlist_add" = Apply).
_MARK_JS = """([inc, exc, icons, tag]) => {
    const re = new RegExp(inc, 'i'), ex = new RegExp(exc, 'i');
    document.querySelectorAll('[data-coop]').forEach(e => e.removeAttribute('data-coop'));
    const hits = [];
    for (const el of document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]')) {
        const r = el.getBoundingClientRect(), st = getComputedStyle(el);
        if (!r.width || !r.height || st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
        const sources = [el.innerText, el.value, el.getAttribute('aria-label'), el.title,
                         el.getAttribute('data-tooltip'), el.getAttribute('data-original-title'),
                         el.getAttribute('data-title'), el.getAttribute('data-bs-original-title')];
        const tipId = el.getAttribute('aria-describedby');
        if (tipId && document.getElementById(tipId)) sources.push(document.getElementById(tipId).innerText);
        const labels = sources.filter(Boolean).map(s => s.replace(/\\s+/g, ' ').trim()).filter(s => s && s.length <= 40);
        const text = (el.innerText || '').trim().toLowerCase();
        const icon = icons.includes(text);
        if (!icon && !labels.some(l => re.test(l) && !ex.test(l))) continue;
        const inPanel = !!el.closest('[role=dialog], [aria-modal=true], .modal, [class*=modal], [class*=drawer], [class*=posting]');
        hits.push({el, label: labels.join(' / '), inPanel, icon});
    }
    // a posting's own Apply lives in its pop-up panel (as an icon); a bare "Apply" elsewhere is often a filter
    const best = hits.some(h => h.inPanel) ? hits.filter(h => h.inPanel)
               : hits.some(h => h.icon) ? hits.filter(h => h.icon) : hits;
    best.forEach(h => h.el.setAttribute('data-coop', tag));
    return best.map(h => h.label);
}"""

_LABELS_JS = """() => [...document.querySelectorAll('button, a, [role=button], input[type=submit]')]
    .filter(el => { const r = el.getBoundingClientRect(); return r.width && r.height; })
    .map(el => [el.innerText, el.getAttribute('aria-label'), el.title, el.getAttribute('data-tooltip')]
        .filter(Boolean).map(s => s.replace(/\\s+/g, ' ').trim()).join(' / '))
    .filter(t => t && t.length <= 60)"""


def find_action(ctx, page, words, css: str = "", other_tabs: bool = True, skip=()):
    """Find a visible button by its label, in this page (and its frames) or other open tabs.

    ``skip``: tabs to ignore (ones that were already open before this application started,
    e.g. an unfinished form for another job). Returns (page, locator) or (None, None).
    """
    inc, exc, icons = words
    others = [x for x in reversed(ctx.pages) if x is not page and x not in skip] if other_tabs else []
    pages = [page] + others
    for p in pages:
        for frame in p.frames:
            try:
                if frame.evaluate(_MARK_JS, [inc, exc, icons, "hit"]):
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
        return list(dict.fromkeys(page.evaluate(_LABELS_JS)))[:40]
    except Exception:
        return []


def _page_with_apply_button(ctx, page, sel, skip=()):
    """This page, or a tab opened during this application, showing a posting's Apply button."""
    found, _ = find_action(ctx, page, APPLY_WORDS, sel.apply_button, skip=skip)
    return found


def _click_and_follow(ctx, page, locator, wait_ms: int = 6000):
    """Click; if that opens a new tab (as WaterlooWorks' Apply does), return the new tab."""
    before = len(ctx.pages)
    locator.click()
    for _ in range(wait_ms // 250):
        if len(ctx.pages) > before:
            new = ctx.pages[-1]
            new.wait_for_load_state()
            return new
        page.wait_for_timeout(250)
    page.wait_for_load_state("networkidle")
    return page


# ---- getting to the posting ------------------------------------------------------


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
                            debug_dir: Path | None = None, log=print, skip=()):
    """Get to the posting's own view. Postings that open in a modal have no URL of their own
    (job.url is the list page), so find the posting's row and open it - asking the user to
    bring the list or the posting up in the browser window if we can't see it."""
    from ..sources.waterlooworks import save_snapshot, wait_for_rows

    sel = cfg.selectors
    wait_for_rows(page, sel, timeout_s=5)
    rows = _matching_rows(page, sel, job)
    if not rows.count() and (found := _page_with_apply_button(ctx, page, sel, skip)):
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
        if not rows.count() and (found := _page_with_apply_button(ctx, page, sel, skip)):
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
    return _page_with_apply_button(ctx, page, sel, skip) or page


# ---- the Submit Application wizard -----------------------------------------------

_EIGHT_MONTHS = re.compile(r"\b(8|eight)[\s-]*months?\b", re.I)
_G_LICENSE = re.compile(r"\bfull\s+G\b|\bG\s*licen[cs]e|\bG\s*class|class\s*G\b|driv(er'?s?|ing)\s+licen[cs]e", re.I)


def prescreen_rules(cfg: WaterlooWorksConfig) -> list[tuple[re.Pattern, str]]:
    """Pre-screening questions we can answer for you, from the switches in Setup."""
    return [
        (_EIGHT_MONTHS, "Yes" if cfg.can_do_8_month_term else "No"),
        (_G_LICENSE, "Yes" if cfg.has_full_g_license else "No"),
    ]


def _pick_option(options: list[str], answer: str) -> str | None:
    """'No' matches 'No', 'No, I cannot', etc. - but not 'Not sure'."""
    for o in options:
        if re.match(rf"^\s*{re.escape(answer)}\b(?![\w'])", o, re.I):
            return o
    return None

# Every <select> on the page (including native ones hidden behind a styled dropdown), with the
# question text nearest to it: its <label>, else the closest ancestor that has text of its own.
_SELECTS_JS = """() => [...document.querySelectorAll('select')].map((s, i) => {
        s.setAttribute('data-coop-q', String(i));
        const byFor = s.id && document.querySelector(`label[for="${s.id}"]`);
        let q = byFor ? byFor.innerText : '';
        for (let n = s.parentElement; n && !q.trim(); n = n.parentElement) {
            q = (n.innerText || '');
            for (const o of s.options) q = q.replace(o.text, '');
        }
        const opt = s.options[s.selectedIndex];
        return {i, question: q.trim().slice(0, 300), selected: opt ? opt.text.trim() : '',
                options: [...s.options].map(o => o.text.trim())};
    })"""

# Set a select's value the way a person would, firing the events the page listens for.
_SET_SELECT_JS = """([i, label]) => {
    const s = document.querySelector(`select[data-coop-q="${i}"]`);
    const o = [...s.options].find(o => o.text.trim() === label);
    if (!o) return false;
    s.value = o.value; o.selected = true;
    for (const t of ['input', 'change', 'blur']) s.dispatchEvent(new Event(t, {bubbles: true}));
    return s.options[s.selectedIndex].text.trim() === label;
}"""


def _unanswered(selected: str) -> bool:
    return not selected or bool(re.fullmatch(r"[-\s]*(please\s+)?select\.*[-\s]*|[-\s]*", selected, re.I))


def answer_prescreening(page, log=print, rules=None) -> list[str]:
    """Answer the dropdowns we have rules for. Returns the questions still unanswered."""
    rules = rules if rules is not None else prescreen_rules(WaterlooWorksConfig())
    left = []
    for q in page.evaluate(_SELECTS_JS):
        if not _unanswered(q["selected"]):
            continue
        answer = next((a for rx, a in rules if rx.search(q["question"])), None)
        choice = _pick_option(q["options"], answer) if answer else None
        if choice and page.evaluate(_SET_SELECT_JS, [q["i"], choice]):
            log(f"  pre-screening: \"{q['question'][:70]}\" -> {choice}")
        else:
            left.append(q["question"][:120] or "(a question)")
    return left


def _on_application_options(page) -> bool:
    body = page.inner_text("body").lower()
    return "create custom application package" in body or "application package name" in body


def get_through_prescreening(ctx, page, cfg: WaterlooWorksConfig, confirm: Confirm, log=print) -> None:
    """Answer what we can, click Next, and hand over to the user if the page doesn't move on."""
    if _on_application_options(page):
        return
    left = answer_prescreening(page, log, prescreen_rules(cfg))
    if not left:
        _, nxt = find_action(ctx, page, NEXT_WORDS, other_tabs=False)
        if nxt is not None:
            nxt.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
    if _on_application_options(page):
        return
    ask = ("Please finish the pre-screening questions in the WaterlooWorks window"
           + (":\n- " + "\n- ".join(left) + "\n" if left else " ")
           + "and click Next there. Click Yes here once you see 'Application Options'.")
    if not confirm(ask):
        raise ApplyError("Stopped at the pre-screening questions.")
    page.wait_for_timeout(1000)
    if not _on_application_options(page):
        raise ApplyError("Still on the pre-screening page - nothing was submitted.")


def _click_text(page, text_rx: str) -> bool:
    loc = page.get_by_text(re.compile(text_rx, re.I)).filter(visible=True)
    if not loc.count():
        return False
    loc.first.click()
    return True


_PICK_RADIO_JS = """(name) => {
    // find the visible text node naming the document, then the radio in the same row
    const want = name.toLowerCase();
    const els = [...document.querySelectorAll('label, span, div, td, p, a')]
        .filter(e => e.children.length <= 3 && (e.innerText || '').toLowerCase().includes(want));
    els.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    for (const e of els) {
        let n = e;
        for (let up = 0; n && up < 5; up++, n = n.parentElement) {
            const radios = n.querySelectorAll('input[type=radio]');
            if (radios.length === 1) { radios[0].click(); return radios[0].checked; }
            if (radios.length > 1) break;
        }
    }
    return false;
}"""


def _upload_cover_letter_here(page, pdf: Path, doc_name: str, confirm: Confirm, log=print) -> None:
    """Use the package's "Upload New Cover Letter" button; ask the user if that doesn't work."""
    try:
        if not _click_text(page, r"^\s*upload new cover letter\s*$"):
            raise RuntimeError("no 'Upload New Cover Letter' button")
        page.wait_for_timeout(1500)
        file_input = page.locator("input[type=file]").last
        file_input.set_input_files(str(pdf))
        # a document name box, if the dialog has one
        for box in page.locator("[role=dialog] input[type=text]:visible, .modal input[type=text]:visible").all():
            if not box.input_value():
                box.fill(doc_name)
        _, save = find_action(page.context, page, (r"^\s*(upload|save|add|ok|confirm|attach)\b", r"new", []),
                              other_tabs=False)
        if save is not None:
            save.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        log(f"  attached cover letter {pdf.name}")
    except Exception as e:
        log(f"  ! couldn't attach the cover letter automatically ({str(e).splitlines()[0]})")
        if not confirm(
            "Please add the cover letter yourself: in the WaterlooWorks window click \"Upload New Cover Letter\" "
            f"and choose this file:\n{pdf.resolve()}\nClick Yes when it's attached (No to send without it)."
        ):
            log("  continuing without a cover letter")


def build_package(page, job: Job, cfg: WaterlooWorksConfig, cover_letter_pdf: Path | None,
                  confirm: Confirm, log=print) -> list[str]:
    """Fill the Application Options step. Returns a list of things the user should check."""
    to_check = []
    if not (page.evaluate(_PICK_RADIO_JS, "Create Custom Application Package")
            or _click_text(page, r"create custom application package")):
        to_check.append("tick 'Create Custom Application Package'")
    page.wait_for_timeout(1200)

    if cfg.resume_document:
        if page.evaluate(_PICK_RADIO_JS, cfg.resume_document):
            log(f"  résumé: {cfg.resume_document}")
        else:
            to_check.append(f"choose your résumé (couldn't find one named '{cfg.resume_document}')")
    else:
        to_check.append("choose your résumé (set its WaterlooWorks name in Setup to automate this)")
    # Work History and Grade Report come preselected when you have one of each; only override
    # them if you named specific documents in Setup.
    for doc in (cfg.transcript_document, cfg.work_history_document):
        if doc and not page.evaluate(_PICK_RADIO_JS, doc):
            to_check.append(f"choose '{doc}'")

    if cover_letter_pdf:
        _upload_cover_letter_here(page, cover_letter_pdf, f"CL - {job.company} - {job.title}"[:60], confirm, log)
    return to_check


def apply_to_posting(
    ctx,
    page,
    job: Job,
    cfg: WaterlooWorksConfig,
    cover_letter_pdf: Path | None,
    confirm: Confirm,
    auto_submit: bool = False,
    log=print,
    debug_dir: Path | None = None,
) -> bool:
    """Returns True if the application was submitted.

    Your tabs are left alone. Tabs that were already open when this application started
    (yours, or an unfinished form for another job) are never used, and the form tab this
    application opens is only closed once it has been submitted - if you stop, or something
    needs your attention, it stays open for you.
    """
    if not job.url:
        raise ApplyError("Posting has no URL recorded; re-run `coop-apply fetch --ww`.")
    already_open = set(ctx.pages)
    submitted = _apply_steps(ctx, page, job, cfg, cover_letter_pdf, confirm, auto_submit, log, debug_dir,
                             already_open)
    if submitted:  # tidy up the finished form
        for p in ctx.pages:
            if p not in already_open and p is not page:
                try:
                    p.close()
                except Exception:
                    pass
    return submitted


def _same_text(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
    return norm(a) in norm(b)


def _apply_steps(ctx, page, job, cfg, cover_letter_pdf, confirm, auto_submit, log, debug_dir,
                 already_open=()) -> bool:
    sel = cfg.selectors
    already_open = set(already_open) - {page}
    page.goto(job.url)
    page.wait_for_load_state("networkidle")
    page = _open_posting_if_listed(ctx, page, job, cfg, confirm, debug_dir, log, skip=already_open)

    # 1. Apply (icon button in the posting's toolbar) -> new "Submit Application" tab
    found_page, apply_btn = find_action(ctx, page, APPLY_WORDS, sel.apply_button, other_tabs=False)
    if apply_btn is not None:
        page = _click_and_follow(ctx, found_page, apply_btn)
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
        new_tabs = [p for p in ctx.pages if p not in already_open and p is not page]
        page = new_tabs[-1] if new_tabs else page
        page.wait_for_load_state()
    page.wait_for_timeout(1500)

    # The form names the posting ("Mapper (ID: 492254)"); make sure it's this job's form.
    if "submit application" in page.inner_text("body").lower() and not _same_text(job.title, page.inner_text("body")):
        raise ApplyError("The application form that opened is for a different posting - nothing was submitted.")

    # 2. Pre-screening questions (only some postings), then Next to Application Options
    get_through_prescreening(ctx, page, cfg, confirm, log)

    # 3. Application package
    to_check = build_package(page, job, cfg, cover_letter_pdf, confirm, log)
    if to_check:
        log("  please check in the browser: " + "; ".join(to_check))

    if not auto_submit and not confirm(
        f"Review the application for '{job.title}' at {job.company} in the WaterlooWorks window"
        + (" (please also: " + "; ".join(to_check) + ")" if to_check else "")
        + ". Submit it now?"
    ):
        log("  not submitted")
        return False

    # 4. Submit -> Confirmation
    _, submit_btn = find_action(ctx, page, SUBMIT_WORDS, sel.submit_button, other_tabs=False)
    if submit_btn is not None:
        submit_btn.click()
    else:
        log("  ! couldn't spot the Submit button. Buttons I can see: " + " | ".join(visible_labels(page)))
        if not confirm("I couldn't find the Submit button. Click it yourself in the WaterlooWorks window, "
                       "then click Yes here (or No to leave it unsubmitted)."):
            return False
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    body = page.inner_text("body").lower()
    if any(s in body for s in ("successfully", "has been submitted", "application submitted", "thank you")):
        return True
    return confirm("I couldn't confirm success from the page. Did the application go through?")
