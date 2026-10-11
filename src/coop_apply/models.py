"""Data models shared across the app.

The ``ResumeProfile``, ``MatchResult`` and ``CoverLetter`` models double as
structured-output schemas for Claude, so keep them free of validators and
numeric constraints that JSON schema output can't express.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["waterlooworks", "greenhouse", "lever", "ashby"]


class Job(BaseModel):
    id: str  # globally unique, prefixed by source, e.g. "ww:123456", "gh:stripe:4012345"
    source: Source
    title: str
    company: str
    location: str = ""
    description: str = ""
    url: str = ""
    apply_url: str = ""
    deadline: str = ""
    extra: dict[str, str] = Field(default_factory=dict)

    def summary_text(self) -> str:
        """Full plain-text rendering used in prompts."""
        lines = [
            f"Title: {self.title}",
            f"Company: {self.company}",
            f"Location: {self.location or 'unspecified'}",
        ]
        if self.deadline:
            lines.append(f"Deadline: {self.deadline}")
        for key, value in self.extra.items():
            lines.append(f"{key}: {value}")
        lines += ["", "Description:", self.description or "(no description)"]
        return "\n".join(lines)


# ---- Claude structured outputs -------------------------------------------------


class Experience(BaseModel):
    title: str
    organization: str
    dates: str
    highlights: list[str]


class Project(BaseModel):
    name: str
    description: str
    technologies: list[str]


class ResumeProfile(BaseModel):
    name: str
    email: str
    phone: str
    links: list[str]
    program: str = Field(description="Degree program and school, e.g. 'BASc Computer Engineering, University of Waterloo'")
    academic_level: str = Field(description="Year/term if stated, e.g. '2B', else ''")
    summary: str = Field(description="2-3 sentence neutral summary of the candidate")
    skills: list[str]
    experiences: list[Experience]
    projects: list[Project]
    target_roles: list[str] = Field(description="Kinds of roles this resume is best suited for")


class Verdict(str, Enum):
    strong = "strong"
    good = "good"
    stretch = "stretch"
    poor = "poor"


class MatchResult(BaseModel):
    score: int = Field(description="Fit score from 0 to 100")
    verdict: Verdict
    reasons: list[str] = Field(description="Specific resume evidence that matches the posting")
    gaps: list[str] = Field(description="Requirements the resume does not show")
    keywords: list[str] = Field(description="Posting keywords worth emphasizing in a cover letter")


class CoverLetter(BaseModel):
    salutation: str
    paragraphs: list[str]
    closing: str

    def as_text(self, signature: str) -> str:
        body = "\n\n".join(p.strip() for p in self.paragraphs)
        return f"{self.salutation}\n\n{body}\n\n{self.closing}\n{signature}\n"


class Status(str, Enum):
    new = "new"
    shortlisted = "shortlisted"
    prepared = "prepared"  # cover letter generated
    submitted = "submitted"
    skipped = "skipped"
    failed = "failed"
