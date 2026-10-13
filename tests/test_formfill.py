"""The company-website application filler, against mock career pages in real Chromium.

A rule-based stand-in plays Claude's part (planning each page from the snapshot), so these
tests check what we control: reading the page (labels, options, hidden uploads, required),
filling every kind of field, clicking through steps, and stopping for sign-ins.
"""

import json

import pytest

from coop_apply.apply import ApplyError, NeedsYou
from coop_apply.apply.formfill import FieldFill, StepPlan, apply_fill, best_option, fill_and_submit, snapshot
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


# ---- safety: one submit, real confirmations only ------------------------------------


def phone_claude(first_phone="519 555 0100"):
    """Fills name + phone (a bad phone the first time if asked), then submits."""
    calls = {"n": 0}

    def claude(snap, job, text):
        calls["n"] += 1
        if "thank you for applying" in snap.text.lower():
            return plan("confirmation", action="done")
        phone = first_phone if calls["n"] == 1 else "519 555 0100"
        answers = {"First name *": "Jane", "Phone *": phone, "How did you hear about us?": "University"}
        fills = [FieldFill(field_id=f["id"], value=answers[f["label"]]) for f in snap.fields
                 if f["label"] in answers and f.get("value") != answers[f["label"]]]
        return plan(fills=fills, action="submit", button="Submit")

    return claude


def test_slow_submit_is_pressed_once(browser, fixtures, files):
    ctx = browser.new_context()
    page = ctx.new_page()
    job = job_at(fixtures, "fake_company_slow.html")
    job.apply_url += "?delay=8000"  # the site takes 8 seconds to answer
    ok = fill_and_submit(ctx, page, job, phone_claude(), files, "", confirm=lambda q: True, auto=True,
                         log=lambda *_: None)
    assert ok is True
    assert page.evaluate("window.SENT") == 1  # never pressed twice while it was sending
    assert not page.evaluate("window.SEARCHED || 0")  # the header search 'Submit' wasn't clicked
    ctx.close()


def test_rejected_submit_is_fixed_and_not_reported_early(browser, fixtures, files):
    # the page says "Thank you for your interest..." all along: that is not a confirmation
    ctx = browser.new_context()
    page = ctx.new_page()
    claude = phone_claude(first_phone="abc")
    ok = fill_and_submit(ctx, page, job_at(fixtures, "fake_company_slow.html"), claude, files, "",
                         confirm=lambda q: True, auto=True, log=lambda *_: None)
    assert ok is True and page.evaluate("window.SENT") == 1
    ctx.close()


def test_unchanged_page_after_submit_is_never_pressed_again(browser, fixtures, files, monkeypatch):
    import coop_apply.apply.formfill as ff

    monkeypatch.setattr(ff, "SUBMIT_WAIT_S", 6)  # (45 s / 60 s on real sites)
    monkeypatch.setattr(ff, "CONFIRM_WAIT_S", 10)
    ctx = browser.new_context()
    page = ctx.new_page()
    job = job_at(fixtures, "fake_company_slow.html")
    job.apply_url += "?delay=600000"  # sent, but the site never shows anything
    with pytest.raises(NeedsYou, match="couldn't confirm the application went through") as e:
        fill_and_submit(ctx, page, job, phone_claude(), files, "", confirm=lambda q: True, auto=True,
                        log=lambda *_: None)
    assert "before applying again" in str(e.value)
    assert page.evaluate("window.SENT") == 1
    ctx.close()


def test_done_before_any_submit_is_not_a_submission(browser, fixtures, files):
    ctx = browser.new_context()
    page = ctx.new_page()
    with pytest.raises(NeedsYou, match="already applied"):
        fill_and_submit(ctx, page, job_at(fixtures, "fake_company_job.html"),
                        lambda s, j, t: plan("confirmation", action="done"), files, "",
                        confirm=lambda q: True, auto=True, log=lambda *_: None)
    ctx.close()


def test_success_text_must_be_new():
    from coop_apply.apply.formfill import newly_confirmed

    boiler = "Thank you for your interest in a career at Stantec."
    assert not newly_confirmed(boiler + " Please enter a valid phone number", boiler)
    assert newly_confirmed("Thank you for applying! We have received your application.", boiler)
    assert not newly_confirmed("Your application has been received", "Your application has been received")
    # instructions and errors that merely mention it
    assert not newly_confirmed("Once your application is submitted you will not be able to edit it.", "")
    assert not newly_confirmed("Your application cannot be submitted until all fields are complete", "")
    assert not newly_confirmed("After your application has been received, a recruiter will contact you.", "")
    assert newly_confirmed("Thanks for applying, please check your inbox for next steps", "")


# ---- reading the page ------------------------------------------------------------------


