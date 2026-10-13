"""Command-line interface: ``coop-apply <command>``."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from .config import Config, load_config
from .db import Store
from .models import Job, MatchResult, Status

EXAMPLE_CONFIG = Path(__file__).resolve().parent / "config.example.toml"


# ---- helpers ---------------------------------------------------------------------


# The GUI replaces this with a function that asks in the browser instead of the terminal.
confirm_hook = None


def confirm(question: str) -> bool:
    if confirm_hook:
        return confirm_hook(question)
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def get_llm(cfg: Config):
    from .llm import LLM

    return LLM(model=cfg.claude.model)


def get_profile(cfg: Config, llm=None, force: bool = False):
    from .resume import load_or_parse

    return load_or_parse(cfg.profile.resume_path, cfg.data_dir, llm or get_llm(cfg), cfg.claude.effort_parse, force)


def fmt_row(job: Job, match: MatchResult | None, status: str) -> str:
    score = f"{match.score:>3}" if match else "  -"
    return f"{score}  {status:<11} {job.id:<28} {job.title[:45]:<45}  {job.company[:28]}"


def applicant_from(cfg: Config, profile):
    from .apply.external import Applicant

    p = cfg.profile
    first, _, last = (profile.name or "").partition(" ")
    return Applicant(
        first_name=p.first_name or first,
        last_name=p.last_name or last,
        email=p.email or profile.email,
        phone=p.phone or profile.phone,
        linkedin=p.linkedin or next((l for l in profile.links if "linkedin" in l.lower()), ""),
        github=p.github or next((l for l in profile.links if "github" in l.lower()), ""),
        website=p.website,
        school=p.school,
    )


# ---- commands --------------------------------------------------------------------


def cmd_init(cfg: Config, args):
    target = Path(args.config)
    if target.exists():
        print(f"{target} already exists.")
        return 1
    shutil.copy(EXAMPLE_CONFIG, target)
    print(f"Wrote {target}. Edit it (resume path, document names, company boards), then run `coop-apply resume`.")


def cmd_resume(cfg: Config, args):
    profile, h = get_profile(cfg, force=args.force)
    print(f"{profile.name} - {profile.program} {profile.academic_level}".strip())
    print(f"Skills: {', '.join(profile.skills)}")
    print(f"Best suited for: {', '.join(profile.target_roles)}")
    print(f"Experiences: {len(profile.experiences)}, projects: {len(profile.projects)}")
    print(f"(cached in {cfg.data_dir / 'profiles'})")


def cmd_login(cfg: Config, args):
    from .browser import browser_context, first_page
    from .sources.waterlooworks import wait_for_login

    with browser_context(cfg.browser_profile_dir, headless=False) as ctx:
        page = first_page(ctx)
        if wait_for_login(page, cfg.waterlooworks):
            print("Logged in. Session saved for future runs.")
        else:
            print("Timed out waiting for login.")
            return 1


def cmd_fetch(cfg: Config, args, store: Store):
    do_ww = args.ww or not args.external
    do_ext = args.external or not args.ww
    new = total = 0
    if do_ext:
        from .sources.external import fetch_external

        print("External boards:")
        for job in fetch_external(cfg.external):
            total += 1
            new += store.upsert_job(job)
    if do_ww:
        from .browser import browser_context, first_page
        from .sources.waterlooworks import scrape_postings, wait_for_login

        print("WaterlooWorks:")
        known = set() if args.refresh else {j.id for j in store.all_jobs() if j.source == "waterlooworks"}
        with browser_context(cfg.browser_profile_dir, headless=cfg.waterlooworks.headless) as ctx:
            page = first_page(ctx)
            if not wait_for_login(page, cfg.waterlooworks):
                print("Not logged in to WaterlooWorks; run `coop-apply login` first.")
                return 1
            applied_count = 0

            def record_applied(job_id: str, applied: bool) -> None:
                nonlocal applied_count
                if applied:
                    applied_count += 1
                    if (store.get_application(job_id) or {}).get("status") != Status.submitted.value:
                        store.set_status(job_id, Status.submitted, notes="Applied (WaterlooWorks shows a checkmark)")

            def on_known(job_id: str, applied: bool, applicants: int | None) -> None:
                job = store.get_job(job_id)
                if job and (job.applicants != applicants or job.applied_on_site != applied):
                    job.applicants, job.applied_on_site = applicants, applied
                    store.upsert_job(job)
                record_applied(job_id, applied)

            for job in scrape_postings(ctx, page, cfg.waterlooworks, known, ask=confirm,
                                       debug_dir=cfg.data_dir / "debug", on_known=on_known):
                total += 1
                # an older copy saved before its real posting number was known: keep its score/status
                if job.id.split(":", 1)[1].isdigit() and (old := store.find_unnumbered_ww_job(job.title, job.company)):
                    store.rename_job(old.id, job.id)
                new += store.upsert_job(job)
                record_applied(job.id, job.applied_on_site)
                print(f"  + {job.title} - {job.company}"
                      + (f" ({job.applicants} applicants)" if job.applicants is not None else "")
                      + (" [already applied]" if job.applied_on_site else ""))
            if applied_count:
                print(f"  {applied_count} postings you've already applied to are marked 'already applied'.")
    print(f"Fetched {total} postings ({new} new).")


def cmd_rank(cfg: Config, args, store: Store):
    from .matcher import prefilter, score_jobs

    llm = get_llm(cfg)
    profile, resume_hash = get_profile(cfg, llm)
    jobs = [j for j in store.all_jobs()  # don't spend money scoring jobs you've already applied to
            if (store.get_application(j.id) or {}).get("status") != Status.submitted.value]
    if not args.rescore:
        jobs = [j for j in jobs if store.get_match(j.id, resume_hash) is None]
    candidates = prefilter(jobs, profile, cfg.search)
    limit = args.limit or cfg.search.max_llm_scores
    to_score = [j for j, _ in candidates[:limit]]
    if not to_score:
        print("Nothing new to score.")
        return
    print(f"Scoring {len(to_score)} of {len(jobs)} unscored postings with {cfg.claude.model}...")

    def on_result(job, match, err):
        if err:
            print(f"  ! {job.id}: {err}")
        else:
            store.save_match(job.id, resume_hash, match)
            print(f"  {match.score:>3} {job.title[:50]} - {job.company}")

    score_jobs(to_score, profile, llm, cfg.claude.effort_rank, cfg.claude.workers, on_result)
    shortlisted = store.ranked(cfg.search.min_score, statuses=["new"])
    for job, _, _ in shortlisted:
        store.set_status(job.id, Status.shortlisted)
    print(f"{len(shortlisted)} newly shortlisted (score >= {cfg.search.min_score}). See `coop-apply list`.")


def cmd_list(cfg: Config, args, store: Store):
    statuses = args.status.split(",") if args.status else None
    rows = store.ranked(args.min_score if args.min_score is not None else cfg.search.min_score, statuses)
    if not rows:
        print("No ranked jobs. Run `coop-apply fetch` then `coop-apply rank`.")
    for job, match, status in rows:
        print(fmt_row(job, match, status))
    counts = store.status_counts()
    if counts:
        print("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))


def cmd_show(cfg: Config, args, store: Store):
    job = _require_job(store, args.job_id)
    match = store.get_match(job.id)
    app = store.get_application(job.id) or {}
    print(job.summary_text())
    print(f"\nURL: {job.url}\nApply: {job.apply_url or '(WaterlooWorks)'}\nStatus: {app.get('status', 'new')}")
    if match:
        print(f"\nScore {match.score} ({match.verdict.value})")
        print("Why:  " + "\n      ".join(match.reasons))
        print("Gaps: " + "\n      ".join(match.gaps or ["none"]))
    if app.get("cover_letter_path"):
        print(f"Cover letter: {app['cover_letter_path']}")


def make_letter(cfg: Config, store: Store, job: Job, profile, llm) -> tuple[Path, Path]:
    from .cover_letter import generate_cover_letter, save_cover_letter

    letter = generate_cover_letter(
        job, profile, llm, store.get_match(job.id), cfg.profile.cover_letter_notes, cfg.claude.effort_letter
    )
    txt, pdf = save_cover_letter(letter, profile, job, cfg.output_dir)
    store.set_status(job.id, letter_status(store, job.id), cover_letter_path=str(pdf))
    return txt, pdf


def letter_status(store: Store, job_id: str) -> Status:
    """A new cover letter makes a job 'prepared' - unless you've already applied to it."""
    current = (store.get_application(job_id) or {}).get("status")
    return Status.submitted if current == Status.submitted.value else Status.prepared


