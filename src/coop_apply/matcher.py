"""Rank jobs against a resume.

Two stages keep API cost down:
1. ``prefilter`` - free, local keyword overlap plus hard filters from config.
2. ``score_job`` - Claude reads the posting and the resume profile and returns a 0-100
   fit score with reasons and gaps, for the top prefiltered jobs only.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .config import SearchConfig
from .llm import LLM, LLMError
from .models import Job, MatchResult, ResumeProfile, Verdict
from .utils import contains_any

STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "to", "in", "for", "with", "on", "at", "by", "developer",
    "engineer", "engineering", "software", "intern", "co-op", "role", "roles", "analyst",
}

SCORE_SYSTEM_TEMPLATE = """You assess how well a university co-op student fits a job posting.

<candidate_profile>
{profile}
</candidate_profile>

Score the fit from 0 to 100 using this rubric:
- 85-100 (strong): meets nearly all stated requirements with direct evidence; seniority matches a co-op student.
- 70-84 (good): meets the core requirements; a few nice-to-haves missing.
- 50-69 (stretch): related background but missing one or more core requirements.
- 0-49 (poor): different field, wrong seniority (e.g. full-time senior role), or hard requirements
  the candidate clearly cannot meet (citizenship/clearance, specific degree year, etc.).

Ground every reason in concrete resume evidence and every gap in a concrete posting requirement.
Keywords should be the posting's own terms the candidate genuinely has evidence for."""


def profile_terms(profile: ResumeProfile) -> set[str]:
    terms: set[str] = set()
    for skill in profile.skills:
        terms.add(skill.lower().strip())
    for project in profile.projects:
        terms.update(t.lower().strip() for t in project.technologies)
    for role in profile.target_roles:
        terms.update(w for w in re.findall(r"[a-z][a-z+#.]+", role.lower()) if w not in STOPWORDS)
    return {t for t in terms if t and len(t) > 1}


def keyword_score(job: Job, terms: set[str]) -> float:
    text = f"{job.title}\n{job.description}".lower()
    if not terms:
        return 0.0
    hits = 0
    for term in terms:
        # word-ish boundary that still matches things like "c++" and "node.js"
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text):
            hits += 1
    title_bonus = 2 if any(t in job.title.lower() for t in terms) else 0
    return hits + title_bonus


def passes_filters(job: Job, cfg: SearchConfig) -> bool:
    if cfg.exclude_keywords and contains_any(job.title, cfg.exclude_keywords):
        return False
    if cfg.locations:
        loc = job.location.lower()
        if loc and not any(l.lower() in loc for l in cfg.locations):
            return False
    return True


def prefilter(jobs: list[Job], profile: ResumeProfile, cfg: SearchConfig) -> list[tuple[Job, float]]:
    terms = profile_terms(profile)
    scored = [(job, keyword_score(job, terms)) for job in jobs if passes_filters(job, cfg)]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[: cfg.prefilter_keep]


def score_job(job: Job, profile: ResumeProfile, llm: LLM, effort: str = "low") -> MatchResult:
    system = SCORE_SYSTEM_TEMPLATE.format(profile=profile.model_dump_json(indent=1))
    content = f"<job_posting>\n{job.summary_text()}\n</job_posting>\n\nAssess the candidate's fit for this posting."
    result = llm.structured(system=system, content=content, schema=MatchResult, effort=effort, cache_system=True)
    result.score = max(0, min(100, result.score))
    return result


def score_jobs(
    jobs: list[Job],
    profile: ResumeProfile,
    llm: LLM,
    effort: str = "low",
    workers: int = 4,
    on_result: Callable[[Job, MatchResult | None, Exception | None], None] | None = None,
) -> dict[str, MatchResult]:
    results: dict[str, MatchResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(score_job, job, profile, llm, effort): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                match = fut.result()
            except LLMError as e:
                if on_result:
                    on_result(job, None, e)
                continue
            results[job.id] = match
            if on_result:
                on_result(job, match, None)
    return results


def verdict_for(score: int) -> Verdict:
    if score >= 85:
        return Verdict.strong
    if score >= 70:
        return Verdict.good
    if score >= 50:
        return Verdict.stretch
    return Verdict.poor
