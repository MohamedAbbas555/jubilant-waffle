"""Generate a tailored cover letter for a job and render it to .txt and .pdf."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .llm import LLM
from .models import CoverLetter, Job, MatchResult, ResumeProfile
from .utils import slugify

LETTER_SYSTEM = """You write cover letters for a university co-op student applying to a specific posting.

Write like a capable student, not a marketing department: plain, specific, confident.
- 3-4 body paragraphs, 250-350 words total.
- Open with the role and one concrete reason this team/company is a fit; no "I am writing to express my interest".
- Connect 2-3 specific experiences or projects from the resume to the posting's actual requirements,
  naming technologies and outcomes the resume states.
- Use only facts from the candidate profile. Never invent employers, metrics, skills or coursework.
  If the posting needs something the candidate lacks, either leave it out or frame it honestly as
  something they are ready to learn, pointing to adjacent evidence.
- Mention availability for the co-op term only if the posting or notes give the term.
- Salutation: "Dear Hiring Manager," unless the posting names a contact.
- Closing: a short sign-off line such as "Sincerely," (the name is added separately)."""


def generate_cover_letter(
    job: Job,
    profile: ResumeProfile,
    llm: LLM,
    match: MatchResult | None = None,
    notes: str = "",
    effort: str = "high",
) -> CoverLetter:
    parts = [
        f"<candidate_profile>\n{profile.model_dump_json(indent=1)}\n</candidate_profile>",
        f"<job_posting>\n{job.summary_text()}\n</job_posting>",
    ]
    if match:
        parts.append(
            "<fit_analysis>\n"
            f"Strengths: {'; '.join(match.reasons)}\n"
            f"Gaps: {'; '.join(match.gaps)}\n"
            f"Keywords to emphasize: {', '.join(match.keywords)}\n"
            "</fit_analysis>"
        )
    if notes:
        parts.append(f"<candidate_notes>\n{notes}\n</candidate_notes>")
    parts.append("Write the cover letter for this posting.")
    return llm.structured(system=LETTER_SYSTEM, content="\n\n".join(parts), schema=CoverLetter, effort=effort)


# ---- rendering -----------------------------------------------------------------

_ASCII_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    "…": "...", " ": " ", "•": "-",
}


def _latin1(text: str) -> str:
    for k, v in _ASCII_MAP.items():
        text = text.replace(k, v)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def letter_header(profile: ResumeProfile, job: Job, today: date | None = None) -> str:
    today = today or date.today()
    contact = " | ".join(x for x in [profile.email, profile.phone, *profile.links[:2]] if x)
    return "\n".join(
        x for x in [profile.name, contact, "", today.strftime("%B %-d, %Y"), "", job.company, ""] if x is not None
    )


def render_pdf(text: str, path: Path):
    from fpdf import FPDF

    pdf = FPDF(format="Letter")
    pdf.set_margins(25, 25, 25)
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for para in text.split("\n"):
        if para.strip():
            pdf.multi_cell(0, 5.5, _latin1(para), new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.ln(4)
    pdf.output(str(path))


def save_cover_letter(letter: CoverLetter, profile: ResumeProfile, job: Job, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = slugify(f"{job.company}-{job.title}-{job.id.split(':')[-1]}")
    text = letter_header(profile, job) + "\n" + letter.as_text(profile.name)
    txt_path = out_dir / f"{stem}.txt"
    pdf_path = out_dir / f"{stem}.pdf"
    txt_path.write_text(text, encoding="utf-8")
    render_pdf(text, pdf_path)
    return txt_path, pdf_path