def cmd_letter(cfg: Config, args, store: Store):
    job = _require_job(store, args.job_id)
    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    txt, pdf = make_letter(cfg, store, job, profile, llm)
    print(txt.read_text())
    print(f"Saved {txt} and {pdf}")


def switches(cfg: Config) -> dict[str, bool]:
    ww = cfg.waterlooworks
    return {"Can do an 8-month work term": ww.can_do_8_month_term, "Has a full G driver's licence": ww.has_full_g_license}


def _is_waterlooworks_apply(job: Job) -> bool:
    from .apply.waterlooworks import needs_employer_website

    return job.source == "waterlooworks" and not needs_employer_website(job)


def application_link(job: Job) -> str:
    """Where to apply outside WaterlooWorks. For a WaterlooWorks posting that says 'apply on the
    employer's website' that's only the website it names - never the WaterlooWorks page itself."""
    if job.source == "waterlooworks":
        return job.apply_url if job.apply_url and "waterlooworks" not in job.apply_url.lower() else ""
    return job.apply_url or job.url


def _apply_one(cfg: Config, store: Store, ctx, page, job: Job, profile, llm, auto_submit: bool,
               external_auto: bool | None = None) -> Status:
    """Apply to one job (WaterlooWorks or a company website). Returns the job's new status.

    auto_submit=True is auto-apply: nothing waits for you - a step only you can do marks the
    job 'needs you' and moves on. external_auto: the same for company websites, if different.

    Raises StopRun (logged out of WaterlooWorks) and LLMError when the Claude API itself is
    unavailable (bad key, no credit, outage): those would fail every job the same way, so the
    job is left as it was instead of being marked failed.
    """
    from playwright.sync_api import Error as BrowserError

    from .apply import ApplyError, NeedsYou, StopRun
    from .apply.external import fill_form
    from .apply.formfill import fill_and_submit, make_answerer, make_planner
    from .apply.waterlooworks import apply_to_posting
    from .llm import LLMError
    from .sources.waterlooworks import save_snapshot, wait_for_login

    app = store.get_application(job.id) or {}
    if app.get("status") == Status.submitted.value:
        print("  already applied - skipping")
        return Status.submitted
    debug_dir = cfg.data_dir / "debug"
    applicant = applicant_from(cfg, profile)
    on_ww = _is_waterlooworks_apply(job)
    if external_auto is None:
        external_auto = auto_submit
    try:
        if on_ww:
            # before writing a letter: logged out means every WaterlooWorks job would fail
            if not wait_for_login(page, cfg.waterlooworks, timeout_s=0 if auto_submit else 300):
                raise StopRun("You're not logged in to WaterlooWorks (the session expired). Log in (Setup, "
                              "step 4), then try again.")
        elif not application_link(job):
            raise ApplyError("This posting asks you to apply on the employer's website, but no link was found in it. "
                             "Apply there yourself, then click 'Mark applied'." if job.source == "waterlooworks" else
                             "This job has no application link. Apply on the company's site yourself, then click "
                             "'Mark applied'.")
        pdf = Path(app["cover_letter_path"]) if app.get("cover_letter_path") else None
        if not pdf or not pdf.exists():
            print("  writing cover letter...")
            _, pdf = make_letter(cfg, store, job, profile, llm)
        txt = pdf.with_suffix(".txt")
        letter_text = txt.read_text(encoding="utf-8") if txt.exists() else ""
        print(f"  cover letter: {pdf}")

        if on_ww:
            answerer = make_answerer(llm, profile, applicant, cfg.profile.application_facts, switches(cfg))
            try:
                # the tailored cover letter is attached inside the application package
                ok = apply_to_posting(ctx, page, job, cfg.waterlooworks, pdf, confirm, auto_submit,
                                      debug_dir=debug_dir, answerer=answerer)
            except BrowserError as e:
                save_snapshot(page, debug_dir, "apply-error")
                raise ApplyError(f"The WaterlooWorks page didn't behave as expected: {str(e).splitlines()[0]} "
                                 f"(snapshot saved in {debug_dir})") from e
        else:
            job = job.model_copy(update={"apply_url": application_link(job)})
            resume = Path(cfg.profile.resume_path)
            planner = make_planner(llm, profile, applicant, cfg.profile.application_facts, switches(cfg))
            files = {"resume": resume if resume.exists() else None, "cover_letter": pdf}
            try:
                ok = fill_and_submit(
                    ctx, page, job, planner, files, letter_text, confirm, auto=external_auto,
                    prefill=lambda p: fill_form(p, applicant, resume, pdf, letter_text, log=lambda *_: None),
                )
            except BrowserError as e:
                save_snapshot(page, debug_dir, "external-apply-error")
                raise ApplyError(f"The company's site didn't behave as expected: {str(e).splitlines()[0]} "
                                 f"(snapshot saved in {debug_dir})") from e
    except NeedsYou as e:
        print(f"  -> needs you: {e}")
        store.set_status(job.id, Status.needs_you, notes=str(e))
        return Status.needs_you
    except StopRun:
        raise
    except LLMError as e:
        if e.systemic:
            raise
        print(f"  ! {e}")
        store.set_status(job.id, Status.failed, notes=str(e))
        return Status.failed
    except ApplyError as e:
        print(f"  ! {e}")
        store.set_status(job.id, Status.failed, notes=str(e))
        return Status.failed
    status = Status.submitted if ok else Status.prepared
    store.set_status(job.id, status, notes="" if ok else None)
    print("  submitted" if ok else "  not submitted (left as prepared)")
    return status


