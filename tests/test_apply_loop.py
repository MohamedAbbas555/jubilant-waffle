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

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto):
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

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto):
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

    def fake_apply(cfg, store, ctx, page, job, profile, llm, auto):
        seen.append(job.id)
        store.set_status(job.id, Status.submitted)
        return Status.submitted

    monkeypatch.setattr(cli, "_apply_one", fake_apply)
    cli.apply_loop(cfg, store, limit=5, auto=True, sleep=lambda s: None)
    assert seen == ["ww:1"]
