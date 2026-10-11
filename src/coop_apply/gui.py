"""Point-and-click app: a small local web server that opens in your browser.

It only listens on 127.0.0.1, and every API call must carry a random per-launch
token, so other websites you have open can't drive it.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import traceback
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import cli
from .config import dump_config, load_config, save_api_key
from .db import Store
from .models import Status

HTML_FILE = Path(__file__).with_name("gui.html")

# Settings shown on the Setup page: (section, key, label, kind, help)
SETTINGS = [
    ("profile", "first_name", "First name", "text", ""),
    ("profile", "last_name", "Last name", "text", ""),
    ("profile", "email", "Email", "text", "Leave blank to use the one on your resume"),
    ("profile", "phone", "Phone", "text", ""),
    ("profile", "linkedin", "LinkedIn URL", "text", ""),
    ("profile", "github", "GitHub URL", "text", ""),
    ("profile", "website", "Personal website", "text", ""),
    ("profile", "cover_letter_notes", "Notes for cover letters", "textarea",
     "Your term (e.g. Winter 2027, 4 months), what to emphasize, tone"),
    ("waterlooworks", "resume_document", "WaterlooWorks resume name", "text",
     "Exactly as it appears in your WaterlooWorks Documents"),
    ("waterlooworks", "transcript_document", "WaterlooWorks grade report name", "text", ""),
    ("waterlooworks", "work_history_document", "WaterlooWorks work history name", "text", "Optional"),
    ("search", "min_score", "Shortlist jobs scoring at least", "int", "0-100. 70 is a good start"),
    ("search", "max_llm_scores", "Max jobs to score per ranking", "int", "Limits API cost per run"),
    ("search", "locations", "Only these locations", "list", "Comma-separated, e.g. Waterloo, Toronto, Remote. Blank = anywhere"),
    ("search", "exclude_keywords", "Skip titles containing", "list", "Comma-separated"),
    ("external", "greenhouse", "Greenhouse companies", "list", "Comma-separated slugs from boards.greenhouse.io/<slug>"),
    ("external", "lever", "Lever companies", "list", "Slugs from jobs.lever.co/<slug>"),
    ("external", "ashby", "Ashby companies", "list", "Slugs from jobs.ashbyhq.com/<slug>"),
    ("external", "title_keywords", "External titles must contain one of", "list", "Comma-separated"),
    ("apply", "max_per_run", "Max applications per run", "int", ""),
]


class LogTee:
    """Copies everything printed (by the CLI functions the GUI reuses) into the activity log."""

    def __init__(self, app: "App", original):
        self.app, self.original = app, original

    def write(self, text):
        if self.original:
            try:
                self.original.write(text)
            except Exception:
                pass
        self.app.append_log(text)
        return len(text)

    def flush(self):
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass


class App:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.log: deque[tuple[int, str]] = deque(maxlen=3000)
        self.seq = 0
        self._partial = ""
        self.task: str | None = None
        self.question: dict | None = None
        self._answer = threading.Event()
        self._answer_value = False

    # ---- config / store ----
    def cfg(self):
        return load_config(self.config_path)

    def store(self) -> Store:
        return Store(self.cfg().db_path)

    # ---- log ----
    def append_log(self, text: str):
        with self.lock:
            self._partial += text
            *lines, self._partial = self._partial.split("\n")
            for line in lines:
                self.seq += 1
                self.log.append((self.seq, line))

    def log_since(self, since: int) -> list[dict]:
        with self.lock:
            return [{"n": n, "text": t} for n, t in self.log if n > since]

    # ---- confirmations from background tasks ----
    def confirm(self, text: str) -> bool:
        self._answer.clear()
        with self.lock:
            self.question = {"id": secrets.token_hex(4), "text": text}
        self._answer.wait()
        with self.lock:
            self.question = None
        print(f"  > {'Yes' if self._answer_value else 'No'}")
        return self._answer_value

    def answer(self, qid: str, yes: bool):
        with self.lock:
            if not self.question or self.question["id"] != qid:
                return False
        self._answer_value = yes
        self._answer.set()
        return True

    # ---- background tasks ----
    def start(self, name: str, fn) -> tuple[bool, str]:
        with self.lock:
            if self.task:
                return False, f"Busy with '{self.task}'. Wait for it to finish."
            self.task = name

        def run():
            print(f"\n=== {name} ===")
            try:
                fn()
                print(f"=== {name}: done ===")
            except SystemExit as e:
                print(f"! {e}")
            except Exception as e:  # show the error in the log instead of killing the thread silently
                print(f"! {name} failed: {e}")
                traceback.print_exc(file=sys.stdout)
            finally:
                with self.lock:
                    self.task = None

        threading.Thread(target=run, daemon=True).start()
        return True, "started"

    def _with_store(self, fn):
        def go():
            from .llm import LLMError

            cfg = self.cfg()
            store = Store(cfg.db_path)
            try:
                fn(cfg, store)
            except (LLMError, FileNotFoundError) as e:
                print(f"! {e}")
            finally:
                store.close()

        return go

    def start_task(self, name: str, job_ids: list[str]) -> tuple[bool, str]:
        ns = argparse.Namespace
        tasks = {
            "resume": ("Read resume", lambda: self._resume()),
            "login": ("Log in to WaterlooWorks", lambda: cli.cmd_login(self.cfg(), ns())),
            "fetch": ("Find jobs", self._with_store(lambda c, s: cli.cmd_fetch(c, ns(ww=False, external=False, refresh=False), s))),
            "fetch_ww": ("Find WaterlooWorks jobs", self._with_store(lambda c, s: cli.cmd_fetch(c, ns(ww=True, external=False, refresh=False), s))),
            "fetch_ext": ("Find external jobs", self._with_store(lambda c, s: cli.cmd_fetch(c, ns(ww=False, external=True, refresh=False), s))),
            "rank": ("Rank jobs", self._with_store(lambda c, s: cli.cmd_rank(c, ns(rescore=False, limit=None), s))),
            "run": ("Apply to top matches", self._with_store(lambda c, s: cli.cmd_run(c, ns(no_fetch=True, limit=None, auto_submit=False), s))),
            "letter": ("Write cover letter", self._with_store(lambda c, s: [cli.cmd_letter(c, ns(job_id=j), s) for j in job_ids])),
            "apply": ("Apply", self._with_store(lambda c, s: cli.cmd_apply(c, ns(job_ids=job_ids, auto_submit=False), s))),
        }
        if name not in tasks:
            return False, f"unknown task {name}"
        if name in ("letter", "apply") and not job_ids:
            return False, "no job selected"
        if name in ("rank", "run", "letter", "apply") and not Path(self.cfg().profile.resume_path).exists():
            return False, "Upload your resume in the Setup tab first (step 2), then click \"Read my resume\"."
        if name in ("rank", "run", "letter", "apply", "resume") and not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "Add your Anthropic API key in the Setup tab first (step 1)."
        label, fn = tasks[name]
        return self.start(label, fn)

    def _resume(self):
        from .llm import LLMError

        try:
            cli.cmd_resume(self.cfg(), argparse.Namespace(force=True))
        except (LLMError, FileNotFoundError) as e:
            print(f"! {e}")

    # ---- read models for the page ----
    def state(self, since: int) -> dict:
        cfg = self.cfg()
        resume = Path(cfg.profile.resume_path)
        profile = None
        if resume.exists():
            from .resume import ProfileCache

            p, _ = ProfileCache(cfg.data_dir).load(resume)
            if p:
                profile = {"name": p.name, "program": f"{p.program} {p.academic_level}".strip(),
                           "skills": p.skills, "target_roles": p.target_roles}
        with self.lock:
            task, question = self.task, self.question
        store = Store(cfg.db_path)
        try:
            counts = store.status_counts()
            total = len(store.all_jobs())
        finally:
            store.close()
        return {
            "task": task,
            "question": question,
            "log": self.log_since(since),
            "setup": {
                "api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "config": self.config_path.exists(),
                "resume": resume.exists(),
                "resume_path": str(resume),
                "profile": profile,
                "ww_resume_doc": bool(cfg.waterlooworks.resume_document),
                "logged_in_before": cfg.browser_profile_dir.exists(),
            },
            "counts": {"jobs": total, **counts},
        }

    def jobs(self) -> list[dict]:
        store = self.store()
        try:
            ranked = store.ranked(0)
            seen = {j.id for j, _, _ in ranked}
            rows = [self._job_row(j, m, st) for j, m, st in ranked]
            rows += [self._job_row(j, None, (store.get_application(j.id) or {}).get("status", "new"))
                     for j in store.all_jobs() if j.id not in seen]
            return rows
        finally:
            store.close()

    @staticmethod
    def _job_row(job, match, status):
        return {"id": job.id, "title": job.title, "company": job.company, "location": job.location,
                "source": job.source, "deadline": job.deadline, "status": status,
                "score": match.score if match else None}

    def job_detail(self, job_id: str) -> dict | None:
        store = self.store()
        try:
            job = store.get_job(job_id)
            if not job:
                return None
            match = store.get_match(job_id)
            app = store.get_application(job_id) or {}
        finally:
            store.close()
        letter = ""
        pdf = app.get("cover_letter_path")
        if pdf and Path(pdf).with_suffix(".txt").exists():
            letter = Path(pdf).with_suffix(".txt").read_text(encoding="utf-8")
        return {
            "job": job.model_dump(),
            "match": match.model_dump(mode="json") if match else None,
            "status": app.get("status", "new"),
            "notes": app.get("notes") or "",
            "letter": letter,
            "has_pdf": bool(pdf and Path(pdf).exists()),
        }

    def save_letter(self, job_id: str, text: str) -> tuple[bool, str]:
        from .cover_letter import render_pdf

        store = self.store()
        try:
            app = store.get_application(job_id) or {}
            pdf = app.get("cover_letter_path")
            if not pdf:
                return False, "Write a cover letter first."
            pdf = Path(pdf)
            pdf.with_suffix(".txt").write_text(text, encoding="utf-8")
            render_pdf(text, pdf)
            return True, "saved"
        finally:
            store.close()

    def settings(self) -> dict:
        cfg = self.cfg()
        out = []
        for section, key, label, kind, help_ in SETTINGS:
            value = getattr(getattr(cfg, section), key)
            if kind == "list":
                value = ", ".join(value)
            out.append({"id": f"{section}.{key}", "label": label, "kind": kind, "help": help_, "value": value})
        return {"fields": out}

    def save_settings(self, values: dict) -> tuple[bool, str]:
        cfg = self.cfg()
        for section, key, label, kind, _ in SETTINGS:
            fid = f"{section}.{key}"
            if fid not in values:
                continue
            raw = values[fid]
            if kind == "int":
                try:
                    raw = int(str(raw).strip())
                except ValueError:
                    return False, f"{label} must be a number"
            elif kind == "list":
                raw = [x.strip() for x in str(raw).split(",") if x.strip()]
            else:
                raw = str(raw).strip() if kind == "text" else str(raw)
            setattr(getattr(cfg, section), key, raw)
        self.config_path.write_text(dump_config(cfg), encoding="utf-8")
        return True, "saved"

    def add_suggested(self) -> tuple[bool, str]:
        from .sources.suggested import merge_suggested

        cfg = self.cfg()
        ext = cfg.external
        merged, added = merge_suggested({"greenhouse": ext.greenhouse, "lever": ext.lever, "ashby": ext.ashby})
        ext.greenhouse, ext.lever, ext.ashby = merged["greenhouse"], merged["lever"], merged["ashby"]
        self.config_path.write_text(dump_config(cfg), encoding="utf-8")
        return True, f"Added {added} companies"

    def save_resume(self, filename: str, data: bytes) -> tuple[bool, str]:
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pdf", ".docx", ".txt", ".md"):
            return False, "Use a .pdf, .docx, .txt or .md file"
        if not data:
            return False, "Empty file"
        target = Path(f"resume{suffix}")
        target.write_bytes(data)
        cfg = self.cfg()
        cfg.profile.resume_path = target.as_posix()
        self.config_path.write_text(dump_config(cfg), encoding="utf-8")
        return True, f"Saved as {target}"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _authorized(self, query) -> bool:
            token = self.headers.get("X-Token") or (query.get("token") or [""])[0]
            return secrets.compare_digest(token, app.token)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                html = HTML_FILE.read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if not self._authorized(q):
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            if url.path == "/api/state":
                return self._json(app.state(int((q.get("since") or ["0"])[0])))
            if url.path == "/api/jobs":
                return self._json({"jobs": app.jobs()})
            if url.path == "/api/job":
                detail = app.job_detail((q.get("id") or [""])[0])
                return self._json(detail) if detail else self._json({"error": "not found"}, 404)
            if url.path == "/api/settings":
                return self._json(app.settings())
            if url.path == "/letter.pdf":
                detail = app.job_detail((q.get("id") or [""])[0])
                store = app.store()
                try:
                    a = store.get_application((q.get("id") or [""])[0]) or {}
                finally:
                    store.close()
                pdf = a.get("cover_letter_path")
                if detail and pdf and Path(pdf).exists():
                    return self._send(200, Path(pdf).read_bytes(), "application/pdf")
                return self._json({"error": "no letter"}, 404)
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            url = urlparse(self.path)
            if not self._authorized(parse_qs(url.query)):
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            if url.path == "/api/resume":
                ok, msg = app.save_resume(unquote(self.headers.get("X-Filename", "")), self._body())
                return self._json({"ok": ok, "message": msg})
            try:
                body = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if url.path == "/api/task":
                ok, msg = app.start_task(body.get("name", ""), body.get("job_ids") or [])
                return self._json({"ok": ok, "message": msg})
            if url.path == "/api/answer":
                return self._json({"ok": app.answer(body.get("id", ""), bool(body.get("yes")))})
            if url.path == "/api/status":
                store = app.store()
                try:
                    if not store.get_job(body.get("id", "")):
                        return self._json({"ok": False, "message": "unknown job"})
                    store.set_status(body["id"], Status(body.get("status")))
                finally:
                    store.close()
                return self._json({"ok": True})
            if url.path == "/api/letter":
                ok, msg = app.save_letter(body.get("id", ""), body.get("text", ""))
                return self._json({"ok": ok, "message": msg})
            if url.path == "/api/settings":
                ok, msg = app.save_settings(body.get("values") or {})
                return self._json({"ok": ok, "message": msg})
            if url.path == "/api/suggest":
                ok, msg = app.add_suggested()
                return self._json({"ok": ok, "message": msg})
            if url.path == "/api/apikey":
                key = (body.get("key") or "").strip()
                if not key.startswith("sk-ant-"):
                    return self._json({"ok": False, "message": "That doesn't look like an Anthropic key (sk-ant-...)"})
                save_api_key(app.cfg().data_dir, key)
                return self._json({"ok": True, "message": "saved"})
            self._json({"error": "not found"}, 404)

    return Handler


def serve(config_path: str = "config.toml", port: int = 8765, open_browser: bool = True):
    app = App(config_path)
    if not app.config_path.exists():
        app.config_path.write_text(dump_config(app.cfg()), encoding="utf-8")
    cli.confirm_hook = app.confirm
    sys.stdout = LogTee(app, sys.__stdout__)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{server.server_port}/"
    sys.__stdout__.write(f"coop-apply is running at {url}\nKeep this window open. Press Ctrl+C to quit.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        sys.stdout = sys.__stdout__