def cmd_apply(cfg: Config, args, store: Store):
    from .browser import browser_context, first_page

    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    from .apply import StopRun

    jobs = [_require_job(store, jid) for jid in args.job_ids]
    # apply.auto_submit is about WaterlooWorks; company-site forms only skip your review with --auto-submit
    ww_auto, ext_auto = args.auto_submit or cfg.apply.auto_submit, bool(args.auto_submit)
    with browser_context(cfg.browser_profile_dir, headless=cfg.apply.headless) as ctx:
        page = first_page(ctx)
        for job in jobs:
            print(f"\n== {job.title} - {job.company} ({job.id})")
            try:
                _apply_one(cfg, store, ctx, page, job, profile, llm, ww_auto, ext_auto)
            except StopRun as e:
                print(f"! {e}")
                return 1


def cmd_run(cfg: Config, args, store: Store):
    """fetch -> rank -> for each shortlisted job: cover letter + apply (with confirmation)."""
    if not args.no_fetch:
        if cmd_fetch(cfg, argparse.Namespace(ww=False, external=False, refresh=False), store):
            return 1
    cmd_rank(cfg, argparse.Namespace(rescore=False, limit=None), store)
    queue = store.ranked(cfg.search.min_score, statuses=["shortlisted", "prepared"])
    limit = args.limit or cfg.apply.max_per_run
    queue = queue[:limit]
    if not queue:
        print("No shortlisted jobs to apply to.")
        return
    print(f"\nUp to {len(queue)} applications this run:")
    for job, match, status in queue:
        print(fmt_row(job, match, status))
    from .browser import browser_context, first_page

    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    from .apply import StopRun

    auto = args.auto_submit or cfg.apply.auto_submit
    with browser_context(cfg.browser_profile_dir, headless=cfg.apply.headless) as ctx:
        page = first_page(ctx)
        for job, match, _ in queue:
            print(f"\n== [{match.score}] {job.title} - {job.company} ({job.id})")
            if not auto and not confirm("Prepare and apply to this job?"):
                store.set_status(job.id, Status.skipped)
                continue
            try:
                _apply_one(cfg, store, ctx, page, job, profile, llm, auto, bool(args.auto_submit))
            except StopRun as e:
                print(f"! {e}")
                return 1


