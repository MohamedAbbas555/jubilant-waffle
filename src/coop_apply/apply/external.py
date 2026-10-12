"""Quick, rule-based pre-fill for application forms on company websites.

Fills what it can identify with certainty (name, contact details, links, resume and cover
letter uploads). The full application - custom questions, dropdowns, multi-page forms,
pressing Next/Submit - is handled by ``formfill``, which has Claude read each page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..models import Job


@dataclass
class Applicant:
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    school: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


# (regex over the field's label/name/id/placeholder/aria-label, Applicant attribute)
FIELD_RULES: list[tuple[str, str]] = [
    (r"first[\s_-]*name|given[\s_-]*name|^first$", "first_name"),
    (r"last[\s_-]*name|family[\s_-]*name|surname|^last$", "last_name"),
    (r"^(full[\s_-]*)?name\*?$|full[\s_-]*name|^_?systemfield_name$", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile|^tel$", "phone"),
    (r"linked\s*in", "linkedin"),
    (r"git\s*hub", "github"),
    (r"website|portfolio|personal\s+site", "website"),
    (r"school|university|college", "school"),
]


def classify_field(descriptor: str) -> str | None:
    d = descriptor.strip().lower()
    for pattern, attr in FIELD_RULES:
        if re.search(pattern, d):
            return attr
    return None


def classify_file_input(descriptor: str) -> str | None:
    d = descriptor.lower()
    if "cover" in d:
        return "cover_letter"
    if re.search(r"resume|résumé|\bcv\b|curriculum", d):
        return "resume"
    return None


# Returns [own descriptors joined, text of the nearest wrapping element]. The wrapper text
# is only trusted when short, since a wrapping <div> can contain the whole form.
_DESCRIBE_JS = """el => {
    const parts = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.getAttribute('autocomplete')];
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) parts.unshift(l.innerText); }
    const wrap = el.closest('label, .field, .application-question, .form-group, [class*="field"], li');
    return [parts.filter(Boolean).join(' | '), wrap ? (wrap.innerText || '').trim() : ''];
}"""


def _describe(el) -> str:
    own, wrap = el.evaluate(_DESCRIBE_JS)
    if wrap and len(wrap) <= 80:
        own = f"{own} | {wrap}" if own else wrap
    return own


def fill_form(page, applicant: Applicant, resume: Path, cover_letter_pdf: Path | None,
              cover_letter_text: str | None, log=print) -> list[str]:
    filled: list[str] = []
    for el in page.locator("input:visible, textarea:visible").all():
        kind = (el.get_attribute("type") or "text").lower()
        if kind in ("hidden", "checkbox", "radio", "submit", "button", "file"):
            continue
        descriptor = _describe(el)
        if el.evaluate("el => el.tagName") == "TEXTAREA":
            if cover_letter_text and "cover" in descriptor.lower() and not el.input_value():
                el.fill(cover_letter_text)
                filled.append("cover letter (text)")
            continue
        attr = next((a for part in descriptor.split(" | ") if (a := classify_field(part))), None)
        value = getattr(applicant, attr, "") if attr else ""
        if value and not el.input_value():
            el.fill(value)
            filled.append(attr)

    for el in page.locator("input[type=file]").all():
        if el.evaluate("e => !!(e.files && e.files.length)"):
            continue  # already attached - don't upload twice
        descriptor = _describe(el)
        which = classify_file_input(descriptor)
        if which == "resume":
            el.set_input_files(str(resume))
            filled.append("resume upload")
        elif which == "cover_letter" and cover_letter_pdf:
            el.set_input_files(str(cover_letter_pdf))
            filled.append("cover letter upload")
    if filled:
        log("  pre-filled: " + ", ".join(dict.fromkeys(filled)))
    return filled


def open_application(page, job: Job, log=print):
    url = job.apply_url or job.url
    if not url:
        raise RuntimeError("Job has no application URL.")
    page.goto(url)
    page.wait_for_load_state("domcontentloaded")
    if job.source == "greenhouse" and not page.locator("input[type=file]").count():
        # Greenhouse job pages put the form below the description, sometimes behind a button.
        btn = page.get_by_role("button", name=re.compile("apply", re.I))
        if btn.count():
            btn.first.click()
    elif job.source == "ashby" and "/application" not in page.url:
        tab = page.get_by_role("tab", name=re.compile("application", re.I))
        if tab.count():
            tab.first.click()
    page.wait_for_timeout(1500)


def apply_external(page, job: Job, applicant: Applicant, resume: Path, cover_letter_pdf: Path | None,
                   cover_letter_text: str | None, confirm, log=print) -> bool:
    open_application(page, job, log)
    fill_form(page, applicant, resume, cover_letter_pdf, cover_letter_text, log)
    return confirm(
        "Finish any remaining questions in the browser and click Submit there. "
        "Did you submit the application?"
    )
