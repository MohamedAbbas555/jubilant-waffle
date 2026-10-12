"""The company-website application filler, against mock career pages in real Chromium.

A rule-based stand-in plays Claude's part (planning each page from the snapshot), so these
tests check what we control: reading the page (labels, options, hidden uploads, required),
filling every kind of field, clicking through steps, and stopping for sign-ins.
"""

import json

import pytest

from coop_apply.apply import ApplyError, NeedsYou
from coop_apply.apply.formfill import FieldFill, StepPlan, best_option, fill_and_submit, snapshot
from coop_apply.models import Job


def plan(kind="form", fills=(), action="click", button="", missing=(), note="test"):
    return StepPlan(page_kind=kind, fills=list(fills), missing_info=list(missing), next_action=action,
                    button_text=button, note=note)


def fake_claude(snap, job, letter_text):
    """Answers like the real prompt asks: facts only, decline EEO, tick required consent only."""
    if snap.password:
        return plan("sign_in", action="needs_user")
    if "thank you for applying" in snap.text.lower():
        return plan("confirmation", action="done")
    if not snap.fields and "Apply now" in snap.buttons:
        return plan("job_page", button="Apply now")
    answers = []
    for f in snap.fields:
        label = f["label"].lower()
        value = None
        if "first name" in label:
            value = "Jane"
        elif "last name" in label:
            value = "Doe"
        elif "email" in label:
            value = "jane@uwaterloo.ca"
        elif "resume" in label and f["type"] == "file":
            value = "resume"
        elif "cover letter" in label and f["type"] == "file":
            value = "cover_letter"
        elif "eligible to work in canada" in label:
            value = "Yes"
        elif "sponsorship" in label:
            value = "No"
        elif "why do you want" in label:
            value = "I want to grow my field skills."
        elif "gender" in label:
            value = "Prefer not to say"
        elif "certify" in label:
            value = "true"
        if value is not None and f.get("value") != value and not (f["type"] == "file" and f.get("value")):
            answers.append(FieldFill(field_id=f["id"], value=value))
    if "Submit application" in snap.buttons:
        return plan(fills=answers, action="submit", button="Submit application")
    return plan(fills=answers, button="Next")


@pytest.fixture
def files(tmp_path):
    resume = tmp_path / "Jane_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    letter = tmp_path / "stantec-cover-letter.pdf"
    letter.write_bytes(b"%PDF-1.4 letter")
    return {"resume": resume, "cover_letter": letter}


def job_at(fixtures, page_name):
    url = (fixtures / page_name).as_uri()
    return Job(id="web:abc", source="web", title="Geotechnical Co-op Student", company="Stantec", url=url, apply_url=url)


def test_snapshot_reads_questions_options_and_hidden_uploads(browser, fixtures):
    page = browser.new_page()
    page.goto((fixtures / "fake_company_form.html").as_uri())
    page.evaluate("() => { document.getElementById('s2').hidden = false; }")
    snap = snapshot(page)
    by_label = {f["label"]: f for f in snap.fields}
    assert by_label["First name *"]["required"] is True
    resume = next(f for f in snap.fields if f["type"] == "file" and "Resume" in f["label"])
    assert resume["required"] is True  # hidden behind an "Attach" button, still found
    elig = next(f for f in snap.fields if "eligible" in f["label"])
    assert elig["type"] == "select" and elig["options"] == ["Select...", "Yes", "No"]
    spons = next(f for f in snap.fields if f["type"] == "radio")
    assert "sponsorship" in spons["label"] and spons["options"] == ["Yes", "No"]
    consent = next(f for f in snap.fields if f["type"] == "checkbox" and "certify" in f["label"])
    assert consent["value"] == "false"
    assert "Submit application" in snap.buttons and "Next" in snap.buttons
    page.close()