def test_invisible_captcha_is_ignored_but_a_real_one_is_seen(browser, fixtures):
    page = browser.new_page()
    page.goto((fixtures / "fake_company_slow.html").as_uri())
    assert snapshot(page).captcha == ""  # invisible reCAPTCHA + badge + hidden challenge frame
    page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
        '<iframe width="304" height="78" src="about:blank#recaptcha/api2/anchor?k=x&size=normal"></iframe>' +
        '<textarea name="g-recaptcha-response" style="display:none"></textarea>')""")
    assert "not a robot" in snapshot(page).captcha
    page.evaluate("() => { document.querySelector('[name=g-recaptcha-response]').value = 'solved'; }")
    assert snapshot(page).captcha == ""  # you ticked it
    page.close()


LABELS_HTML = """
<form>
  <div><input type="checkbox" name="consent" required> <span>I consent to Stantec processing my data *</span></div>
  <div><input type="checkbox" name="news" value="1"> <span>Send me job alerts</span></div>
  <div role="radiogroup" aria-labelledby="q1"><span id="q1">Do you need sponsorship? *</span>
    <input type="radio" name="sp" value="1"><span>Yes</span> <input type="radio" name="sp" value="0"><span>No</span></div>
  <div class="field"><label>Resume/CV *</label>
    <div class="attach"><div>Accepted file types: pdf, doc, docx</div><input type="file" name="resume" style="display:none"></div></div>
  <div class="field"><label>Cover Letter</label>
    <div class="attach"><div>Accepted file types: pdf, doc, docx</div><input type="file" name="cover_letter" style="display:none"></div></div>
  <label for="country">Country *</label><select id="country" required>%s</select>
  <button type="button" aria-haspopup="listbox" aria-label="How did you hear about us? Select One Required">Select One</button>