def next_best_job(cfg: Config, store: Store, exclude: set[str] = frozenset(), waterlooworks_only: bool = False,
                  now=None, auto: bool = False, skip_waterlooworks: bool = False):
    """The highest-scoring shortlisted job you haven't applied to (or tried) yet, whose deadline
    hasn't passed and that has somewhere to apply.

    auto=True (auto-apply) also leaves out jobs from 'Cover letter from a job description':
    you pasted those for a letter, and may have applied to them yourself."""
    from .utils import deadline_passed

    for job, match, _ in store.ranked(cfg.search.min_score, statuses=["shortlisted", "prepared"]):
        if job.id in exclude or deadline_passed(job.deadline, now):
            continue
        on_ww = _is_waterlooworks_apply(job)
        if (waterlooworks_only and not on_ww) or (skip_waterlooworks and on_ww):
            continue
        if not on_ww and not application_link(job):
            continue  # e.g. a pasted description with no link, or "apply on the employer's website" with none given
        if auto and job.source == "manual":
            continue
        return job, match
    return None, None


def apply_loop(cfg: Config, store: Store, limit: int = 1, auto: bool = False, should_stop=lambda: False,
               sleep=None) -> dict[str, list[str]]:
    """Apply to the next best job(s), one at a time, in one browser window.

    limit: how many applications to submit (auto-apply) - or how many jobs to try (manual).
    auto=True: submit without asking; WaterlooWorks jobs, plus company-website jobs when
    apply.auto_include_external is on. Jobs that need you are marked 'needs you' and skipped.
    Returns {status: [job titles]} for the summary.
    """
    import time

    from .apply import StopRun
    from .browser import browser_context, first_page
    from .llm import LLMError

    sleep = sleep or time.sleep
    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    tried: set[str] = set()
    results: dict[str, list[str]] = {}
    ww_only = auto and not cfg.apply.auto_include_external
    skip_ww = False
    if auto and not cfg.waterlooworks.resume_document.strip():
        # every WaterlooWorks package would stop at "choose your resume" after uploading a letter
        print("Skipping WaterlooWorks jobs: set your resume's WaterlooWorks document name in Setup so auto-apply "
              "can pick it for the application package.")
        skip_ww = True
        if ww_only:
            return results
    max_attempts = limit * 3 if auto else limit  # don't loop forever on jobs that keep needing you
    with browser_context(cfg.browser_profile_dir, headless=cfg.apply.headless) as ctx:
        page = first_page(ctx)
        while not should_stop() and len(tried) < max_attempts:
            if auto and len(results.get(Status.submitted.value, [])) >= limit:
                print(f"\nReached the limit of {limit} applications for this run.")
                break
            job, match = next_best_job(cfg, store, tried, waterlooworks_only=ww_only, auto=auto,
                                       skip_waterlooworks=skip_ww)
            if job is None:
                print("\nNo more shortlisted jobs to apply to." if tried else
                      f"No shortlisted jobs to apply to (score {cfg.search.min_score}+, deadline not passed). "
                      "Find and rank more jobs, or lower the cutoff in Setup.")
                break
            if auto and tried and cfg.apply.pause_seconds > 0:  # be gentle with the sites
                for _ in range(cfg.apply.pause_seconds * 2):
                    if should_stop():
                        break
                    sleep(0.5)
                if should_stop():
                    break
            tried.add(job.id)
            print(f"\n== [{match.score}] {job.title} - {job.company} ({job.id})")
            try:
                status = _apply_one(cfg, store, ctx, page, job, profile, llm, auto)
            except StopRun as e:  # logged out of WaterlooWorks: the job is left as it was
                print(f"  ! {e}")
                if not auto or ww_only:
                    break
                print("  Carrying on with company-website jobs only.")
                skip_ww = True
                continue
            except LLMError as e:  # the Claude API itself is unavailable: stop, leave the job as it was
                print(f"\n! Stopping: {e}\nThis job was left as it was; nothing else was tried.")
                break
            results.setdefault(status.value, []).append(f"{job.title} - {job.company}")
    if auto or len(tried) > 1:
        print_summary(results)
    return results


