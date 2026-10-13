"""Exercise the Claude-backed flows against a local fake of the Messages API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest

from coop_apply.cover_letter import generate_cover_letter, save_cover_letter
from coop_apply.llm import LLM, LLMError
from coop_apply.matcher import score_job
from coop_apply.models import Job
from coop_apply.resume import load_or_parse


class FakeAPI:
    def __init__(self):
        self.requests = []
        self.replies = []  # queue of (output dict, stop_reason)
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                api.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                output, stop = api.replies.pop(0)
                text = output if isinstance(output, str) else json.dumps(output)
                if body.get("stream"):
                    return self._stream(body, text, stop)
                payload = {
                    "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": stop, "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, body, text, stop):
                events = [
                    ("message_start", {"type": "message_start", "message": {
                        "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": text}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                       "usage": {"output_tokens": 10}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                data = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def api():
    fake = FakeAPI()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def llm(api):
    return LLM(client=anthropic.Anthropic(api_key="test", base_url=api.url, max_retries=0))


def test_resume_parse_is_cached(api, llm, profile, fixtures, tmp_path):
    api.replies.append((profile.model_dump(mode="json"), "end_turn"))
    p1, h1 = load_or_parse(fixtures / "resume.txt", tmp_path, llm)
    p2, h2 = load_or_parse(fixtures / "resume.txt", tmp_path, llm)  # served from cache
    assert p1 == p2 == profile and h1 == h2
    assert len(api.requests) == 1
    req = api.requests[0]
    assert req["body"]["model"] == "claude-opus-5-5"
    assert req["body"]["output_config"]["effort"] == "medium"
    assert req["body"]["output_config"]["format"]["type"] == "json_schema"
    assert req["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")
    assert "Skills: Python, SQL" in req["body"]["messages"][0]["content"]


def test_score_job_clamps_and_caches_profile(api, llm, profile):
    api.replies.append(({"score": 140, "verdict": "strong", "reasons": ["Python APIs at Shopify"],
                         "gaps": [], "keywords": ["REST"]}, "end_turn"))
    job = Job(id="gh:x:1", source="greenhouse", title="Backend Intern", company="X", description="Python")
    m = score_job(job, profile, llm)
    assert m.score == 100
    body = api.requests[0]["body"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Shopify" in body["system"][0]["text"]  # profile in the cached system prompt
    assert "Backend Intern" in body["messages"][0]["content"]


def test_refusal_raises(api, llm, profile):
    api.replies.append(({}, "refusal"))
    job = Job(id="gh:x:1", source="greenhouse", title="T", company="C")
    with pytest.raises(LLMError):
        score_job(job, profile, llm)


def test_cover_letter_generated_and_rendered(api, llm, profile, tmp_path):
    api.replies.append(({"salutation": "Dear Hiring Manager,",
                         "paragraphs": ["I’m applying to the Backend Intern role — excited.", "At Shopify I built APIs."],
                         "closing": "Sincerely,"}, "end_turn"))
    job = Job(id="gh:acme:42", source="greenhouse", title="Backend Intern", company="Acme", description="Python")
    letter = generate_cover_letter(job, profile, llm, notes="Winter 2027 term")
    txt, pdf = save_cover_letter(letter, profile, job, tmp_path)
    text = txt.read_text()
    assert text.startswith("Jane Doe\n") and "Acme" in text and text.rstrip().endswith("Jane Doe")
    assert pdf.read_bytes().startswith(b"%PDF")
    prompt = api.requests[0]["body"]["messages"][0]["content"]
    assert "Winter 2027 term" in prompt and api.requests[0]["body"]["output_config"]["effort"] == "high"


def test_letter_header_date_is_windows_safe(profile):
    from datetime import date

    from coop_apply.cover_letter import letter_header

    job = Job(id="gh:a:1", source="greenhouse", title="T", company="Acme")
    assert "October 1, 2026" in letter_header(profile, job, today=date(2026, 10, 1))


def test_revise_and_review(api, llm, profile):
    from coop_apply.cover_letter import review_cover_letter, revise_cover_letter

    job = Job(id="gh:acme:42", source="greenhouse", title="Backend Intern", company="Acme")
    api.replies.append(({"salutation": "Dear Hiring Manager,", "paragraphs": ["Shorter."], "closing": "Sincerely,"}, "end_turn"))
    letter = revise_cover_letter(job, profile, "Old draft text", "Make it shorter", llm)
    assert letter.paragraphs == ["Shorter."]
    prompt = api.requests[0]["body"]["messages"][0]["content"]
    assert "<current_draft>\nOld draft text" in prompt and "Make it shorter" in prompt

    api.replies.append(({"overall": "Solid.", "suggestions": ["Fix typo 'recieve'"]}, "end_turn"))
    review = review_cover_letter(job, profile, "I recieve", llm)
    assert review.suggestions == ["Fix typo 'recieve'"]


def test_discover_keeps_only_postings_with_real_urls(api, llm, profile):
    from coop_apply.sources.discover import discover_jobs

    # 1st call: research with web search (returns notes as text); 2nd: structured extraction
    api.replies.append(("Notes: Stantec Geotechnical Co-op https://careers.stantec.com/job/123 ...", "end_turn"))
    api.replies.append(({"postings": [
        {"company": "Stantec", "title": "Geotechnical Co-op", "location": "Waterloo, ON",
         "url": "https://careers.stantec.com/job/123", "apply_url": "", "deadline": "", "summary": "Field testing."},
        {"company": "Made Up Inc", "title": "Intern", "location": "", "url": "(not found)", "apply_url": "",
         "deadline": "", "summary": ""},
    ]}, "end_turn"))
    jobs = discover_jobs(profile, llm, "Ontario", 5, log=lambda *_: None)
    assert [(j.company, j.source, j.apply_url) for j in jobs] == [
        ("Stantec", "web", "https://careers.stantec.com/job/123")]
    research = api.requests[0]["body"]
    assert research["tools"][0]["type"] == "web_search_20260209"
    assert "Ontario" in research["messages"][0]["content"] and research["stream"] is True
    assert "Stantec Geotechnical" in api.requests[1]["body"]["messages"][0]["content"]


def test_cover_letter_from_pasted_description(api, llm, profile, tmp_path, monkeypatch):
    from coop_apply import cli
    from coop_apply.config import Config
    from coop_apply.db import Store
    from coop_apply.resume import ProfileCache
    from coop_apply.utils import file_hash

    monkeypatch.chdir(tmp_path)
    cfg = Config()
    cfg.data_dir, cfg.output_dir = tmp_path / "data", tmp_path / "out"
    (tmp_path / "resume.txt").write_text("Jane Doe resume")
    cfg.profile.resume_path = str(tmp_path / "resume.txt")
    ProfileCache(cfg.data_dir).save(file_hash(tmp_path / "resume.txt"), profile)  # resume already read
    monkeypatch.setattr(cli, "get_llm", lambda c: llm)
    store = Store(":memory:")
    posting = ("Stantec is hiring a Geotechnical Co-op Student in Waterloo for Winter 2027. You will support "
               "field investigations, log boreholes and run lab tests. Requirements: civil or geological engineering.")
    api.replies += [
        ({"title": "Geotechnical Co-op Student", "company": "Stantec", "location": "Waterloo, ON", "deadline": "",
          "description": posting}, "end_turn"),
        ({"score": 78, "verdict": "good", "reasons": ["Field work"], "gaps": [], "keywords": []}, "end_turn"),
        ({"salutation": "Dear Hiring Manager,", "paragraphs": ["I would love to join Stantec."], "closing": "Sincerely,"},
         "end_turn"),
    ]
    job = cli.letter_from_description(cfg, store, posting, url="https://careers.stantec.com/job/1")
    assert job.id == cli.quick_letter_job_id(posting, "https://careers.stantec.com/job/1")
    assert (job.source, job.title, job.company, job.apply_url) == (
        "manual", "Geotechnical Co-op Student", "Stantec", "https://careers.stantec.com/job/1")
    assert store.get_match(job.id).score == 78
    from pathlib import Path

    pdf = Path(store.get_application(job.id)["cover_letter_path"])
    assert "I would love to join Stantec." in pdf.with_suffix(".txt").read_text()
    assert posting in api.requests[0]["body"]["messages"][0]["content"]  # read from the pasted text


def test_pasted_description_too_short_is_rejected(tmp_path):
    from coop_apply import cli
    from coop_apply.config import Config
    from coop_apply.db import Store

    with pytest.raises(FileNotFoundError, match="Paste the job description"):
        cli.letter_from_description(Config(), Store(":memory:"), "too short")


def test_quick_letter_links(profile, tmp_path, monkeypatch):
    from coop_apply import cli
    from coop_apply.config import Config
    from coop_apply.db import Store
    from coop_apply.models import Status

    store = Store(":memory:")
    assert cli.quick_letter_inputs("  https://x.com/jobs/1 ", "") == ("", "https://x.com/jobs/1")  # link in the big box
    # two postings pasted from one careers page stay two jobs, and keep their own status
    a = cli.quick_letter_job_id("Backend co-op posting text. " * 5, "https://acme.com/careers#backend", store)
    store.upsert_job(Job(id=a, source="manual", title="Backend", company="Acme", url="https://acme.com/careers#backend"))
    store.set_status(a, Status.submitted)
    b = cli.quick_letter_job_id("Data co-op posting text. " * 5, "https://acme.com/careers#data", store)
    b2 = cli.quick_letter_job_id("Data co-op posting text. " * 5, "https://acme.com/careers", store)
    assert len({a, b, b2}) == 3
    # a posting the app already found is reused (no second copy to apply to twice)
    store.upsert_job(Job(id="gh:acme:7", source="greenhouse", title="Data Co-op", company="Acme",
                         url="https://boards.greenhouse.io/acme/jobs/7", description="short"))
    store.set_status("gh:acme:7", Status.submitted)
    assert cli.quick_letter_job_id("", "https://boards.greenhouse.io/acme/jobs/7/", store) == "gh:acme:7"

    cfg = Config()
    letters = []
    monkeypatch.setattr(cli, "get_llm", lambda c: object())
    monkeypatch.setattr(cli, "get_profile", lambda c, llm=None, force=False: (profile, "h"))
    monkeypatch.setattr("coop_apply.matcher.score_job", lambda *a, **k: pytest.fail("it was already scored"))

    def fake_letter(cfg, store, job, profile, llm):
        letters.append(job.id)
        store.set_status(job.id, cli.letter_status(store, job.id), cover_letter_path=str(tmp_path / "l.pdf"))
        return tmp_path / "l.txt", tmp_path / "l.pdf"

    monkeypatch.setattr(cli, "make_letter", fake_letter)
    from coop_apply.models import MatchResult, Verdict

    store.save_match("gh:acme:7", "h", MatchResult(score=80, verdict=Verdict.good, reasons=[], gaps=[], keywords=[]))
    posting = "Acme is hiring a Data Co-op for Winter 2027. You will build dashboards and pipelines in Python. " * 2
    job = cli.letter_from_description(cfg, store, posting, "https://boards.greenhouse.io/acme/jobs/7")
    assert job.id == "gh:acme:7" and letters == ["gh:acme:7"]
    assert store.get_job("gh:acme:7").description == posting.strip()  # the fuller pasted text is kept
    assert store.get_application("gh:acme:7")["status"] == "submitted"  # a new letter doesn't un-apply it
