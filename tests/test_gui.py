"""The GUI server's API, plus the page itself in a real browser."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from pathlib import Path

from coop_apply import cli
from coop_apply.config import load_config
from coop_apply.db import Store
from coop_apply.gui import App, LogTee, make_handler
from coop_apply.models import Job, MatchResult, Verdict


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = App("config.toml")
    store = Store(app.cfg().db_path)
    store.upsert_job(Job(id="ww:111111", source="waterlooworks", title="Backend Intern", company="Maple",
                         location="Waterloo", description="Python APIs", deadline="Oct 20"))
    store.upsert_job(Job(id="gh:acme:2", source="greenhouse", title="Data Intern", company="Acme"))
    store.save_match("ww:111111", "h", MatchResult(score=88, verdict=Verdict.strong, reasons=["Built Python APIs"],
                                                   gaps=["Kubernetes"], keywords=["REST"]))
    store.close()
    cli.confirm_hook = app.confirm
    # no network in tests: treat keys ending in "bad" as rejected by Anthropic
    monkeypatch.setattr("coop_apply.gui.check_api_key",
                        lambda key, model: (False, "Anthropic rejected this key") if key.endswith("bad") else (True, "Key works."))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    cli.confirm_hook = None


def call(base, path, token, body=None):
    req = urllib.request.Request(base + path, headers={"X-Token": token} if token else {},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_api_requires_token(server):
    app, base = server
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base, "/api/jobs", "wrong")
    assert e.value.code == 403


def test_jobs_settings_and_key(server):
    app, base = server
    jobs = call(base, "/api/jobs", app.token)["jobs"]
    assert [(j["id"], j["score"]) for j in jobs] == [("ww:111111", 88), ("gh:acme:2", None)]

    r = call(base, "/api/settings", app.token, {"values": {"search.min_score": "75", "external.greenhouse": "stripe, databricks",
                                                           "profile.cover_letter_notes": "Winter 2027"}})
    assert r["ok"]
    cfg = load_config("config.toml")
    assert cfg.search.min_score == 75 and cfg.external.greenhouse == ["stripe", "databricks"]
    assert call(base, "/api/settings", app.token, {"values": {"search.min_score": "lots"}})["ok"] is False

    assert call(base, "/api/apikey", app.token, {"key": "nope"})["ok"] is False
    assert call(base, "/api/apikey", app.token, {"key": "sk-ant-bad"})["ok"] is False
    assert call(base, "/api/state", app.token)["setup"]["api_key"] is False  # rejected key isn't saved
    assert call(base, "/api/apikey", app.token, {"key": "sk-ant-test123"})["ok"]
    assert call(base, "/api/state", app.token)["setup"]["api_key"] is True


def test_background_task_confirmation_roundtrip(server, monkeypatch):
    app, base = server
    # (patched here, not in the fixture: pytest re-installs its own capture before the test body)
    monkeypatch.setattr("sys.stdout", LogTee(app, None))
    result = {}
    app.start("Demo", lambda: result.setdefault("answer", cli.confirm("Submit it now?")))
    for _ in range(50):
        q = call(base, "/api/state", app.token)["question"]
        if q:
            break
        threading.Event().wait(0.05)
    assert q["text"] == "Submit it now?"
    assert call(base, "/api/answer", app.token, {"id": q["id"], "yes": True})["ok"]
    for _ in range(50):
        if not app.task:
            break
        threading.Event().wait(0.05)
    assert result["answer"] is True
    log = "\n".join(l["text"] for l in call(base, "/api/state", app.token)["log"])
    assert "=== Demo: done ===" in log


def test_page_in_browser(server, browser, monkeypatch):
    app, base = server
    read = []
    monkeypatch.setattr(cli, "cmd_resume", lambda cfg, args: read.append(cfg.profile.resume_path))
    page = browser.new_page()
    page.goto(base + "/")
    # no API key / resume yet -> opens on Setup
    page.wait_for_selector("#tab-setup:not([hidden])")
    page.fill("#apikey", "sk-ant-abc")
    page.click("button:has-text('Save key')")
    page.wait_for_selector("#keymsg:has-text('Key works')")
    page.wait_for_selector("#keystatus:has-text('…-abc')")
    # choosing a file uploads it straight away (names with spaces/brackets/accents included)
    page.set_input_files("#resumefile", files=[{"name": "Jane Doé (Eng).txt", "mimeType": "text/plain", "buffer": b"Jane Doe\nPython"}])
    page.wait_for_selector("#resumemsg:has-text('resume.txt')")
    assert (Path.cwd() / "resume.txt").read_bytes() == b"Jane Doe\nPython"
    page.wait_for_function("() => true")
    for _ in range(100):
        if read and not app.task:
            break
        threading.Event().wait(0.05)
    assert read == ["resume.txt"]  # reading started automatically after the upload
    page.fill("[data-id='waterlooworks.resume_document']", "Resume F26")
    page.click("button:has-text('Save settings')")
    page.wait_for_selector("#setmsg:has-text('Saved')")
    assert load_config("config.toml").waterlooworks.resume_document == "Resume F26"
    # the toggle switches save as soon as they're flipped
    page.click("label.switch:has-text('8-month')")
    page.wait_for_function("() => true")
    for _ in range(50):
        if load_config("config.toml").waterlooworks.can_do_8_month_term:
            break
        threading.Event().wait(0.05)
    assert load_config("config.toml").waterlooworks.can_do_8_month_term is True
    assert load_config("config.toml").waterlooworks.has_full_g_license is False

    page.click("nav >> text=Jobs")
    page.wait_for_selector("tr.job")
    assert page.locator("tr.job").count() == 1  # default filter 70+
    page.select_option("#fmin", "-1")
    assert page.locator("tr.job").count() == 2
    page.click("tr.job:has-text('Backend Intern')")
    page.wait_for_selector("#detail:has-text('Built Python APIs')")
    assert "Kubernetes" in page.inner_text("#detail")

    app.start("Demo", lambda: cli.confirm("Submit the application?"))
    page.wait_for_selector(".modal-bg.show:has-text('Submit the application?')")
    page.click(".modal >> text=Yes")
    page.wait_for_selector("#pill:has-text('Ready')")
    page.close()


def test_add_suggested_companies(server):
    app, base = server
    r = call(base, "/api/suggest", app.token, {})
    assert r["ok"] and r["message"].startswith("Added")
    assert "stripe" in load_config("config.toml").external.greenhouse
    assert call(base, "/api/suggest", app.token, {})["message"] == "Added 0 companies"


def test_tasks_needing_resume_are_blocked_with_help(server):
    app, base = server
    r = call(base, "/api/task", app.token, {"name": "rank"})
    assert r["ok"] is False and "Setup tab" in r["message"]



def test_letter_preview_revise_and_proofread(server, browser, monkeypatch, profile):
    from coop_apply import cover_letter
    from coop_apply.models import CoverLetter, LetterReview, Status

    app, base = server
    Path("resume.txt").write_text("Jane")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = app.cfg()
    cfg.profile.resume_path = "resume.txt"
    app.config_path.write_text(__import__("coop_apply.config", fromlist=["dump_config"]).dump_config(cfg))
    monkeypatch.setattr(cli, "get_llm", lambda c: object())
    monkeypatch.setattr(cli, "get_profile", lambda c, llm=None, force=False: (profile, "h"))
    asked = []
    monkeypatch.setattr(cover_letter, "revise_cover_letter", lambda job, prof, text, changes, llm, effort="high": (
        asked.append(changes) or CoverLetter(salutation="Dear Hiring Manager,", paragraphs=["REVISED BODY"], closing="Sincerely,")))
    monkeypatch.setattr(cover_letter, "review_cover_letter", lambda job, prof, text, llm, effort="medium": LetterReview(
        overall="Good start.", suggestions=["Cut the first sentence", "Name the stormwater project"]))

    store = Store(cfg.db_path)
    job = store.get_job("ww:111111")
    _, pdf = cover_letter.save_cover_letter(
        CoverLetter(salutation="Dear Hiring Manager,", paragraphs=["ORIGINAL BODY"], closing="Sincerely,"), profile, job, cfg.output_dir)
    store.set_status(job.id, Status.prepared, cover_letter_path=str(pdf))
    store.close()

    page = browser.new_page()
    page.goto(base + "/")
    page.click("nav >> text=Jobs")
    page.click("tr.job:has-text('Backend Intern')")
    page.wait_for_selector(".paper:has-text('ORIGINAL BODY')")

    page.click("button.qbtn:has-text('Make it shorter')")
    page.fill("#changes", page.input_value("#changes") + "\nMention GIS")
    page.click("text=Rewrite with these changes")
    page.wait_for_selector(".paper:has-text('REVISED BODY')")
    assert asked == ["Make it shorter\nMention GIS"]
    assert page.input_value("#changes") == ""  # cleared after sending

    page.click("text=Proofread for me")
    page.wait_for_selector(".sugg:has-text('Cut the first sentence')")
    page.uncheck(".sugg:has-text('Name the stormwater') input")
    page.click("text=Apply checked suggestions")
    page.wait_for_function("document.querySelectorAll('.sugg').length === 0")  # old review cleared after rewrite
    assert asked[-1] == "- Cut the first sentence"

    page.click("button:has-text('Edit text')")
    assert "REVISED BODY" in page.input_value("#letter")
    page.close()


def test_already_applied_jobs_sink_and_cannot_be_opened(server, browser):
    from coop_apply.models import Status

    app, base = server
    store = Store(app.cfg().db_path)
    store.upsert_job(Job(id="ww:492209", source="waterlooworks", title="Project Coordinator Student",
                         company="Graham", applicants=96, applied_on_site=True))
    store.save_match("ww:492209", "h", MatchResult(score=95, verdict=Verdict.strong, reasons=[], gaps=[], keywords=[]))
    store.set_status("ww:492209", Status.submitted)
    store.close()
    assert call(base, "/api/task", app.token, {"name": "apply", "job_ids": ["ww:492209"]})["message"] \
        == "You've already applied to this job."

    page = browser.new_page()
    page.goto(base + "/")
    page.click("nav >> text=Jobs")
    page.select_option("#fmin", "-1")
    page.wait_for_selector("tr.job.done")
    titles = page.locator("tr.job b").all_inner_texts()
    assert titles[-1] == "Project Coordinator Student"  # highest score, but at the bottom
    assert "already applied" in page.inner_text("tr.job.done") and "96" in page.inner_text("tr.job.done")
    page.click("tr.job.done", force=True)
    page.wait_for_timeout(300)
    assert page.locator("#detail").is_hidden()  # not clickable
    page.close()


def test_auto_apply_switch(server, monkeypatch):
    app, base = server
    Path("resume.txt").write_text("Jane")
    cfg = app.cfg()
    cfg.profile.resume_path = "resume.txt"
    app.config_path.write_text(__import__("coop_apply.config", fromlist=["dump_config"]).dump_config(cfg))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    seen = {}

    def fake_loop(cfg, store, limit=1, auto=False, should_stop=lambda: False):
        seen.update(limit=limit, auto=auto, answer=cli.confirm("Unknown question?"))
        while not should_stop():  # keeps applying until switched off
            threading.Event().wait(0.05)
        return 3

    monkeypatch.setattr(cli, "apply_loop", fake_loop)
    assert call(base, "/api/task", app.token, {"name": "auto"})["ok"]
    for _ in range(100):
        if call(base, "/api/state", app.token)["auto_on"] and seen:
            break
        threading.Event().wait(0.05)
    assert seen == {"limit": cfg.apply.max_per_run, "auto": True, "answer": False}  # never waits on you
    assert call(base, "/api/task", app.token, {"name": "auto_stop"})["ok"]
    for _ in range(100):
        if not app.task:
            break
        threading.Event().wait(0.05)
    assert call(base, "/api/state", app.token)["auto_on"] is False
    assert cli.confirm_hook == app.confirm  # normal Yes/No questions are back