def print_summary(results: dict[str, list[str]]) -> None:
    labels = [("submitted", "Submitted"), ("needs_you", "Needs you (open them in the app)"),
              ("failed", "Couldn't apply"), ("prepared", "Not submitted")]
    print("\n--- Summary ---")
    for key, label in labels:
        items = results.get(key, [])
        if items:
            print(f"{label}: {len(items)}")
            for t in items:
                print(f"  - {t}")
    if not any(results.values()):
        print("Nothing was applied to.")


def quick_letter_inputs(text: str, url: str = "") -> tuple[str, str]:
    """Tidy what was pasted: a link pasted into the description box is the link."""
    text, url = (text or "").strip(), (url or "").strip()
    if re.fullmatch(r"https?://\S+", text):
        text, url = "", url or text
    return text, url


def _same_link(a: str, b: str) -> bool:
    norm = lambda u: re.sub(r"#.*$", "", (u or "").strip()).rstrip("/").lower()  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def job_with_link(store: Store, url: str) -> Job | None:
    """A job already in your list with this posting / application link."""
    if not url:
        return None
    hits = [j for j in store.all_jobs() if _same_link(j.url, url) or _same_link(j.apply_url, url)]
    return hits[0] if len(hits) == 1 else None  # several (e.g. a job-list page): not one posting


