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
import time
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
    const idsText = ids => clean((ids || '').split(/\s+/).map(id => byId(id) ? textOf(byId(id)) : '').join(' '));
    const labelOf = el => {
        let t = '';
        if (el.labels && el.labels.length) t = [...el.labels].map(l => textOf(l)).join(' ');
        if (!t) t = idsText(el.getAttribute('aria-labelledby'));
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
    // Upload widgets: the question ("Resume/CV") is usually a <label> or heading in the field's
    // container, next to helper text ("Attach, Dropbox... Accepted file types") we must not use.
    const fileLabelOf = el => {
        if (el.labels && el.labels.length) { const t = [...el.labels].map(textOf).join(' '); if (t) return clean(t).slice(0, 300); }
        const own = idsText(el.getAttribute('aria-labelledby')) || el.getAttribute('aria-label');
        if (own) return clean(own).slice(0, 300);
        let near = '';
        for (let n = el.parentElement, up = 0; n && up < 6; n = n.parentElement, up++) {
            const t = idsText(n.getAttribute('aria-labelledby'));
            if (t) return t.slice(0, 300);
            const others = [...n.querySelectorAll('input, select, textarea')].filter(x => x !== el && x.type !== 'hidden');
            if (others.length) break;  // left this upload's own box
            const lab = [...n.querySelectorAll('label, legend, h1, h2, h3, h4, h5, [class*=label i]')]
                .find(l => textOf(l) && !(l.htmlFor && l.htmlFor !== el.id));
            if (lab) return textOf(lab).slice(0, 300);
            const own = textOf(n);
            if (!near && own && own.length <= 300) near = own;
        }
        return near || labelOf(el);
    };
    // The text right beside a control ('next' or 'prev'), skipping blanks and comments
    // (React leaves <!-- --> markers between an input and its text).
    const besideText = (x, dir) => {
        const step = s => dir === 'prev' ? s.previousSibling : s.nextSibling;
        let s = step(x);
        while (s && (s.nodeType === 8 || (s.nodeType === 3 && !clean(s.textContent)))) s = step(s);
        if (!s) return '';
        if (s.nodeType === 3) return clean(s.textContent);
        if (s.nodeType !== 1 || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(s.tagName) ||
            s.querySelector('input, select, textarea')) return '';
        return textOf(s);
    };
    const hasOwnLabel = x => !!((x.labels && x.labels.length) || x.getAttribute('aria-labelledby') || x.getAttribute('aria-label'));
    // A radio/checkbox option's own text: its label, aria-labelledby/aria-label, else the text
    // beside it (on the side `dir` that every option in the group has text on); the value
    // ("on", "1") only as a last resort.
    const ownLabel = (x, dir) => {
        if (x.labels && x.labels.length) { const t = [...x.labels].map(textOf).join(' '); if (t) return t; }
        const lb = idsText(x.getAttribute('aria-labelledby'));
        if (lb) return lb;
        if (x.getAttribute('aria-label')) return clean(x.getAttribute('aria-label'));
        if (dir !== 'none') {
            const t = besideText(x, dir || 'next');
            if (t && t.length <= 200) return t;
        }
        const p = x.parentElement;
        if (p && p.querySelectorAll('input, select, textarea').length === 1) {
            const t = textOf(p);
            if (t && t.length <= 200) return t;
        }
        return '';
    };
    const requiredOf = (el, label) => !!(el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label));
    // the browser itself refuses to submit a form with these (unless the form has novalidate);
    // a newsletter box in the footer or a header search doesn't count
    const chromeEl = el => !!el.closest('nav, header, footer, [role=search], [role=navigation], [role=banner], [role=contentinfo]');
    const blocked = el => !!(el.willValidate && el.validity && !el.validity.valid && !(el.form && el.form.noValidate)) && !chromeEl(el);
    const flagged = el => el.getAttribute('aria-invalid') === 'true';
    document.querySelectorAll('[data-coop-f]').forEach(e => { e.removeAttribute('data-coop-f'); e.removeAttribute('data-coop-o'); });
    const fields = [], invalid = [];
    let n = 0;
    const newId = () => prefix + (n++);
    const seenGroups = new Set();
    const controls = document.querySelectorAll('input, textarea, select, button[aria-haspopup=listbox], [role=button][aria-haspopup=listbox]');
    for (const el of controls) { try {  // one odd control mustn't blank the whole page
        const tag = el.tagName;
        const popup = el.getAttribute('aria-haspopup') === 'listbox' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag);
        const type = popup ? 'listbox' : (tag === 'INPUT' ? (el.getAttribute('type') || 'text') : tag).toLowerCase();
        if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
        if (type === 'file') {  // upload inputs are often hidden behind a styled button
            const id = newId();
            el.setAttribute('data-coop-f', id);
            const label = fileLabelOf(el);
            const f = {id, type: 'file', label, required: requiredOf(el, label),
                       value: el.files && el.files.length ? `(attached: ${el.files[0].name})` : ''};
            const hint = clean(el.name || el.id);
            if (hint) f.name = hint.slice(0, 60);
            fields.push(f);
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
            const unlabeled = shown.filter(x => !hasOwnLabel(x));
            const dir = !unlabeled.length || unlabeled.every(x => besideText(x, 'next')) ? 'next'
                      : unlabeled.every(x => besideText(x, 'prev')) ? 'prev' : 'none';  // "Yes (o) No (o)" layouts
            const optionLabel = x => clean(ownLabel(x, dir)) || clean(x.value);
            const options = shown.map(optionLabel);
            shown.forEach((x, i) => { x.setAttribute('data-coop-f', id); x.setAttribute('data-coop-o', String(i)); });
            // the question: fieldset legend / group label, else the text around the whole group
            let q = '';
            const fs = el.closest('fieldset');
            if (fs && fs.querySelector('legend')) q = textOf(fs.querySelector('legend'));
            const rg = el.closest('[role=radiogroup], [role=group]');
            if (!q && rg) q = rg.getAttribute('aria-label') || idsText(rg.getAttribute('aria-labelledby'));
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
            let label, f;
            if (type === 'checkbox' && shown.length === 1) {
                label = clean(ownLabel(shown[0], dir) || q || shown[0].value).slice(0, 300);
                f = {id, type: 'checkbox', label, required: requiredOf(shown[0], label), value: shown[0].checked ? 'true' : 'false'};
            } else {
                label = clean(q).slice(0, 300);
                const checked = shown.filter(x => x.checked).map(optionLabel);
                f = {id, type: type === 'radio' ? 'radio' : 'checkbox_group', label,
                     required: shown.some(x => requiredOf(x, q)), options, value: checked.join('; ')};
            }
            if (shown.some(flagged)) f.flagged = true;
            fields.push(f);
            if (shown.some(blocked)) invalid.push((label || options[0] || '').slice(0, 100));
            continue;
        }
        if (!visible(el) || el.disabled) continue;
        const id = newId();
        el.setAttribute('data-coop-f', id);
        const label = labelOf(el);
        const f = {id, type, label, required: requiredOf(el, label)};
        if (type === 'listbox') {  // Workday-style "Select One" dropdown button
            f.type = 'combobox';
            f.pick_only = true;
            f.value = clean(el.innerText || el.value || '').slice(0, 200);
        } else if (type === 'select') {
            f.options = [...el.options].map(o => clean(o.text));
            f.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
        } else {
            f.value = (el.value || '').slice(0, 200);
            const combo = el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete');
            if (combo) f.type = 'combobox';
            if (el.readOnly) {
                if (combo) f.pick_only = true;
                else f.type = 'readonly';  // e.g. a date picker - we can't type into it
            }
            if (el.placeholder) f.placeholder = el.placeholder;
        }
        if (flagged(el)) f.flagged = true;
        if (blocked(el)) invalid.push(label.slice(0, 100));
        fields.push(f);
    } catch (e) { /* skip it */ } }
    const buttons = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
        .filter(b => visible(b) && b.getAttribute('aria-haspopup') !== 'listbox')
        .map(b => clean(b.innerText || b.value || b.getAttribute('aria-label') || ''))
        .filter(t => t && t.length <= 60);
    const headings = [...document.querySelectorAll('h1, h2, h3, legend')].filter(visible).map(h => clean(h.innerText)).filter(Boolean).slice(0, 15);
    const errors = [...document.querySelectorAll('[role=alert], [aria-invalid=true], .error, .errors, [class*=error-message], [class*=field-error]')]
        .filter(visible).map(e => clean(e.innerText || e.getAttribute('aria-label') || '')).filter(Boolean).slice(0, 15);
    const password = [...document.querySelectorAll('input[type=password]')].some(visible);
    // Only a CAPTCHA you'd actually have to do counts. Invisible reCAPTCHA / hCaptcha and the
    // "protected by reCAPTCHA" badge are on most application forms and need nothing from you.
    const onScreen = el => { const r = el.getBoundingClientRect(); return visible(el) && r.bottom > 0 && r.right > 0; };  // hidden ones sit at -10000px
    const frames = [...document.querySelectorAll('iframe')].filter(onScreen).map(f => f.src || f.getAttribute('src') || '');
    const solved = [...document.querySelectorAll('textarea[name=g-recaptcha-response], textarea[name=h-captcha-response], input[name=cf-turnstile-response]')]
        .some(x => x.value);
    let captcha = '';
    if (frames.some(src => /recaptcha\/.*bframe/.test(src) || /hcaptcha.*frame=challenge/.test(src))) captcha = 'a CAPTCHA challenge is open on screen';
    else if (!solved && frames.some(src => (/recaptcha\/.*anchor/.test(src) && !/size=invisible/.test(src)) ||
                                           /hcaptcha.*frame=checkbox(?!-invisible)/.test(src) ||
                                           /challenges\.cloudflare\.com/.test(src)))
        captcha = 'an "I\'m not a robot" CAPTCHA box is waiting to be ticked';
    return {fields, invalid, buttons: [...new Set(buttons)].slice(0, 50), headings, errors, password, captcha,
            text: clean(document.body ? document.body.innerText : '').slice(0, 12000)};
}"""


MAX_OPTIONS_SHOWN = 300  # longer dropdowns are cut for Claude, but matched against every option


@dataclass
class Snapshot:
    url: str
    title: str
    fields: list[dict]
    buttons: list[str]
    headings: list[str]
    errors: list[str]
    password: bool
    captcha: str  # "" or what's on screen ("a CAPTCHA challenge is open...")
    text: str  # for Claude: the page's text (and the text of any frame holding the form)
    invalid: list[str] = field(default_factory=list)  # fields the browser won't submit as they are
    all_text: str = ""  # every frame's text, to spot a confirmation message
    frame_urls: list[str] = field(default_factory=list)
    frames: dict[str, object] = field(default_factory=dict)  # field id -> frame it lives in

    def for_prompt(self) -> str:
        data = {k: getattr(self, k) for k in ("url", "title", "headings", "errors", "buttons")}
        fields = []
        for f in self.fields:
            opts = f.get("options")
            if opts and len(opts) > MAX_OPTIONS_SHOWN:
                f = {**f, "options": opts[:MAX_OPTIONS_SHOWN],
                     "note": f"{len(opts)} options, only the first {MAX_OPTIONS_SHOWN} shown - give the exact option "
                             "text and it will be found in the full list"}
            fields.append(f)
        data["fields"] = fields
        if self.invalid:
            data["fields_the_browser_says_are_missing_or_invalid"] = self.invalid
        data["sign_in_form_on_page"] = self.password
        data["captcha_on_page"] = self.captcha or "no"
        data["page_text_excerpt"] = self.text
        return json.dumps(data, ensure_ascii=False, indent=1)

    def signature(self) -> str:
        """Changes whenever the page moves on (new step, new text, new errors)."""
        return json.dumps([self.url, self.frame_urls, [(f["id"], f["label"], f.get("value")) for f in self.fields],
                           self.errors, self.invalid, self.headings, self.buttons, hash(self.all_text)], sort_keys=True)

    def form_key(self) -> str:
        """Which form this is (its questions, not their answers)."""
        return json.dumps(sorted((f["type"], f["label"]) for f in self.fields))

_stuck_frames: dict[int, float] = {}  # id(frame) -> when to try it again


def frame_eval(frame, js: str, arg=None, timeout_ms: float = 2500):
    """frame.evaluate with a time limit. A frame that never finishes loading (a stuck ad or
    tracker iframe) would otherwise hang it forever; such a frame is skipped for 30 s."""
    key = id(frame)
    if _stuck_frames.get(key, 0) > time.monotonic():
        raise TimeoutError("frame not ready")
    try:
        return frame.locator(":root").evaluate(f"(_root, arg) => ({js})(arg)", arg, timeout=timeout_ms)
    except Exception as e:
        # only child frames (ads, trackers) are set aside; the page itself may just be busy
        if frame.parent_frame is not None and ("Timeout" in type(e).__name__ or "Timeout" in str(e)[:100]):
            _stuck_frames[key] = time.monotonic() + 30
        raise


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def snapshot(page) -> Snapshot:
    snap = Snapshot(url=page.url, title=_safe(lambda: page.title(), ""), fields=[], buttons=[], headings=[],
                    errors=[], password=False, captcha="", text="")
    texts, main_text = [], ""
    for i, frame in enumerate(page.frames):
        try:
            data = frame_eval(frame, _FIELDS_JS, f"f{i}_")
        except Exception:  # detached / not loaded
            continue
        for f in data["fields"]:
            snap.frames[f["id"]] = frame
        snap.fields += data["fields"]
        snap.buttons += [b for b in data["buttons"] if b not in snap.buttons]
        snap.headings += data["headings"]
        snap.errors += data["errors"]
        snap.invalid += data.get("invalid", [])
        snap.password = snap.password or data["password"]
        snap.captcha = snap.captcha or data["captcha"]
        if i == 0:
            main_text = data["text"]
        else:
            snap.frame_urls.append(frame.url)
            if data["fields"] and data["text"]:  # an embedded application form (Greenhouse, iCIMS...)
                texts.append(data["text"][:2000])
        snap.all_text += data["text"] + "\n"
    snap.text = (main_text[:2500] + ("\n[form frame] " + "\n[form frame] ".join(texts) if texts else ""))[:5000]
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
    page_kind: Literal["job_page", "form", "sign_in", "captcha", "confirmation", "error", "other"] = Field(
        description="job_page: a posting with an Apply button. form: application questions (including a final "
                    "'review your application' step). sign_in: a login / create-account wall. captcha: a CAPTCHA "
                    "you'd have to solve is on screen. confirmation: ONLY a page saying the application was "
                    "received. error: the site shows an error. other: anything else")
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
- For select/radio/checkbox-group/combobox fields, copy an option's text exactly.
- File uploads: a resume/CV field gets "resume", a cover letter field gets "cover_letter". If the
  current value already shows a file attached, leave it. Other required uploads (transcript,
  portfolio...) go in missing_info.
- If there is a cover letter text box, use the cover letter text provided.
- Short written questions (why this role/company, tell us about yourself, relevant experience):
  write 2-4 genuine first-person sentences grounded in the candidate's resume and the job.
- "How did you hear about this job?": use where the job was found (given with the job), picking the
  closest option (e.g. a university / campus / job board option for WaterlooWorks). This overrides
  any "how did you hear" line in the facts.
- Demographic / EEO / voluntary self-identification / disability / veteran / gender / race
  questions: pick the "prefer not to say" / "decline to self-identify" style option, or leave optional
  ones empty if there is no such option.
- Required consent / acknowledgement / "information is accurate" / privacy-policy checkboxes: tick
  them. Marketing or "talent community" email opt-ins: leave unticked.
- Dates: follow the field's placeholder format. Fields of type "readonly" can't be typed into: if
  one is required and empty, list it in missing_info.
- Never type into password fields, never create an account, never try to solve a CAPTCHA.

Choosing next_action:
- "click": go to the application or the next step without sending it (e.g. "Apply" / "Apply now" on
  a job posting, "Next", "Continue", "Save and continue"). Put the exact button text in button_text.
- "submit": the button that SENDS the completed application, whatever its label ("Submit
  application", "Submit", "Send", "Apply", "Finish", "Complete"). Only when every required field
  is answered. button_text = that button's exact text.
- "needs_user": the page needs the candidate: a sign-in or account creation (password fields) with
  no way around it, a CAPTCHA that captcha_on_page says is showing, a required question or upload
  you have no facts for, or you are stuck. A "protected by reCAPTCHA" notice is not a CAPTCHA.
  If a sign-in page offers to apply without an account / as a guest, click that instead.
- "done": the page says the application was received / submitted.
If the page shows validation errors (or fields the browser says are missing or invalid), fix those
fields before continuing."""