def test_fills_a_multi_step_application_and_submits(browser, fixtures, files):
    ctx = browser.new_context()
    page = ctx.new_page()
    reported = {}
    ctx.expose_function("reportSent", lambda sent: reported.update(json.loads(sent)))
    asked = []
    ok = fill_and_submit(ctx, page, job_at(fixtures, "fake_company_job.html"), fake_claude, files,
                         "Dear Hiring Manager...", confirm=lambda q: asked.append(q) or True, log=lambda *_: None)
    assert ok is True
    assert reported == {
        "first": "Jane", "last": "Doe", "email": "jane@uwaterloo.ca", "phone": "",
        "resume": "Jane_Resume.pdf", "cover": "stantec-cover-letter.pdf",
        "elig": "Yes", "spons": "n", "why": "I want to grow my field skills.", "gender": "Prefer not to say",
        "cert": True, "news": False,
    }
    assert len(asked) == 1 and "Submit it now?" in asked[0]  # you approve the final submit
    ctx.close()


def test_auto_mode_submits_without_asking(browser, fixtures, files):
    ctx = browser.new_context()
    page = ctx.new_page()
    asked = []
    ok = fill_and_submit(ctx, page, job_at(fixtures, "fake_company_form.html"), fake_claude, files, "",
                         confirm=lambda q: asked.append(q) or True, auto=True, log=lambda *_: None)
    assert ok is True and asked == []
    ctx.close()


def test_saying_no_leaves_the_filled_form_unsubmitted(browser, fixtures, files):
    ctx = browser.new_context()
    page = ctx.new_page()
    ok = fill_and_submit(ctx, page, job_at(fixtures, "fake_company_form.html"), fake_claude, files, "",
                         confirm=lambda q: "Submit it now?" not in q, log=lambda *_: None)
    assert ok is False
    assert page.input_value("#elig") == "Yes" and not page.is_visible("#s3")  # filled, not sent
    ctx.close()


def test_sign_in_wall_needs_you(browser, fixtures, files):
    ctx = browser.new_context()
    page = ctx.new_page()
    job = job_at(fixtures, "fake_company_login.html")
    with pytest.raises(NeedsYou, match="sign in"):
        fill_and_submit(ctx, page, job, fake_claude, files, "", confirm=lambda q: True, auto=True, log=lambda *_: None)
    with pytest.raises(ApplyError):  # interactive: you're asked; saying No stops cleanly
        fill_and_submit(ctx, page, job, fake_claude, files, "", confirm=lambda q: False, log=lambda *_: None)
    ctx.close()


def test_missing_required_answer_is_never_submitted_in_auto(browser, fixtures, files):
    def claude_without_facts(snap, job, text):
        p = fake_claude(snap, job, text)
        if p.next_action == "submit":  # e.g. no facts about sponsorship
            return plan(fills=p.fills, action="submit", button=p.button_text,
                        missing=["Will you now or in the future require visa sponsorship?"])
        return p

    ctx = browser.new_context()
    page = ctx.new_page()
    with pytest.raises(NeedsYou, match="sponsorship"):
        fill_and_submit(ctx, page, job_at(fixtures, "fake_company_form.html"), claude_without_facts, files, "",
                        confirm=lambda q: True, auto=True, log=lambda *_: None)
    assert not page.is_visible("#s3")
    ctx.close()


def test_stuck_page_needs_you(browser, fixtures, files):
    def claude_that_skips_required(snap, job, text):
        return plan(button="Next")  # never fills anything -> "Please fill in all required fields"

    ctx = browser.new_context()
    page = ctx.new_page()
    with pytest.raises(NeedsYou, match="isn't moving on"):
        fill_and_submit(ctx, page, job_at(fixtures, "fake_company_form.html"), claude_that_skips_required, files,
                        "", confirm=lambda q: True, auto=True, log=lambda *_: None)
    ctx.close()


def test_best_option():
    opts = ["Select...", "Yes, I am eligible", "No", "Prefer not to say"]
    assert best_option(opts, "No") == 2
    assert best_option(opts, "yes") == 1
    assert best_option(opts, "Prefer not to say") == 3
    assert best_option(opts, "") is None
    assert best_option(opts, "Maybe") is None