def quick_letter_job_id(text: str, url: str = "", store: Store | None = None) -> str:
    """The job a pasted description becomes: the one already in your list with that link, else a new one."""
    from .utils import short_hash

    text, url = quick_letter_inputs(text, url)
    existing = job_with_link(store, url) if store is not None else None
    return existing.id if existing else f"manual:{short_hash(text[:4000], url)}"


def letter_from_description(cfg: Config, store: Store, text: str = "", url: str = "", title: str = "",
                            company: str = "") -> Job:
    """Cover letter from a pasted job description (or a link to one). Saves it as a job so the
    preview / changes / proofread tools work on it, and scores the fit."""
    from .cover_letter import extract_job_info
    from .matcher import score_job
    from .utils import fetch_page_text

    text, url = quick_letter_inputs(text, url)
    job_id = quick_letter_job_id(text, url, store)  # from what was pasted, so the app knows which job to open
    existing = store.get_job(job_id)
    if existing and existing.source != "manual":
        # the same posting is already in your list: write its letter there (no duplicate to apply to twice)
        print(f"  that posting is already in your list ({existing.title} - {existing.company})")
        if len(text) >= 80 and len(text) > len(existing.description):  # keep the fuller description
            existing = existing.model_copy(update={"description": text[:20000]})
            store.upsert_job(existing)
        llm = get_llm(cfg)
        profile, resume_hash = get_profile(cfg, llm)
        if not store.get_match(existing.id):
            store.save_match(existing.id, resume_hash, score_job(existing, profile, llm, cfg.claude.effort_rank))
        print("  writing the cover letter...")
        _, pdf = make_letter(cfg, store, existing, profile, llm)
        print(f"Cover letter for {existing.title} at {existing.company} saved to {pdf}")
        return existing
    if len(text) < 80 and url:  # a link (with maybe a title): read the posting from it
        print(f"  reading {url} ...")
        try:
            text = fetch_page_text(url)
        except Exception as e:
            raise FileNotFoundError(f"Couldn't open that link ({e}). Paste the job description text instead.")
        if len(text) < 300:
            raise FileNotFoundError("That page didn't have readable job text (it may need a login or load with "
                                    "scripts). Paste the job description text instead.")
    if len(text) < 80:
        raise FileNotFoundError("Paste the job description (at least a few sentences).")
    llm = get_llm(cfg)
    profile, resume_hash = get_profile(cfg, llm)
    print("  reading the job description...")
    info = extract_job_info(text, llm)
    job = Job(
        id=job_id, source="manual",
        title=(title or info.title or "Job").strip(), company=(company or info.company or "the company").strip(),
        location=info.location, deadline=info.deadline, description=info.description or text,
        url=url, apply_url=url,
    )
    store.upsert_job(job)
    print("  checking how well you fit...")
    store.save_match(job.id, resume_hash, score_job(job, profile, llm, cfg.claude.effort_rank))
    print("  writing the cover letter...")
    txt, pdf = make_letter(cfg, store, job, profile, llm)
    print(f"Cover letter for {job.title} at {job.company} saved to {pdf}")
    return job


def cmd_quick_letter(cfg: Config, args, store: Store):
    text = Path(args.file).read_text(encoding="utf-8") if args.file else ""
    if not text and not args.url:
        print("Paste the job description, then press Ctrl+Z and Enter (Windows) or Ctrl+D (Mac/Linux):")
        text = sys.stdin.read()
    job = letter_from_description(cfg, store, text, args.url or "", args.title or "", args.company or "")
    app = store.get_application(job.id) or {}
    print(Path(app["cover_letter_path"]).with_suffix(".txt").read_text(encoding="utf-8"))


def cmd_discover(cfg: Config, args, store: Store):
    """Search the web for postings at the top employers in your field."""
    from .sources.discover import discover_jobs

    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    jobs = discover_jobs(profile, llm, cfg.search.discover_region, cfg.search.discover_companies)
    new = 0
    for job in jobs:
        new += store.upsert_job(job)
        print(f"  + {job.title} - {job.company} ({job.location})  {job.url}")
    print(f"Found {len(jobs)} postings on company websites ({new} new). Rank them next.")


def cmd_mark(cfg: Config, args, store: Store):
    _require_job(store, args.job_id)
    store.set_status(args.job_id, Status(args.status))
    print(f"{args.job_id} -> {args.status}")


