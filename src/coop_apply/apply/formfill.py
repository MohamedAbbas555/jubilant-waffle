"""Fill and submit application forms on company websites, with Claude reading each page.

Company career sites (Greenhouse, Lever, Ashby, Workday, SuccessFactors, iCIMS, custom
forms...) all differ, so instead of hard-coding each one, every step works like this:

1. Snapshot the page: every visible form field (in every frame) with its question text,
   type, options and current value, plus the buttons, headings and any error messages.
2. Claude plans the step from that snapshot and your facts: what to put in each field,
   then which button to press (Apply / Next / Submit) - or that it needs you (a sign-in or
   account, a CAPTCHA, a question it has no facts for).
3. We fill the fields, press the button, and repeat until the site confirms the
   application was received.

Claude only uses what you've told it (resume, contact details, the "facts for application
forms" box in Setup). It never creates accounts, types passwords or solves CAPTCHAs, and
it picks "prefer not to say" for demographic questions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from ..models import Job, ResumeProfile
from . import ApplyError, NeedsYou

Confirm = Callable[[str], bool]

# ---- page snapshot ----------------------------------------------------------------

# Collect the fields in one document. Every field gets data-coop-f="<prefix><n>" (radio and
# checkbox-group options also get data-coop-o="<option index>") so we can find it again.
_FIELDS_JS = r"""(prefix) => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const visible = el => {
        const r = el.getBoundingClientRect(), st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    };
    const byId = id => { try { return document.getElementById(id); } catch (e) { return null; } };
    const textOf = el => {
        // the element's own text without the text of form controls inside it
        const c = el.cloneNode(true);
        c.querySelectorAll('select, option, input, textarea, button, script, style').forEach(x => x.remove());
        return clean(c.innerText || c.textContent);
    };
    const labelOf = el => {
        let t = '';
        if (el.labels && el.labels.length) t = [...el.labels].map(l => textOf(l)).join(' ');
        const lb = el.getAttribute('aria-labelledby');
        if (!t && lb) t = lb.split(/\s+/).map(id => byId(id) ? textOf(byId(id)) : '').join(' ');
        if (!t) t = el.getAttribute('aria-label') || '';
        if (!t) {
            for (let n = el.parentElement, up = 0; n && up < 5 && !t; n = n.parentElement, up++) {
                const own = textOf(n);
                if (own && own.length <= 400) t = own;
            }
        }
        if (!t) t = el.placeholder || el.name || el.id || '';
        return clean(t).slice(0, 300);
    };
    const requiredOf = (el, label) => !!(el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label));
    document.querySelectorAll('[data-coop-f]').forEach(e => { e.removeAttribute('data-coop-f'); e.removeAttribute('data-coop-o'); });
    const fields = [];
    let n = 0;
    const newId = () => prefix + (n++);
    const seenGroups = new Set();
    const controls = document.querySelectorAll('input, textarea, select');
    for (const el of controls) {
        const type = (el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text') : el.tagName).toLowerCase();
        if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
        if (type === 'file') {  // upload inputs are often hidden behind a styled button
            const id = newId();
            el.setAttribute('data-coop-f', id);
            const label = labelOf(el);
            fields.push({id, type: 'file', label, required: requiredOf(el, label),
                         value: el.files && el.files.length ? `(attached: ${el.files[0].name})` : ''});
            continue;
        }
        if (type === 'radio' || type === 'checkbox') {
            const group = el.name ? [...document.querySelectorAll(`input[type=${type}]`)].filter(x => x.name === el.name) : [el];
            const key = type + ':' + (el.name || Math.random());
            if (el.name && seenGroups.has(key)) continue;
            seenGroups.add(key);
            const shown = group.filter(x => visible(x) || (x.labels && [...x.labels].some(visible)) || visible(x.parentElement));
            if (!shown.length) continue;
            const id = newId();
            const optionLabel = x => clean((x.labels && x.labels.length ? [...x.labels].map(textOf).join(' ') : '') ||
                                           x.getAttribute('aria-label') || x.value);
            const options = shown.map(optionLabel);
            shown.forEach((x, i) => { x.setAttribute('data-coop-f', id); x.setAttribute('data-coop-o', String(i)); });
            // the question: fieldset legend / group label, else the text around the whole group
            let q = '';
            const fs = el.closest('fieldset');
            if (fs && fs.querySelector('legend')) q = textOf(fs.querySelector('legend'));
            const rg = el.closest('[role=radiogroup], [role=group]');
            if (!q && rg) q = rg.getAttribute('aria-label') || (byId(rg.getAttribute('aria-labelledby') || '') ? textOf(byId(rg.getAttribute('aria-labelledby'))) : '');
            if (!q) {
                let anc = shown[0].parentElement;
                while (anc && !shown.every(x => anc.contains(x))) anc = anc.parentElement;
                for (let up = 0; anc && up < 3 && !q; anc = anc.parentElement, up++) {
                    let t = textOf(anc);
                    options.forEach(o => { if (o) t = t.replace(o, ''); });
                    t = clean(t);
                    if (t && t.length <= 400) q = t;
                }
            }
            if (type === 'checkbox' && shown.length === 1) {
                const label = clean(optionLabel(shown[0]) || q).slice(0, 300);
                fields.push({id, type: 'checkbox', label, required: requiredOf(shown[0], label), value: shown[0].checked ? 'true' : 'false'});
            } else {
                const checked = shown.filter(x => x.checked).map(optionLabel);
                fields.push({id, type: type === 'radio' ? 'radio' : 'checkbox_group', label: clean(q).slice(0, 300),
                             required: shown.some(x => requiredOf(x, q)), options, value: checked.join('; ')});
            }
            continue;
        }
        if (!visible(el) || el.disabled || el.readOnly) continue;
        const id = newId();
        el.setAttribute('data-coop-f', id);
        const label = labelOf(el);
        const f = {id, type, label, required: requiredOf(el, label)};
        if (type === 'select') {
            const opts = [...el.options].map(o => clean(o.text));
            f.options = opts.slice(0, 80);
            if (opts.length > 80) f.note = `${opts.length} options; type to search`;
            f.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
        } else {
            f.value = (el.value || '').slice(0, 200);
            if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) f.type = 'combobox';
            if (el.placeholder) f.placeholder = el.placeholder;
        }
        fields.push(f);
    }
    const buttons = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
        .filter(visible)
        .map(b => clean(b.innerText || b.value || b.getAttribute('aria-label') || ''))
        .filter(t => t && t.length <= 60);
    const headings = [...document.querySelectorAll('h1, h2, h3, legend')].filter(visible).map(h => clean(h.innerText)).filter(Boolean).slice(0, 15);
    const errors = [...document.querySelectorAll('[role=alert], [aria-invalid=true], .error, .errors, [class*=error-message], [class*=field-error]')]
        .filter(visible).map(e => clean(e.innerText || e.getAttribute('aria-label') || '')).filter(Boolean).slice(0, 15);
    const password = [...document.querySelectorAll('input[type=password]')].some(visible);
    const captcha = !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], .g-recaptcha, .h-captcha, .cf-turnstile');
    return {fields, buttons: [...new Set(buttons)].slice(0, 50), headings, errors, password, captcha,
            text: clean(document.body ? document.body.innerText : '').slice(0, 3500)};
}"""


@dataclass
class Snapshot:
    url: str
    title: str
    fields: list[dict]
    buttons: list[str]
    headings: list[str]
    errors: list[str]
    password: bool
    captcha: bool
    text: str
    frames: dict[str, object] = field(default_factory=dict)  # field id -> frame it lives in

    def for_prompt(self) -> str:
        data = {k: getattr(self, k) for k in ("url", "title", "headings", "errors", "buttons", "fields")}
        data["sign_in_form_on_page"] = self.password
        data["captcha_on_page"] = self.captcha
        data["page_text_excerpt"] = self.text
        return json.dumps(data, ensure_ascii=False, indent=1)

    def signature(self) -> str:
        return json.dumps([self.url, [(f["id"], f.get("value")) for f in self.fields], self.errors], sort_keys=True)


def snapshot(page) -> Snapshot:
    snap = Snapshot(url=page.url, title=_safe(lambda: page.title(), ""), fields=[], buttons=[], headings=[],
                    errors=[], password=False, captcha=False, text="")
    for i, frame in enumerate(page.frames):
        try:
            data = frame.evaluate(_FIELDS_JS, f"f{i}_")
        except Exception:  # cross-origin / detached frame
            continue
        for f in data["fields"]:
            snap.frames[f["id"]] = frame
        snap.fields += data["fields"]
        snap.buttons += [b for b in data["buttons"] if b not in snap.buttons]
        snap.headings += data["headings"]
        snap.errors += data["errors"]
        snap.password = snap.password or data["password"]
        snap.captcha = snap.captcha or data["captcha"]
        if i == 0 or (not snap.text and data["text"]):
            snap.text = data["text"]
    return snap


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


# ---- planning a step (Claude) -----------------------------------------------------


class FieldFill(BaseModel):
    field_id: str
    value: str = Field(description="Text to enter; for select/radio the exact option text; for a checkbox "
                                   "'true' or 'false'; for a checkbox group the exact option text to tick; for a "
                                   "file upload 'resume' or 'cover_letter'")


class StepPlan(BaseModel):
    page_kind: Literal["job_page", "form", "sign_in", "captcha", "confirmation", "error", "other"]
    fills: list[FieldFill]
    missing_info: list[str] = Field(description="Required questions on this page you could not answer from the facts")
    next_action: Literal["click", "submit", "needs_user", "done"]
    button_text: str = Field(description="Exact text of the button to press for click/submit, from the buttons list")
    note: str = Field(description="One short sentence on what this page is and what you did")


FORM_SYSTEM = """You fill in online job applications for a university co-op student, one page at a time.
You get the candidate's facts, the job, and a snapshot of the current page (its fields with ids,
question labels, types, options and current values; its buttons; headings; errors).

