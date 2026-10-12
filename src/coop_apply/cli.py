"""Command-line interface: ``coop-apply <command>``."""

from __future__ import annotations

import argparse
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
    store.set_status(job.id, Status.prepared, cover_letter_path=str(pdf))
    return txt, pdf


def cmd_letter(cfg: Config, args, store: Store):
    job = _require_job(store, args.job_id)
    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    txt, pdf = make_letter(cfg, store, job, profile, llm)
    print(txt.read_text())
    print(f"Saved {txt} and {pdf}")


def _apply_one(cfg: Config, store: Store, ctx, page, job: Job, profile, llm, auto_submit: bool) -> bool:
    from .apply.external import apply_external
    from .apply.waterlooworks import ApplyError, apply_to_posting, needs_employer_website
    from .sources.waterlooworks import wait_for_login

    app = store.get_application(job.id) or {}
    if app.get("status") == Status.submitted.value:
        print("  already applied - skipping")
        return False
    pdf = Path(app["cover_letter_path"]) if app.get("cover_letter_path") else None
    if not pdf or not pdf.exists():
        print("  writing cover letter...")
        _, pdf = make_letter(cfg, store, job, profile, llm)
    txt = pdf.with_suffix(".txt")
    print(f"  cover letter: {pdf}")

    try:
        if job.source == "waterlooworks" and not needs_employer_website(job):
            if not wait_for_login(page, cfg.waterlooworks):
                raise ApplyError("Not logged in to WaterlooWorks; run `coop-apply login`.")
            from playwright.sync_api import Error as BrowserError

            debug_dir = cfg.data_dir / "debug"
            try:
                # the tailored cover letter is attached inside the application package
                ok = apply_to_posting(ctx, page, job, cfg.waterlooworks, pdf, confirm, auto_submit,
                                      debug_dir=debug_dir)
            except BrowserError as e:
                from .sources.waterlooworks import save_snapshot

                save_snapshot(page, debug_dir, "apply-error")
                raise ApplyError(f"The WaterlooWorks page didn't behave as expected: {str(e).splitlines()[0]} "
                                 f"(snapshot saved in {debug_dir})") from e
        else:
            if job.source == "waterlooworks" and not job.apply_url:
                raise ApplyError("This posting asks you to apply on the employer's website, but no URL was found in it.")
            ok = apply_external(
                page, job, applicant_from(cfg, profile), Path(cfg.profile.resume_path), pdf,
                txt.read_text() if txt.exists() else None, confirm,
            )
    except ApplyError as e:
        print(f"  ! {e}")
        store.set_status(job.id, Status.failed, notes=str(e))
        return False
    store.set_status(job.id, Status.submitted if ok else Status.prepared)
    print("  submitted" if ok else "  left as prepared")
    return ok


def cmd_apply(cfg: Config, args, store: Store):
    from .browser import browser_context, first_page

    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    jobs = [_require_job(store, jid) for jid in args.job_ids]
    with browser_context(cfg.browser_profile_dir, headless=cfg.apply.headless) as ctx:
        page = first_page(ctx)
        for job in jobs:
            print(f"\n== {job.title} - {job.company} ({job.id})")
            _apply_one(cfg, store, ctx, page, job, profile, llm, args.auto_submit or cfg.apply.auto_submit)


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
    auto = args.auto_submit or cfg.apply.auto_submit
    with browser_context(cfg.browser_profile_dir, headless=cfg.apply.headless) as ctx:
        page = first_page(ctx)
        for job, match, _ in queue:
            print(f"\n== [{match.score}] {job.title} - {job.company} ({job.id})")
            if not auto and not confirm("Prepare and apply to this job?"):
                store.set_status(job.id, Status.skipped)
                continue
            _apply_one(cfg, store, ctx, page, job, profile, llm, auto)


def next_best_job(cfg: Config, store: Store, exclude: set[str] = frozenset(), waterlooworks_only: bool = False):
    """The highest-scoring shortlisted job you haven't applied to (or tried) yet."""
    from .apply.waterlooworks import needs_employer_website

    for job, match, _ in store.ranked(cfg.search.min_score, statuses=["shortlisted", "prepared"]):
        if job.id in exclude:
            continue
        if waterlooworks_only and (job.source != "waterlooworks" or needs_employer_website(job)):
            continue
        return job, match
    return None, None


def apply_loop(cfg: Config, store: Store, limit: int = 1, auto: bool = False, should_stop=lambda: False) -> int:
    """Apply to the next best job(s), one at a time, in one browser window.

    auto=True: submit without asking, and only WaterlooWorks jobs (external sites need you to
    finish their forms). Returns how many jobs were attempted.
    """
    from .browser import browser_context, first_page

    llm = get_llm(cfg)
    profile, _ = get_profile(cfg, llm)
    tried: set[str] = set()
    with browser_context(cfg.browser_profile_dir, headless=cfg.apply.headless) as ctx:
        page = first_page(ctx)
        while len(tried) < limit and not should_stop():
            job, match = next_best_job(cfg, store, tried, waterlooworks_only=auto)
            if job is None:
                print("No more shortlisted jobs to apply to." if tried else
                      f"No shortlisted jobs to apply to (score {cfg.search.min_score}+). Rank more jobs or lower the cutoff.")
                break
            tried.add(job.id)
            print(f"\n== [{match.score}] {job.title} - {job.company} ({job.id})")
            _apply_one(cfg, store, ctx, page, job, profile, llm, auto)
    return len(tried)


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
