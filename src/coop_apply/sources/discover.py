"""Find co-op/intern postings at the top employers for your discipline, on their own websites.

Two steps:
1. Claude searches the web (server-side web search): it works out which companies are the
   leading employers for your program and region, then looks on each company's careers
   site for current student / co-op / intern postings, noting the exact posting URLs.
2. The written findings are turned into a structured list of postings (no web access in
   this step, so nothing new can be made up).

Postings without a real URL from the search are dropped.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, Field

from ..llm import LLM
from ..models import Job, ResumeProfile
from ..utils import short_hash

RESEARCH_SYSTEM = """You research job openings for a university co-op student.

Use web search. First identify the leading employers for the student's discipline in the given
region (companies that hire co-op students or interns in this field). Then, for each company,
find its official careers / jobs page and look for CURRENT student, co-op or intern postings
relevant to the student.

Rules:
- Only report postings you actually found during this search, with the exact URL of the
  posting (and of its application page if different). Never guess or construct URLs.
- Prefer the company's own careers site or its applicant tracking system (Workday, Greenhouse,
  Lever, SuccessFactors, Taleo, iCIMS, etc.) over job aggregators.
- Skip postings that are clearly closed, expired, or for experienced professionals.
- For each posting give: company, job title, location, posting URL, application URL,
  deadline if shown, and a 2-4 sentence summary of duties and requirements.
- Also list companies you checked that had no relevant openings, with their careers URL."""

EXTRACT_SYSTEM = """Convert research notes about job postings into structured data.
Copy only postings that have a real http(s) URL in the notes. Do not add anything that is not
in the notes. Leave a field empty if the notes don't give it."""


class FoundPosting(BaseModel):
    company: str
    title: str
    location: str
    url: str = Field(description="URL of the posting itself")
    apply_url: str = Field(description="URL of the application page, or the posting URL if the same")
    deadline: str
    summary: str


class FoundPostings(BaseModel):
    postings: list[FoundPosting]


def research_prompt(profile: ResumeProfile, region: str, n_companies: int) -> str:
    roles = ", ".join(profile.target_roles) or "co-op roles in the student's field"
    skills = ", ".join(profile.skills[:20])
    return (
        f"Student: {profile.program} {profile.academic_level}\n"
        f"Best suited for: {roles}\n"
        f"Key skills: {skills}\n"
        f"Region: {region}\n\n"
        f"Find the top {n_companies} employers for this student's discipline in {region} and their current "
        "co-op / intern / student postings on their own websites."
    )


def discover_jobs(profile: ResumeProfile, llm: LLM, region: str = "Canada", n_companies: int = 15,
                  log: Callable[[str], None] = print) -> list[Job]:
    log(f"  researching top employers for {profile.program or 'your program'} in {region} (this takes a few minutes)...")
    notes = llm.research(system=RESEARCH_SYSTEM, prompt=research_prompt(profile, region, n_companies),
                         max_searches=max(10, n_companies * 2))
    if not notes:
        return []
    found = llm.structured(system=EXTRACT_SYSTEM, content=f"<research_notes>\n{notes}\n</research_notes>",
                           schema=FoundPostings, effort="low")
    jobs = []
    for p in found.postings:
        url = p.url.strip()
        if not url.startswith(("http://", "https://")):
            continue
        apply_url = p.apply_url.strip() if p.apply_url.strip().startswith("http") else url
        jobs.append(Job(
            id=f"web:{short_hash(p.company.lower(), p.title.lower(), url)}",
            source="web",
            title=p.title.strip(),
            company=p.company.strip(),
            location=p.location.strip(),
            description=p.summary.strip(),
            url=url,
            apply_url=apply_url,
            deadline=p.deadline.strip(),
        ))
    return jobs