def candidate_block(profile: ResumeProfile, applicant, facts: str, switches: dict[str, bool]) -> str:
    contact = {k: v for k, v in vars(applicant).items() if v}
    contact["full_name"] = applicant.full_name
    extra = "\n".join(f"- {k}: {'Yes' if v else 'No'}" for k, v in switches.items())
    return (
        f"<candidate_resume_profile>\n{profile.model_dump_json(indent=1)}\n</candidate_resume_profile>\n"
        f"<candidate_contact>\n{json.dumps(contact, indent=1)}\n</candidate_contact>\n"
        f"<candidate_facts>\n{facts.strip()}\n{extra}\n</candidate_facts>"
    )


FOUND_ON = {
    "waterlooworks": "WaterlooWorks (the University of Waterloo co-op job board)",
    "web": "the company's careers website",
    "greenhouse": "the company's careers website",
    "lever": "the company's careers website",
    "ashby": "the company's careers website",
}


def make_planner(llm, profile: ResumeProfile, applicant, facts: str, switches: dict[str, bool],
                 effort: str = "medium") -> Callable[[Snapshot, Job, str], StepPlan]:
    system = FORM_SYSTEM + "\n\n" + candidate_block(profile, applicant, facts, switches)

    def plan(snap: Snapshot, job: Job, cover_letter_text: str) -> StepPlan:
        found_on = FOUND_ON.get(job.source, "not known (use the facts)")
        content = (
            f"<job>\n{job.title} at {job.company} ({job.location})\nFound on: {found_on}\n"
            f"{job.description[:1500]}\n</job>\n"
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
    """Index of the option matching `value`: exact, then ignoring case/spaces, then a prefix or
    partial match - but only if exactly one option matches that way ("India" must not pick
    "British Indian Ocean Territory", "Yes" must not pick between two "Yes, ..." options)."""
    v = _norm(value)
    if not options or not v:
        return None
    for i, o in enumerate(options):
        if o == value:
            return i
    norm = [_norm(o) for o in options]
    for i, o in enumerate(norm):
        if o == v:
            return i
    for test in (lambda o: o.startswith(v) or v.startswith(o), lambda o: v in o or (len(o) > 2 and o in v)):
        hits = [i for i, o in enumerate(norm) if o and test(o)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None  # ambiguous: better to leave it than to pick the wrong answer
    return None


_TRUE = {"true", "yes", "y", "1", "checked", "on", "tick", "ticked"}

# The visible options of an open dropdown: the listbox the control points at if it says which,
# else every visible [role=option] in the frame. Tags them data-coop-opt=<index>.
_OPTIONS_JS = r"""(el) => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const visible = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
                           return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
    document.querySelectorAll('[data-coop-opt]').forEach(e => e.removeAttribute('data-coop-opt'));
    const ids = (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
    const lists = ids.map(id => document.getElementById(id)).filter(Boolean);
    let opts = lists.flatMap(l => [...l.querySelectorAll('[role=option]')]).filter(visible);
    if (!opts.length) opts = [...document.querySelectorAll('[role=option]')].filter(visible);
    // react-select before 5.8: options are plain divs with ids like react-select-3-option-0
    if (!opts.length) opts = [...document.querySelectorAll('[id*="-option-"]')].filter(visible);
    if (!opts.length) opts = lists.flatMap(l => [...l.children]).filter(visible);
    opts.forEach((o, i) => o.setAttribute('data-coop-opt', String(i)));
    return opts.map(o => clean(o.innerText || o.textContent));
}"""


def _pick_from_dropdown(frame, box, value: str, log, label: str, typed: bool) -> bool:
    texts = box.evaluate(_OPTIONS_JS)
    if not texts:
        return typed  # a free-text field with suggestions we can't see: the typed text stands
    i = best_option(texts, value)
    if i is None:
        log(f"    ! '{label[:60]}': no option matches '{value[:60]}'")
        _safe(lambda: box.press("Escape"), None)
        return False
    frame.locator(f'[data-coop-opt="{i}"]').first.click(timeout=5000)
    return True


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
            i = best_option(f.get("options", []), value)  # the full list, not just what Claude saw
            if i is None:
                log(f"    ! '{f['label'][:60]}': no option matches '{value[:60]}'")
                return False
            loc.first.select_option(index=i)
        elif kind in ("radio", "checkbox_group"):
            i = best_option(f.get("options", []), value)
            if i is None:
                log(f"    ! '{f['label'][:60]}': no option matches '{value[:60]}'")
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
            box.click(timeout=5000)
            if not f.get("pick_only"):
                box.fill(value)
            frame.wait_for_timeout(700)
            return _pick_from_dropdown(frame, box, value, log, f["label"], typed=not f.get("pick_only"))
        elif kind == "readonly":
            return False
        else:
            loc.first.fill(value)
        return True
    except Exception as e:
        log(f"    ! couldn't fill '{f['label'][:60]}': {str(e).splitlines()[0][:120]}")
        return False


# Finds the button to press in one document and tags it data-coop-b=<tag>. Returns a score
# (0 = none): exact text beats a prefix beats a partial match; then the button nearest the
# most filled-in fields (the application form, not a job-alert or newsletter form), and not
# one in the header / nav / search box / cookie banner.
_CLICK_JS = r"""([want, tag]) => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const w = clean(want);
    document.querySelectorAll('[data-coop-b]').forEach(e => e.removeAttribute('data-coop-b'));
    if (!w) return 0;
    const els = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
        .filter(el => { const r = el.getBoundingClientRect(), st = getComputedStyle(el);
                        return r.width && r.height && st.visibility !== 'hidden' && !el.disabled &&
                               el.getAttribute('aria-disabled') !== 'true'; });
    const label = el => clean(el.innerText || el.value || el.getAttribute('aria-label') || '');
    const chrome = el => !!el.closest('nav, header, footer, [role=search], [role=navigation], [role=banner], ' +
        '[role=contentinfo], [id*=cookie i], [class*=cookie i], [id*=onetrust i]');
    const isFilled = f => f.type === 'checkbox' || f.type === 'radio' ? f.checked
        : f.type === 'file' ? !!(f.files && f.files.length)
        : f.tagName === 'SELECT' ? f.selectedIndex > 0 : !!(f.value || '').trim();
    const filled = [...document.querySelectorAll('[data-coop-f]')].filter(isFilled);
    // how many filled fields share the button's nearest container that has any (and how far up it is)
    const near = el => {
        let a = el.parentElement, depth = 1;
        for (; a && a !== document.documentElement; a = a.parentElement, depth++) {
            const n = filled.filter(f => a.contains(f)).length;
            if (n) return [n, depth];
        }
        return [0, 99];
    };
    let best = null, bestScore = 0;
    for (const el of els) {
        const t = label(el);
        const tier = t === w ? 3 : (w && t.startsWith(w)) ? 2 : (w && t.includes(w)) ? 1 : 0;
        if (!tier) continue;
        const [n, depth] = near(el);
        const score = tier * 1000 + Math.min(n, 50) * 10 - Math.min(depth, 30) - (chrome(el) ? 300 : 0) + 400;
        if (score > bestScore) { best = el; bestScore = score; }
    }
    if (!best) return 0;
    best.setAttribute('data-coop-b', tag);
    return bestScore;
}"""

# Would the browser refuse to send the form this button belongs to (an empty required field)?
_WOULD_BLOCK_JS = r"""(b) => {
    const form = b.form || b.closest('form');
    const submits = b.tagName === 'INPUT' ? b.type === 'submit' : b.tagName === 'BUTTON' && (b.type || 'submit') === 'submit';
    if (!form || !submits || form.noValidate || b.formNoValidate) return [];
    return [...form.elements].filter(e => e.willValidate && !e.validity.valid)
        .map(e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.name || e.type || '').trim().slice(0, 60));
}"""


def _find_button(page, text: str):
    """The frame whose best match for `text` scores highest (tagged data-coop-b="go"), or None."""
    if not (text or "").strip():
        return None
    best_score, best_frame = 0, None
    for frame in page.frames:
        try:
            score = frame_eval(frame, _CLICK_JS, [text, "go"])
        except Exception:
            continue
        if score and score > best_score:
            best_score, best_frame = score, frame
    return best_frame


def _click_and_follow(ctx, page, frame, on_tick=None):
    """Click the tagged button. Returns the page to continue on (a new tab if it opened one),
    or None if it couldn't be clicked. A page that closes after the click is returned closed.
    on_tick() is called while watching for a new tab; returning True stops the wait."""
    before = len(ctx.pages)
    try:
        # don't wait for a navigation here: a slow form POST must not look like a failed click
        frame.locator('[data-coop-b="go"]').first.click(timeout=10000, no_wait_after=True)
    except Exception:
        return page if page.is_closed() else None
    try:
        for _ in range(16):  # a link may open the form in a new tab
            if len(ctx.pages) > before:
                new = ctx.pages[-1]
                _safe(lambda: new.wait_for_load_state(), None)
                return new
            if on_tick and on_tick():
                return page
            page.wait_for_timeout(250)
        _safe(lambda: page.wait_for_load_state("load", timeout=15000), None)
    except Exception:  # the window closed itself
        pass
    return page


def click_button(ctx, page, text: str):
    """Click the button with this text (the best match across all frames). Returns the page to
    continue on (a new tab if it opened one), or None if there's no such button."""
    frame = _find_button(page, text)
    return _click_and_follow(ctx, page, frame) if frame is not None else None


SUCCESS_RX = re.compile(
    r"thanks? you for (applying|your application|submitting)|thanks for (applying|your application|submitting)"
    r"|application (has been |was )?(successfully )?(submitted|received|sent)"
    r"|we('ve| have) received your application|successfully (submitted|applied)"
    r"|you('ve| have) successfully applied",
    re.I,
)
# "Thank you for your interest in X" is boilerplate on forms and job pages, so it doesn't count;
# neither do instructions like "once your application is submitted..." or errors.
_NOT_A_CONFIRMATION_RX = re.compile(
    r"\b(once|after|when|until|before|if|cannot|can't|not|unable|error|failed|make sure)\b", re.I)


def newly_confirmed(text: str, before: str) -> bool:
    """The page now says the application was received, and didn't say so before."""
    seen = (before or "").lower()
    for line in re.split(r"[\n.!?]+", text or ""):
        if _NOT_A_CONFIRMATION_RX.search(line):
            continue
        if any(m.group(0).lower() not in seen for m in SUCCESS_RX.finditer(line)):
            return True
    return False


# Looks like the button that sends the application (when Claude calls it a plain "click").
SUBMITISH_RX = re.compile(r"\b(submit|send|finish|complete)\b|^\s*apply(\s+now)?\s*$", re.I)
# What a validation message (the site rejecting the form) looks like - not "Uploading, please wait..."
VALIDATION_RX = re.compile(
    r"\brequired\b|\binvalid\b|not (a )?valid|please (enter|fill|provide|select|choose|correct|complete|upload|add|check)"
    r"|can(no|')t be (blank|empty)|is missing|must (be|have|contain|include)|too (long|short)|\bfix\b", re.I)


def _page_text(page) -> str:
    """Every frame's text."""
    texts = []
    for frame in page.frames:
        try:
            texts.append(frame_eval(frame, "() => document.body ? document.body.innerText : ''", timeout_ms=1500) or "")
        except Exception:
            continue
    return "\n".join(texts)


_PRESSED_JS = r"""() => { const b = document.querySelector('[data-coop-pressed]');
    if (!b) return false; const r = b.getBoundingClientRect(); return !!(r.width && r.height); }"""


def _pressed_button_visible(page) -> bool:
    for frame in page.frames:
        try:
            if frame_eval(frame, _PRESSED_JS, timeout_ms=1500):
                return True
        except Exception:
            continue
    return False


@dataclass
class _Submitted:
    button: str = ""
    url: str = ""
    text_before: str = ""
    errors_before: list[str] = field(default_factory=list)
    flagged_before: int = 0
    pressed_at: float = 0.0


SUBMIT_WAIT_S = 45  # how long to wait for the page to react after pressing Submit
CONFIRM_WAIT_S = 60  # how long after pressing it a slow site may take to confirm, before we give up


def _await_confirmation(page, sub: _Submitted) -> bool:
    """Keep watching for the site's confirmation until CONFIRM_WAIT_S after the press."""
    while time.monotonic() < sub.pressed_at + CONFIRM_WAIT_S:
        if page.is_closed():
            return False
        if newly_confirmed(_safe(lambda: _page_text(page), ""), sub.text_before):
            return True
        _safe(lambda: page.wait_for_timeout(1000), None)
    return False


def _url_core(url: str) -> str:
    return re.sub(r"#.*$", "", url or "").rstrip("/")


def _moved_on(page, sub: _Submitted) -> bool:
    """A different page/form than the one we submitted: the URL changed and the button we
    pressed is gone. (Fields alone don't tell: our answers can reveal or relabel some.)"""
    return _url_core(page.url) != _url_core(sub.url) and not _pressed_button_visible(page)


def _rejected(snap: Snapshot, sub: _Submitted) -> str:
    """The site showed validation errors after our submit (so nothing was sent): the message, or ''."""
    new = [e for e in snap.errors if e not in sub.errors_before and VALIDATION_RX.search(e)]
    if new:
        return new[0]
    if sum(1 for f in snap.fields if f.get("flagged")) > sub.flagged_before:
        return next(f["label"] for f in snap.fields if f.get("flagged")) + " (marked invalid)"
    return ""


def wait_after_submit(page, sub: _Submitted, max_s: float | None = None, settle_s: float = 2.5) -> str:
    """After pressing Submit, wait for the site to answer instead of a fixed pause: uploading
    files can take a while, and acting on the unchanged form too early is how an application
    gets sent twice. Checks for a confirmation all along (toasts vanish after a few seconds).

    Returns "confirmed", "changed" (and settled), "closed" or "no change"."""
    max_s = SUBMIT_WAIT_S if max_s is None else max_s
    start = time.monotonic()
    last, last_change, changed = None, start, False
    while time.monotonic() - start < max_s:
        if page.is_closed():
            return "closed"
        state = _safe(lambda: (page.url, _page_text(page)), None)
        if state is not None:
            if newly_confirmed(state[1], sub.text_before):
                return "confirmed"
            if not changed and (state[0] != sub.url or state[1] != sub.text_before):
                changed = True
            if state != last:
                last, last_change = state, time.monotonic()
            elif changed and time.monotonic() - last_change >= settle_s:
                return "changed"
        _safe(lambda: page.wait_for_timeout(500), None)
    return "changed" if changed else "no change"


def fill_and_submit(ctx, page, job: Job, plan_step, files: dict[str, Path | None], cover_letter_text: str,
                    confirm: Confirm, auto: bool = False, log=print, prefill=None, max_steps: int = 12) -> bool:
    """Work through the application, page by page. True once the site confirms it was submitted.

    auto=True: submit without asking; anything that needs you raises NeedsYou.
    Otherwise you confirm before the final submit, and are asked to handle sign-ins etc.

    Submit is pressed once. It is only pressed again if the site shows validation errors on
    that form, or after moving on to a different form. Anything that goes wrong after a press
    says "check before applying again", so you're never told to finish an application that may
    already have been sent.
    """
    url = job.apply_url or job.url
    if not url:
        raise ApplyError("This job has no application link.")
    page.goto(url)
    _safe(lambda: page.wait_for_load_state("networkidle", timeout=20000), None)
    sent: list[_Submitted] = []  # [the submit we pressed] once we have
    try:
        return _work_through(ctx, page, job, plan_step, files, cover_letter_text, confirm, auto, log, prefill,
                             max_steps, sent)
    except Exception as e:
        if not sent:
            raise
        raise NeedsYou(
            f"I pressed \"{sent[0].button}\" but couldn't confirm the application went through ({_first_line(e)}). "
            "Check your email or the company's site before applying again."
        ) from e


def _first_line(e: Exception) -> str:
    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
    return msg.removeprefix("Needs you: ")[:200]


def _work_through(ctx, page, job, plan_step, files, cover_letter_text, confirm, auto, log, prefill, max_steps,
                  sent: list[_Submitted]) -> bool:
    last_sig, repeats = None, 0
    for step in range(1, max_steps + 1):
        page.wait_for_timeout(1500)
        if prefill and not sent:
            _safe(lambda: prefill(page), None)
        snap = snapshot(page)
        if sent:
            if newly_confirmed(snap.all_text, sent[0].text_before):
                log("  the site confirmed your application was received")
                return True
            why = _rejected(snap, sent[0]) if not _moved_on(page, sent[0]) else ""
            if why:
                log(f"  the site didn't accept it yet: {why[:100]}")
                sent.clear()  # nothing was sent: fix it and submit again
        sig = snap.signature()
        repeats = repeats + 1 if sig == last_sig else 0
        last_sig = sig
        if repeats >= 2:
            _needs(f"the page isn't moving on ({(snap.errors or snap.invalid or ['no error shown'])[0][:100]})",
                   auto, confirm, log)
            last_sig, repeats = None, 0
            continue

        plan = plan_step(snap, job, cover_letter_text)
        log(f"  step {step}: {plan.note}")
        n = sum(apply_fill(snap, f, files, log) for f in plan.fills)
        if n:
            log(f"    filled {n} field{'s' if n != 1 else ''}")
        if plan.next_action == "done":
            if sent:
                return True
            # Claude reads it as finished, but we never pressed Submit (e.g. "you've already applied")
            if auto:
                raise NeedsYou(f"check this one - the site may say you've already applied ({plan.note})")
            return confirm(f"The site seems to say the application is done ({plan.note}). Has it already "
                           "been submitted? (Yes = mark it as applied)")
        if plan.missing_info:
            _needs("answer: " + "; ".join(plan.missing_info), auto, confirm, log)
            last_sig, repeats = None, 0
            continue
        captcha_wall = plan.page_kind == "captcha" and bool(snap.captcha)
        sign_in_wall = plan.page_kind == "sign_in" and plan.next_action != "click"  # "continue as guest" is fine
        if plan.next_action == "needs_user" or captcha_wall or sign_in_wall:
            why = {"sign_in": "sign in or create an account", "captcha": "complete the CAPTCHA"}.get(plan.page_kind)
            _needs(why or plan.note, auto, confirm, log)
            last_sig, repeats = None, 0
            continue
        is_submit = plan.next_action == "submit" or (n > 0 and bool(SUBMITISH_RX.search(plan.button_text or "")))
        if not is_submit:
            nxt = click_button(ctx, page, plan.button_text)
            if nxt is None:
                _needs(f"press \"{plan.button_text}\" (I couldn't find that button)", auto, confirm, log)
                continue
            page = nxt
            continue

        # ---- the submit ----
        if sent and not _moved_on(page, sent[0]):
            # Same form, no validation errors, no confirmation: it may well have been sent (or still be
            # sending). Never press twice - give a slow site the rest of a minute to confirm instead.
            if _await_confirmation(page, sent[0]):
                log("  the site confirmed your application was received")
                return True
            if auto:
                raise NeedsYou("the page didn't confirm it after Submit")
            return confirm(f"I pressed \"{sent[0].button}\" but the site hasn't confirmed it. Did the application "
                           "go through? (Yes = mark it as applied, No = leave it for you to finish)")
        frame = _find_button(page, plan.button_text)
        if frame is None:
            _needs(f"press \"{plan.button_text}\" (I couldn't find that button)", auto, confirm, log)
            continue
        missing = _safe(lambda: frame.locator('[data-coop-b="go"]').first.evaluate(_WOULD_BLOCK_JS), [])
        if missing:  # the browser won't send it as it is - nothing to press yet
            _needs("fill in the required fields the form says are empty: " + ", ".join(missing[:6]), auto, confirm, log)
            last_sig, repeats = None, 0
            continue
        if not auto and not confirm(
            f"The application for '{job.title}' at {job.company} is filled in - please review it in the browser "
            "window. Submit it now?"
        ):
            log("  not submitted (the form is left open for you)")
            return False
        pre = snapshot(page)  # the form as it is sent: errors / invalid marks to compare with afterwards
        frame = _find_button(page, plan.button_text)  # re-tag: you may have changed things while reviewing
        if frame is None:
            _needs(f"press \"{plan.button_text}\" (I couldn't find that button)", auto, confirm, log)
            continue
        _safe(lambda: frame.locator('[data-coop-b="go"]').first.evaluate(
            "b => { document.querySelectorAll('[data-coop-pressed]').forEach(e => e.removeAttribute('data-coop-pressed'));"
            " b.setAttribute('data-coop-pressed', '1'); }"), None)
        sent[:] = [_Submitted(plan.button_text, page.url, _page_text(page), list(pre.errors),
                              sum(1 for f in pre.fields if f.get("flagged")), time.monotonic())]
        confirmed = []  # a confirmation toast can come and go within seconds: watch from the click on

        def tick(p=page):
            if newly_confirmed(_safe(lambda: _page_text(p), ""), sent[0].text_before):
                confirmed.append(True)
            return bool(confirmed)

        nxt = _click_and_follow(ctx, page, frame, on_tick=tick)
        if nxt is None:
            sent.clear()  # the click didn't happen
            _needs(f"press \"{plan.button_text}\" (I couldn't click it)", auto, confirm, log)
            continue
        page = nxt
        log("  pressed submit - waiting for the site to answer...")
        outcome = "confirmed" if confirmed else wait_after_submit(page, sent[0])
        if outcome == "confirmed":
            log("  the site confirmed your application was received")
            return True
        if outcome == "closed":
            raise ApplyError("the window closed after Submit")
    if sent:
        if not page.is_closed() and newly_confirmed(_page_text(page), sent[0].text_before):
            return True
        raise NeedsYou("it took more steps than expected")
    _needs("finish the application (it took more steps than expected)", auto, confirm, log)
    return confirm("Did the application go through?") if not auto else False


def _needs(what: str, auto: bool, confirm: Confirm, log) -> None:
    if auto:
        raise NeedsYou(f"Needs you: {what}")
    log(f"  ! needs you: {what}")
    if not confirm(f"This application needs you to {what}. Do that in the browser window, then click Yes "
                   "and I'll carry on (No to stop)."):
        raise ApplyError(f"Stopped - needed you to {what}.")
