"""apply_loop (the 'apply to next best job' button and auto-apply) and next_best_job,
with the browser and the per-job apply step stubbed out."""

from contextlib import contextmanager
from datetime import datetime

import pytest

from coop_apply import cli
from coop_apply.config import Config
from coop_apply.db import Store
from coop_apply.models import Job, MatchResult, Status, Verdict


@pytest.fixture
def setup(monkeypatch, tmp_path):
    import coop_apply.browser as b

    class Ctx:
        pages = ["page"]

    @contextmanager
    def fake_browser(*a, **k):
        yield Ctx()

    monkeypatch.setattr(b, "browser_context", fake_browser)
    monkeypatch.setattr(cli, "get_llm", lambda cfg: object())
    monkeypatch.setattr(cli, "get_profile", lambda cfg, llm=None, force=False: ("profile", "h"))
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.apply.pause_seconds = 3
    cfg.waterlooworks.resume_document = "Jane_Resume"
    store = Store(":memory:")
    return cfg, store


def add(store, jid, score, source="waterlooworks", status=Status.shortlisted, deadline="", url="https://x", extra=None):
    store.upsert_job(Job(id=jid, source=source, title=jid, company="C", deadline=deadline, url=url,
                         apply_url=url if source != "waterlooworks" else "", extra=extra or {}))
    store.save_match(jid, "h", MatchResult(score=score, verdict=Verdict.good, reasons=[], gaps=[], keywords=[]))
    store.set_status(jid, status)


def test_auto_counts_submissions_skips_needs_you_and_pauses(setup, monkeypatch):
    cfg, store = setup
    for i, score in enumerate([95, 90, 85, 80, 75]):
        add(store, f"ww:{i}", score)
    outcome = {"ww:0": Status.needs_you, "ww:1": Status.submitted, "ww:2": Status.failed, "ww:3": Status.submitted}
    order = []

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto, external_auto=None):
        order.append(job.id)
        store.set_status(job.id, outcome.get(job.id, Status.submitted))
        return outcome.get(job.id, Status.submitted)

    monkeypatch.setattr(cli, "_apply_one", fake_apply)
    sleeps = []
    results = cli.apply_loop(cfg, store, limit=2, auto=True, sleep=sleeps.append)
    assert order == ["ww:0", "ww:1", "ww:2", "ww:3"]  # best first; stops after 2 *submitted*
    assert results == {"needs_you": ["ww:0 - C"], "submitted": ["ww:1 - C", "ww:3 - C"], "failed": ["ww:2 - C"]}
    assert len(sleeps) == 3 * cfg.apply.pause_seconds * 2  # paused between jobs, not before the first


def test_stop_switch_ends_the_run(setup, monkeypatch):
    cfg, store = setup
    add(store, "ww:1", 90)
    add(store, "ww:2", 80)
    stop = {"now": False}

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto, external_auto=None):
        stop["now"] = True  # you switch it off while the first job is being applied to
        store.set_status(job.id, Status.submitted)
        return Status.submitted

    monkeypatch.setattr(cli, "_apply_one", fake_apply)
    results = cli.apply_loop(cfg, store, limit=10, auto=True, should_stop=lambda: stop["now"], sleep=lambda s: None)
    assert results == {"submitted": ["ww:1 - C"]}


def test_next_best_job_filters():
    cfg = Config()
    s = Store(":memory:")
    add(s, "ww:old", 99, deadline="Oct 1, 2026 9:00 AM")  # deadline passed
    add(s, "manual:nolink", 95, source="manual", url="")  # pasted description without a link
    add(s, "gh:a:1", 90, source="greenhouse")
    add(s, "ww:ok", 85, deadline="Oct 30, 2026 9:00 AM")
    now = datetime(2026, 10, 12, 12, 0)
    assert cli.next_best_job(cfg, s, now=now)[0].id == "gh:a:1"
    assert cli.next_best_job(cfg, s, waterlooworks_only=True, now=now)[0].id == "ww:ok"


def test_auto_without_external_only_does_waterlooworks(setup, monkeypatch):
    cfg, store = setup
    cfg.apply.auto_include_external = False
    add(store, "gh:a:1", 95, source="greenhouse")
    add(store, "ww:1", 80)
    seen = []

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto, external_auto=None):
        seen.append(job.id)
        store.set_status(job.id, Status.submitted)
        return Status.submitted

    monkeypatch.setattr(cli, "_apply_one", fake_apply)
    cli.apply_loop(cfg, store, limit=5, auto=True, sleep=lambda s: None)
    assert seen == ["ww:1"]


def status(store, jid):
    return store.get_application(jid)["status"]