Rules for answers:
- Use ONLY the candidate facts given. Never invent employers, dates, grades, numbers, legal status,
  addresses or anything else. If a REQUIRED question can't be answered from the facts, list it in
  missing_info and don't fill it. Optional questions you can't answer: leave empty.
- Don't re-fill fields whose current value is already correct.
- For select/radio/checkbox-group fields, copy an option's text exactly.
- File uploads: a resume/CV field gets "resume", a cover letter field gets "cover_letter". If the
  current value already shows a file attached, leave it. Other required uploads (transcript,
  portfolio...) go in missing_info.
- If there is a cover letter text box, use the cover letter text provided.
- Short written questions (why this role/company, tell us about yourself, relevant experience):
  write 2-4 genuine first-person sentences grounded in the candidate's resume and the job.
- Demographic / EEO / voluntary self-identification / disability / veteran / gender / race
  questions: pick the "prefer not to say" / "decline to self-identify" style option, or leave optional
  ones empty if there is no such option.
- Required consent / acknowledgement / "information is accurate" / privacy-policy checkboxes: tick
  them. Marketing or "talent community" email opt-ins: leave unticked.
- Dates: follow the field's placeholder format.
- Never type into password fields, never create an account, never try to solve a CAPTCHA.

Choosing next_action:
- "click": go to the application / next page (e.g. "Apply", "Apply now", "Next", "Continue",
  "Save and continue"). Put the exact button text in button_text.