</form>
"""


def test_labels_of_checkboxes_radios_uploads_and_dropdown_buttons(browser):
    countries = ["Select..."] + [f"Country {i}" for i in range(30)] + ["British Indian Ocean Territory"] + \
                [f"Place {i}" for i in range(200)] + ["India", "Germany"]
    page = browser.new_page()
    page.set_content(LABELS_HTML % "".join(f"<option>{c}</option>" for c in countries))
    snap = snapshot(page)
    labels = [f["label"] for f in snap.fields]
    assert "I consent to Stantec processing my data *" in labels and "Send me job alerts" in labels
    radio = next(f for f in snap.fields if f["type"] == "radio")
    assert radio["label"] == "Do you need sponsorship? *" and radio["options"] == ["Yes", "No"]
    uploads = [f for f in snap.fields if f["type"] == "file"]
    assert [u["label"] for u in uploads] == ["Resume/CV *", "Cover Letter"]
    assert [u["name"] for u in uploads] == ["resume", "cover_letter"]
    dropdown = next(f for f in snap.fields if f.get("pick_only"))
    assert dropdown["type"] == "combobox" and "How did you hear" in dropdown["label"]
    assert "Select One" not in snap.buttons
    # a long list: Claude sees part of it, but the answer is found in all of it, exactly
    country = next(f for f in snap.fields if f["type"] == "select")
    assert len(country["options"]) == len(countries)
    assert "only the first 300 shown" not in snap.for_prompt()  # 234 options: all shown
    assert apply_fill(snap, FieldFill(field_id=country["id"], value="India"), {}, log=lambda *_: None)
    assert page.input_value("#country") == "India"
    # required and still empty when the page was read: the browser wouldn't send the form like that
    assert "I consent to Stantec processing my data *" in snap.invalid and "Country *" not in snap.invalid
    page.close()


COMBO_HTML = """
<ul role="listbox" style="display:none"><li role="option">Canada (+1)</li></ul>
<label for="cit">Country of citizenship *</label>
<input id="cit" role="combobox" aria-autocomplete="list" aria-controls="cit-list">
<ul id="cit-list" role="listbox" hidden></ul>
<p id="picked"></p>
<script>
const all = ["British Indian Ocean Territory", "India", "Indonesia"];
const box = document.getElementById('cit'), list = document.getElementById('cit-list');
box.addEventListener('input', () => {
  const v = box.value.toLowerCase();
  list.innerHTML = all.filter(c => c.toLowerCase().includes(v)).map(c => `<li role="option">${c}</li>`).join('');
  list.hidden = false;
});
list.addEventListener('click', e => { document.getElementById('picked').textContent = e.target.textContent; list.hidden = true; });
</script>
"""


def test_combobox_picks_the_exact_visible_option(browser):
    page = browser.new_page()
    page.set_content(COMBO_HTML)
    snap = snapshot(page)
    f = next(x for x in snap.fields if x["type"] == "combobox")
    assert apply_fill(snap, FieldFill(field_id=f["id"], value="India"), {}, log=lambda *_: None)
    assert page.text_content("#picked") == "India"  # not "British Indian Ocean Territory", not the hidden list
    page.close()


def test_best_option_refuses_to_guess():
    from coop_apply.apply.formfill import best_option as bo

    assert bo(["Select...", "British Indian Ocean Territory", "India"], "India") == 2
    assert bo(["Yes, I am authorized", "Yes, but I will need sponsorship", "No"], "Yes") is None  # ambiguous
    assert bo(["Select...", "Yes, I am eligible", "No"], "yes") == 1
    assert bo(["Norway", "No"], "No") == 1


def test_click_button_needs_text(browser):
    from coop_apply.apply.formfill import click_button

    ctx = browser.new_context()
    page = ctx.new_page()
    page.set_content('<a href="#logo"><img width="40" height="20"></a><button>Apply now</button>')
    assert click_button(ctx, page, "") is None and click_button(ctx, page, "   ") is None
    ctx.close()


@pytest.mark.parametrize("options", [
    "delay=8000&newsletter=1",  # a required job-alerts email in the footer doesn't mean "blocked"
    "delay=8000&reveal=1",  # our answer reveals another field: still the same form
    "delay=20000&busy=1",  # "Uploading your documents, please wait..." in a role=alert isn't a rejection
    "delay=1000&toast=1",  # a 3-second toast, then the form is cleared
])
def test_slow_or_odd_sites_get_exactly_one_submit_and_a_confirmation(browser, fixtures, files, options):
    ctx = browser.new_context()
    page = ctx.new_page()
    job = job_at(fixtures, "fake_company_slow.html")
    job.apply_url += "?" + options
    ok = fill_and_submit(ctx, page, job, phone_claude(), files, "", confirm=lambda q: True, auto=True,
                         log=lambda *_: None)
    assert ok is True and page.evaluate("window.SENT") == 1
    if "reveal" in options:
        assert page.input_value("#hear") == "University"
    ctx.close()


ODD_LABELS_HTML = """
<p>Will you need sponsorship? *</p>
<div>Yes <input type="radio" name="sp" value="1" required> No <input type="radio" name="sp" value="0"></div>
<div><input type="checkbox" name="agree" required> <!-- -->I agree to the privacy policy *</div>
<div><span>Send me news</span> <input type="checkbox" name="news"></div>
"""


def test_text_before_radios_and_comment_nodes(browser):
    page = browser.new_page()
    page.set_content(ODD_LABELS_HTML)
    snap = snapshot(page)
    radio = next(f for f in snap.fields if f["type"] == "radio")
    assert radio["options"] == ["Yes", "No"]  # not ["No", "0"]: the text comes before each button here
    labels = [f["label"] for f in snap.fields]
    assert "I agree to the privacy policy *" in labels and "Send me news" in labels  # React's <!-- --> is skipped
    assert apply_fill(snap, FieldFill(field_id=radio["id"], value="No"), {}, log=lambda *_: None)
    assert page.evaluate("document.querySelector('input[name=sp]:checked').value") == "0"
    page.close()


OLD_REACT_SELECT_HTML = """
<label for="rs">Are you legally eligible to work in Canada? *</label>
<input id="rs" role="combobox" aria-autocomplete="list" aria-controls="rs-listbox">
<div id="rs-listbox" hidden></div>
<p id="picked"></p>
<script>
const box = document.getElementById('rs'), menu = document.getElementById('rs-listbox');
box.addEventListener('input', () => {  // react-select before 5.8: options are divs without role=option
  menu.innerHTML = ['Yes', 'No'].filter(o => o.toLowerCase().startsWith(box.value.toLowerCase()))
    .map((o, i) => `<div id="react-select-3-option-${i}">${o}</div>`).join('');
  menu.hidden = false;
});
menu.addEventListener('click', e => { document.getElementById('picked').textContent = e.target.textContent; menu.hidden = true; });
</script>
"""


def test_combobox_without_role_option(browser):
    page = browser.new_page()
    page.set_content(OLD_REACT_SELECT_HTML)
    snap = snapshot(page)
    f = next(x for x in snap.fields if x["type"] == "combobox")
    assert apply_fill(snap, FieldFill(field_id=f["id"], value="Yes"), {}, log=lambda *_: None)
    assert page.text_content("#picked") == "Yes"
    page.close()


TWO_FORMS_HTML = """
<aside><form onsubmit="event.preventDefault(); window.ALERTS = 1;">
  <label>Get job alerts <input type="email" name="alerts"></label><button>Submit</button></form></aside>
<div id="app">
  <label for="fn">First name *</label><input id="fn">
  <label for="ln">Last name *</label><input id="ln">
  <label for="em">Email *</label><input id="em">
  <button type="button" onclick="window.APPLIED = 1">Submit</button>
</div>
"""


def test_submit_goes_to_the_form_we_filled_not_a_job_alerts_box(browser):
    from coop_apply.apply.formfill import click_button

    ctx = browser.new_context()
    page = ctx.new_page()
    page.set_content(TWO_FORMS_HTML)
    for sel, v in (("#fn", "Jane"), ("#ln", "Doe"), ("#em", "jane@uwaterloo.ca"),
                   ("input[name=alerts]", "jane@uwaterloo.ca")):  # the rule-based prefill fills the alerts box too
        page.fill(sel, v)
    snapshot(page)
    assert click_button(ctx, page, "Submit") is not None
    assert page.evaluate("[window.APPLIED || 0, window.ALERTS || 0]") == [1, 0]
    ctx.close()