def test_api_outage_stops_the_run_and_leaves_jobs_alone(setup, monkeypatch):
    from coop_apply.llm import LLMError

    cfg, store = setup
    for i in range(5):
        add(store, f"ww:{i}", 90 - i)
    tried = []

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto, external_auto=None):
        tried.append(job.id)
        raise LLMError("Could not reach the Anthropic API.", systemic=True)

    monkeypatch.setattr(cli, "_apply_one", fake_apply)
    results = cli.apply_loop(cfg, store, limit=3, auto=True, sleep=lambda s: None)
    assert tried == ["ww:0"] and results == {}
    assert {status(store, f"ww:{i}") for i in range(5)} == {"shortlisted"}  # still in the queue for next time


def test_logged_out_of_waterlooworks_carries_on_with_company_sites(setup, monkeypatch):
    from coop_apply.apply import StopRun

    cfg, store = setup
    add(store, "ww:1", 95)
    add(store, "ww:2", 90)
    add(store, "gh:a:1", 85, source="greenhouse")
    tried = []

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto, external_auto=None):
        tried.append(job.id)
        if job.source == "waterlooworks":
            raise StopRun("You're not logged in to WaterlooWorks")
        store.set_status(job.id, Status.submitted)
        return Status.submitted

    monkeypatch.setattr(cli, "_apply_one", fake_apply)
    results = cli.apply_loop(cfg, store, limit=5, auto=True, sleep=lambda s: None)
    assert tried == ["ww:1", "gh:a:1"]  # tried WaterlooWorks once, then only company sites
    assert results == {"submitted": ["gh:a:1 - C"]}
    assert status(store, "ww:1") == status(store, "ww:2") == "shortlisted"


def test_auto_skips_waterlooworks_without_a_resume_name(setup, monkeypatch):
    cfg, store = setup
    cfg.waterlooworks.resume_document = ""
    add(store, "ww:1", 95)
    add(store, "gh:a:1", 85, source="greenhouse")
    seen = []

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto, external_auto=None):
        seen.append(job.id)
        store.set_status(job.id, Status.submitted)
        return Status.submitted

    monkeypatch.setattr(cli, "_apply_one", fake_apply)
    cli.apply_loop(cfg, store, limit=5, auto=True, sleep=lambda s: None)
    assert seen == ["gh:a:1"]


def test_next_best_job_skips_unusable_links_and_pasted_jobs_in_auto():
    cfg = Config()
    s = Store(":memory:")
    # WaterlooWorks says "apply on the employer's website" but gives no link: nowhere to apply
    add(s, "ww:site", 99, extra={"Application Method": "Through Employer Website"},
        url="https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm")
    add(s, "manual:abc", 95, source="manual", url="https://careers.example.com/job/1")
    add(s, "gh:a:1", 90, source="greenhouse")
    now = datetime(2026, 10, 12, 12, 0)
    assert cli.next_best_job(cfg, s, now=now)[0].id == "manual:abc"  # you can still apply to it yourself
    assert cli.next_best_job(cfg, s, now=now, auto=True)[0].id == "gh:a:1"  # auto-apply leaves it to you


def test_employer_website_posting_without_a_link_is_not_sent_to_the_form_filler(tmp_path, profile):
    cfg = Config()
    cfg.data_dir = tmp_path
    store = Store(":memory:")
    add(store, "ww:site", 99, extra={"Application Method": "Through Employer Website"},
        url="https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm")
    job = store.get_job("ww:site")
    st = cli._apply_one(cfg, store, ctx=None, page=None, job=job, profile=profile, llm=None, auto_submit=True)
    assert st == Status.failed
    assert "employer's website" in store.get_application("ww:site")["notes"]


def test_systemic_api_error_leaves_the_job_as_it_was(tmp_path, monkeypatch, profile):
    from coop_apply.llm import LLMError

    cfg = Config()
    cfg.data_dir = tmp_path
    store = Store(":memory:")
    add(store, "gh:a:1", 90, source="greenhouse")

    def no_api(*a, **k):
        raise LLMError("Anthropic rejected the API key", systemic=True)

    monkeypatch.setattr(cli, "make_letter", no_api)
    with pytest.raises(LLMError):
        cli._apply_one(cfg, store, None, None, store.get_job("gh:a:1"), profile, None, auto_submit=True)
    assert status(store, "gh:a:1") == "shortlisted"


def test_midnight_deadline_has_passed_that_morning():
    from coop_apply.utils import deadline_passed

    morning = datetime(2026, 10, 21, 10, 0)
    assert deadline_passed("Oct 21, 2026 12:00 AM", morning) is True
    assert deadline_passed("Oct 21, 2026", morning) is False  # date only: open all day