- "submit": only on the final step, when every required field is answered. button_text = the
  submit button's exact text (e.g. "Submit application").
- "needs_user": the page needs the candidate: a sign-in or account creation (password fields), a
  CAPTCHA, a required question or upload you have no facts for, or you are stuck.
- "done": the page confirms the application was received.
If the page shows validation errors, fix the fields they point to before continuing."""


def candidate_block(profile: ResumeProfile, applicant, facts: str, switches: dict[str, bool]) -> str:
    contact = {k: v for k, v in vars(applicant).items() if v}
    contact["full_name"] = applicant.full_name
    extra = "\n".join(f"- {k}: {'Yes' if v else 'No'}" for k, v in switches.items())
    return (
        f"<candidate_resume_profile>\n{profile.model_dump_json(indent=1)}\n</candidate_resume_profile>\n"
        f"<candidate_contact>\n{json.dumps(contact, indent=1)}\n</candidate_contact>\n"
        f"<candidate_facts>\n{facts.strip()}\n{extra}\n</candidate_facts>"
    )


def make_planner(llm, profile: ResumeProfile, applicant, facts: str, switches: dict[str, bool],
                 effort: str = "medium") -> Callable[[Snapshot, Job, str], StepPlan]:
    system = FORM_SYSTEM + "\n\n" + candidate_block(profile, applicant, facts, switches)

    def plan(snap: Snapshot, job: Job, cover_letter_text: str) -> StepPlan:
        content = (
            f"<job>\n{job.title} at {job.company} ({job.location})\n{job.description[:1500]}\n</job>\n"
            f"<cover_letter_text>\n{cover_letter_text or '(none)'}\n</cover_letter_text>\n"
            f"<page_snapshot>\n{snap.for_prompt()}\n</page_snapshot>\n"
            "Plan this step."
        )
        return llm.structured(system=system, content=content, schema=StepPlan, effort=effort, cache_system=True)

    return plan


class PrescreenAnswer(BaseModel):
    answer: str = Field(description="The exact option text to pick, or UNKNOWN if the facts don't answer it")
    reason: str


def make_answerer(llm, profile: ResumeProfile, applicant, facts: str, switches: dict[str, bool]):
    """For WaterlooWorks pre-screening dropdowns we have no rule for."""
    system = (
        "You answer a job application's pre-screening question for a co-op student, using ONLY the facts below. "
        "Pick one of the given options, copying its text exactly. If the facts don't clearly answer the "
        "question, answer UNKNOWN - never guess.\n\n" + candidate_block(profile, applicant, facts, switches)
    )

    def answer(question: str, options: list[str]) -> str | None:
        real = [o for o in options if o and not re.fullmatch(r"[-\s]*(please\s+)?select\.*[-\s]*", o, re.I)]
        if not real:
            return None
        res = llm.structured(system=system, content=f"Question: {question}\nOptions: {json.dumps(real)}",
                             schema=PrescreenAnswer, effort="low", cache_system=True)
        return res.answer if res.answer in real else None

    return answer


# ---- doing a step -----------------------------------------------------------------


def best_option(options: list[str], value: str) -> int | None:
    """Index of the option matching `value` (exact, then case-insensitive, then prefix/contains)."""
    norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().lower())  # noqa: E731
    v = norm(value)
    if not options or not v:
        return None
    tests = (
        lambda o: o == v,
        lambda o: bool(o) and (o.startswith(v) or v.startswith(o)),
        lambda o: bool(o) and v in o,
        lambda o: bool(o) and len(o) > 2 and o in v,
    )
    for test in tests:
        for i, o in enumerate(options):
            if test(norm(o)):
                return i
    return None


_TRUE = {"true", "yes", "y", "1", "checked", "on", "tick", "ticked"}


def apply_fill(snap: Snapshot, fill: FieldFill, files: dict[str, Path | None], log=print) -> bool:
    f = next((x for x in snap.fields if x["id"] == fill.field_id), None)
    frame = snap.frames.get(fill.field_id)
    if f is None or frame is None:
        return False
    loc = frame.locator(f'[data-coop-f="{fill.field_id}"]')
    kind, value = f["type"], fill.value
    try:
        if kind == "file":
            path = files.get(value.strip().lower())
            if not path or f.get("value"):
                return False
            loc.first.set_input_files(str(path))
        elif kind == "select":
            i = best_option(f.get("options", []), value)
            if i is None:
                return False
            loc.first.select_option(index=i)
        elif kind in ("radio", "checkbox_group"):
            i = best_option(f.get("options", []), value)
            if i is None:
                return False
            opt = frame.locator(f'[data-coop-f="{fill.field_id}"][data-coop-o="{i}"]').first
            if not opt.evaluate("e => e.checked"):
                opt.evaluate("e => (e.labels && e.labels[0] ? e.labels[0] : e).click()")
                if not opt.evaluate("e => e.checked"):
                    opt.evaluate("e => e.click()")
        elif kind == "checkbox":
            want = value.strip().lower() in _TRUE
            box = loc.first
            if box.evaluate("e => e.checked") != want:
                box.evaluate("e => (e.labels && e.labels[0] ? e.labels[0] : e).click()")
                if box.evaluate("e => e.checked") != want:
                    box.evaluate("e => e.click()")
        elif kind == "combobox":
            box = loc.first
            box.click()
            box.fill(value)
            frame.wait_for_timeout(700)
            option = frame.locator("[role=option]").filter(has_text=re.compile(re.escape(value), re.I)).first
            if option.count():
                option.click()
            else:
                box.press("Enter")
        else:
            loc.first.fill(value)
        return True
    except Exception as e:
        log(f"    ! couldn't fill '{f['label'][:60]}': {str(e).splitlines()[0][:120]}")
        return False


_CLICK_JS = r"""([want, tag]) => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const w = clean(want);
    document.querySelectorAll('[data-coop-b]').forEach(e => e.removeAttribute('data-coop-b'));
    const els = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
        .filter(el => { const r = el.getBoundingClientRect(); return r.width && r.height && !el.disabled; });
    const label = el => clean(el.innerText || el.value || el.getAttribute('aria-label') || '');
    const pick = els.find(el => label(el) === w) || els.find(el => label(el).startsWith(w)) ||
                 els.find(el => w && label(el).includes(w));
    if (!pick) return false;
    pick.setAttribute('data-coop-b', tag);
    return true;
}"""


def click_button(ctx, page, text: str):
    """Click the button with this text (in any frame). Returns the page to continue on, or None."""
    for frame in page.frames:
        try:
            if frame.evaluate(_CLICK_JS, [text, "go"]):
                before = len(ctx.pages)
                frame.locator('[data-coop-b="go"]').first.click()
                for _ in range(16):  # a link may open the form in a new tab
                    if len(ctx.pages) > before:
                        new = ctx.pages[-1]
                        new.wait_for_load_state()
                        return new
                    page.wait_for_timeout(250)
                _safe(lambda: page.wait_for_load_state("networkidle", timeout=15000), None)
                return page
        except Exception:
            continue
    return None


SUCCESS_RX = re.compile(
    r"thank(s| you) for (applying|your application|your interest)|application (has been |was )?(submitted|received)"
    r"|we('ve| have) received your application|successfully (submitted|applied)",
    re.I,
)


def fill_and_submit(ctx, page, job: Job, plan_step, files: dict[str, Path | None], cover_letter_text: str,
                    confirm: Confirm, auto: bool = False, log=print, prefill=None, max_steps: int = 12) -> bool:
    """Work through the application, page by page. True once the site confirms it was submitted.

    auto=True: submit without asking; anything that needs you raises NeedsYou.
    Otherwise you confirm before the final submit, and are asked to handle sign-ins etc.
    """
    url = job.apply_url or job.url
    if not url:
        raise ApplyError("This job has no application link.")
    page.goto(url)
    _safe(lambda: page.wait_for_load_state("networkidle", timeout=20000), None)
    last_sig, repeats, submitted_once = None, 0, False
    for step in range(1, max_steps + 1):
        page.wait_for_timeout(1500)
        if prefill:
            _safe(lambda: prefill(page), None)
        snap = snapshot(page)
        if submitted_once and SUCCESS_RX.search(snap.text):
            log("  the site confirmed your application was received")
            return True
        sig = snap.signature()
        repeats = repeats + 1 if sig == last_sig else 0
        last_sig = sig
        if repeats >= 2:
            _needs(f"the page isn't moving on ({(snap.errors or ['no error shown'])[0][:100]})", auto, confirm, log)
            last_sig, repeats = None, 0
            continue

        plan = plan_step(snap, job, cover_letter_text)
        log(f"  step {step}: {plan.note}")
        n = sum(apply_fill(snap, f, files, log) for f in plan.fills)
        if n:
            log(f"    filled {n} field{'s' if n != 1 else ''}")
        if plan.next_action == "done" or plan.page_kind == "confirmation":
            return True
        if plan.next_action == "needs_user" or plan.page_kind in ("sign_in", "captcha") or \
                (plan.next_action == "submit" and plan.missing_info):
            why = {"sign_in": "sign in or create an account", "captcha": "complete the CAPTCHA"}.get(plan.page_kind)
            why = why or ("answer: " + "; ".join(plan.missing_info) if plan.missing_info else plan.note)
            _needs(why, auto, confirm, log)
            last_sig, repeats = None, 0
            continue
        if plan.next_action == "submit":
            if not auto and not confirm(
                f"The application for '{job.title}' at {job.company} is filled in - please review it in the browser "
                "window. Submit it now?"
            ):
                log("  not submitted (the form is left open for you)")
                return False
            submitted_once = True
        nxt = click_button(ctx, page, plan.button_text)
        if nxt is None:
            _needs(f"press \"{plan.button_text}\" (I couldn't find that button)", auto, confirm, log)
            continue
        page = nxt
    _needs("finish the application (it took more steps than expected)", auto, confirm, log)
    return confirm("Did the application go through?") if not auto else False


def _needs(what: str, auto: bool, confirm: Confirm, log) -> None:
    if auto:
        raise NeedsYou(f"Needs you: {what}")
    log(f"  ! needs you: {what}")
    if not confirm(f"This application needs you to {what}. Do that in the browser window, then click Yes "
                   "and I'll carry on (No to stop)."):
        raise ApplyError(f"Stopped - needed you to {what}.")
