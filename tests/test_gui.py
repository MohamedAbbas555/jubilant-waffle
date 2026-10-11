"""The GUI server's API, plus the page itself in a real browser."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

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


def test_page_in_browser(server, browser):
    app, base = server
    page = browser.new_page()
    page.goto(base + "/")
    # no API key / resume yet -> opens on Setup
    page.wait_for_selector("#tab-setup:not([hidden])")
    page.fill("#apikey", "sk-ant-abc")
    page.click("button:has-text('Save key')")
    page.wait_for_selector("#keymsg:has-text('Saved')")
    page.set_input_files("#resumefile", files=[{"name": "cv.txt", "mimeType": "text/plain", "buffer": b"Jane Doe\nPython"}])
    page.click("button:has-text('Upload')")
    page.wait_for_function("document.querySelector('#resumemsg').textContent !== ''")
    assert "resume.txt" in page.inner_text("#resumemsg"), page.inner_text("#resumemsg")
    page.fill("[data-id='waterlooworks.resume_document']", "Resume F26")
    page.click("button:has-text('Save settings')")
    page.wait_for_selector("#setmsg:has-text('Saved')")
    assert load_config("config.toml").waterlooworks.resume_document == "Resume F26"

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