def cmd_ww_debug(cfg: Config, args):
    """Open a page in the session browser and dump its HTML + screenshot to tune selectors."""
    from .browser import browser_context, first_page

    out = cfg.data_dir / "debug"
    out.mkdir(parents=True, exist_ok=True)
    with browser_context(cfg.browser_profile_dir, headless=False) as ctx:
        page = first_page(ctx)
        page.goto(args.url or cfg.waterlooworks.postings_url)
        input("Navigate to the page you want to capture, then press Enter here...")
        (out / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out / "page.png"), full_page=True)
        sel = cfg.waterlooworks.selectors
        print(f"rows matching '{sel.row}': {page.locator(sel.row).count()}")
        print(f"detail matching '{sel.detail}': {page.locator(sel.detail).count()}")
        print(f"Saved {out / 'page.html'} and {out / 'page.png'}")


def _require_job(store: Store, job_id: str) -> Job:
    job = store.get_job(job_id)
    if not job:
        sys.exit(f"Unknown job id {job_id!r}. See `coop-apply list --min-score 0`.")
    return job


# ---- entry point -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coop-apply", description=__doc__)
    p.add_argument("--config", default="config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create config.toml from the example")
    s = sub.add_parser("resume", help="parse your resume into a profile")
    s.add_argument("--force", action="store_true", help="re-parse even if cached")
    sub.add_parser("login", help="log in to WaterlooWorks once (session is saved)")

    s = sub.add_parser("fetch", help="fetch postings")
    s.add_argument("--ww", action="store_true", help="WaterlooWorks only")
    s.add_argument("--external", action="store_true", help="external boards only")
    s.add_argument("--refresh", action="store_true", help="re-open postings already stored")

    s = sub.add_parser("rank", help="score postings against your resume")
    s.add_argument("--rescore", action="store_true")
    s.add_argument("--limit", type=int)

    s = sub.add_parser("list", help="show ranked postings")
    s.add_argument("--min-score", type=int)
    s.add_argument("--status", help="comma-separated, e.g. shortlisted,prepared")

    s = sub.add_parser("show", help="show one posting and its fit analysis")
    s.add_argument("job_id")
    s = sub.add_parser("letter", help="write a cover letter for a posting")
    s.add_argument("job_id")

    s = sub.add_parser("apply", help="apply to specific postings")
    s.add_argument("job_ids", nargs="+")
    s.add_argument("--auto-submit", action="store_true", help="submit WaterlooWorks applications without asking")

    s = sub.add_parser("run", help="fetch, rank, write letters and apply in one go")
    s.add_argument("--no-fetch", action="store_true")
    s.add_argument("--limit", type=int)
    s.add_argument("--auto-submit", action="store_true")

    sub.add_parser("discover", help="search the web for postings at the top employers in your field")

    s = sub.add_parser("quick-letter", help="write a cover letter from a pasted job description or a link")
    s.add_argument("--file", help="a text file with the job description (otherwise paste it)")
    s.add_argument("--url", help="a link to the job posting")
    s.add_argument("--title")
    s.add_argument("--company")

    s = sub.add_parser("mark", help="set a posting's status manually")
    s.add_argument("job_id")
    s.add_argument("status", choices=[st.value for st in Status])

    s = sub.add_parser("gui", help="open the point-and-click app in your browser")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("ww-debug", help="capture a page's HTML/screenshot to tune selectors")
    s.add_argument("--url")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    from .config import load_api_key
    from .llm import LLMError

    load_api_key(cfg.data_dir)
    if args.command == "gui":
        from .gui import serve

        return serve(args.config, port=args.port, open_browser=not args.no_browser) or 0
    no_store = {"init": cmd_init, "resume": cmd_resume, "login": cmd_login, "ww-debug": cmd_ww_debug}
    commands = {
        "fetch": cmd_fetch, "rank": cmd_rank, "list": cmd_list, "show": cmd_show, "letter": cmd_letter,
        "apply": cmd_apply, "run": cmd_run, "mark": cmd_mark, "discover": cmd_discover,
        "quick-letter": cmd_quick_letter,
    }
    try:
        if args.command in no_store:
            return no_store[args.command](cfg, args) or 0
        store = Store(cfg.db_path)
        try:
            return commands[args.command](cfg, args, store) or 0
        finally:
            store.close()
    except (LLMError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
