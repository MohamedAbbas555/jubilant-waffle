"""Drive the scraper and form filler against local fake pages in real Chromium."""

from coop_apply.apply.external import Applicant, fill_form
from coop_apply.config import WaterlooWorksConfig
from coop_apply.sources.waterlooworks import scrape_postings


def test_scrape_fake_waterlooworks(browser, fixtures):
    ctx = browser.new_context()
    page = ctx.new_page()
    cfg = WaterlooWorksConfig(postings_url=(fixtures / "fake_ww.html").as_uri(), max_pages=3)
    jobs = list(scrape_postings(ctx, page, cfg, known_ids={"ww:999999"}, log=lambda *_: None))
    assert [j.id for j in jobs] == ["ww:123456", "ww:234567", "ww:345678"]
    assert jobs[0].title == "Software Developer Intern" and jobs[0].location == "Waterloo"
    assert jobs[1].apply_url == "https://careers.example.com/apply/1"
    assert jobs[2].company == "Lakeside Devices"

    # already-known postings are skipped without opening them
    again = list(scrape_postings(ctx, page, cfg, known_ids={"ww:123456", "ww:234567"}, log=lambda *_: None))
    assert [j.id for j in again] == ["ww:345678"]
    ctx.close()


def test_fill_fake_application_form(browser, fixtures, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    cl = tmp_path / "cl.pdf"
    cl.write_bytes(b"%PDF-1.4 cl")
    page = browser.new_page()
    page.goto((fixtures / "fake_form.html").as_uri())
    applicant = Applicant("Jane", "Doe", "jane@uwaterloo.ca", "519-555-0100",
                          linkedin="https://linkedin.com/in/janedoe", github="https://github.com/janedoe")
    fill_form(page, applicant, resume, cl, "Dear Hiring Manager, ...", log=lambda *_: None)
    v = lambda css: page.locator(css).input_value()
    assert v("#fn") == "Jane" and v("#ln") == "Doe" and v("#em") == "jane@uwaterloo.ca"
    assert v("#ph") == "519-555-0100"
    assert v("#li").startswith("https://linkedin") and v("#gh").startswith("https://github")
    assert v("#cl_text").startswith("Dear Hiring Manager")
    assert v("#why") == ""  # custom questions are left for the human
    files = page.evaluate("() => [...document.querySelectorAll('input[type=file]')].map(i => i.files[0] && i.files[0].name)")
    assert files == ["resume.pdf", "cl.pdf"]
    page.close()


def test_scrape_asks_user_when_landing_on_dashboard(browser, fixtures, tmp_path):
    ctx = browser.new_context()
    page = ctx.new_page()
    cfg = WaterlooWorksConfig(postings_url=(fixtures / "fake_ww_dashboard.html").as_uri(), max_pages=1)
    asked = []

    def ask(question):
        asked.append(question)
        page.click("#mine")  # what the user does in the browser window
        return True

    jobs = list(scrape_postings(ctx, page, cfg, log=lambda *_: None, ask=ask, debug_dir=tmp_path))
    assert len(asked) == 1 and "For My Program" in asked[0]
    assert [j.id for j in jobs] == ["ww:123456", "ww:234567"]
    assert (tmp_path / "postings-page.png").exists() and (tmp_path / "postings-page.html").exists()

    # answering No stops cleanly
    assert list(scrape_postings(ctx, page, cfg, log=lambda *_: None, ask=lambda q: False)) == []
    ctx.close()


def test_apply_finds_posting_by_title_and_company(browser, fixtures, tmp_path):
    from coop_apply.apply.waterlooworks import _open_posting_if_listed
    from coop_apply.models import Job

    ctx = browser.new_context()
    page = ctx.new_page()
    cfg = WaterlooWorksConfig()
    # an ID we made up (no real WaterlooWorks number was found when scraping)
    job = Job(id="ww:06a37c7a4e", source="waterlooworks", title="Engineering Co-op Student", company="Pre-Con")

    page.goto((fixtures / "fake_ww_apply.html").as_uri())
    p = _open_posting_if_listed(ctx, page, job, cfg, confirm=lambda q: False, log=lambda *_: None)
    assert p.inner_text("#t") == "b"  # the Pre-Con row, not the other company's, and not "Apply filters"

    # landing on a dashboard: ask the user to bring the list up, then carry on
    page.goto((fixtures / "fake_ww_apply_dash.html").as_uri())
    asked = []

    def confirm(q):
        asked.append(q)
        page.click("#search")
        return True

    p = _open_posting_if_listed(ctx, page, job, cfg, confirm=confirm, debug_dir=tmp_path, log=lambda *_: None)
    assert len(asked) == 1 and "Engineering Co-op Student" in asked[0]
    assert p.inner_text("#t") == "b"
    ctx.close()


def test_old_apply_selector_in_config_is_upgraded(tmp_path):
    from coop_apply.config import WaterlooWorksSelectors, load_config

    p = tmp_path / "c.toml"
    p.write_text("[waterlooworks.selectors]\napply_button = \"button:has-text('Apply'), a:has-text('Apply')\"\n")
    assert load_config(p).waterlooworks.selectors.apply_button == WaterlooWorksSelectors().apply_button


def test_apply_end_to_end_like_real_waterlooworks(browser, fixtures, tmp_path, monkeypatch):
    """List -> posting panel (icon-only Apply) -> new tab wizard: pre-screening (8 months -> No),
    custom package, résumé by name, cover letter uploaded in the package, Submit -> Confirmation."""
    import json

    from coop_apply.apply.waterlooworks import apply_to_posting
    from coop_apply.models import Job

    import coop_apply.browser

    # keep the form tab open after applying so the test can inspect what was sent
    monkeypatch.setattr(coop_apply.browser, "close_other_tabs", lambda ctx, keep: None)
    ctx = browser.new_context()
    page = ctx.new_page()
    cfg = WaterlooWorksConfig(resume_document="Mohamed Abbas Geological Engineering", has_full_g_license=True)
    job = Job(id="ww:492254", source="waterlooworks", title="Mapper", company="MappedIn Inc",
              url=(fixtures / "fake_ww_real.html").as_uri())
    letter = tmp_path / "mappedin-mapper.pdf"
    letter.write_bytes(b"%PDF-1.4 letter")
    questions = []
    ok = apply_to_posting(ctx, page, job, cfg, letter, confirm=lambda q: questions.append(q) or True,
                          log=lambda *_: None)
    assert ok is True
    wizard = ctx.pages[-1]
    sent = json.loads(wizard.evaluate("document.body.dataset.sent"))
    assert sent == {"q8": "No", "qg": "Yes, I have a full G licence", "resume": "geo",
                    "cl": "mappedin-mapper.pdf", "custom": True}
    assert wizard.evaluate("document.body.dataset.docname").startswith("CL - MappedIn Inc - Mapper")
    assert len(questions) == 1 and "Submit it now?" in questions[0]  # only the final check-in
    ctx.close()


def test_apply_falls_back_to_asking_when_no_button(browser, fixtures):
    import pytest

    from coop_apply.apply.waterlooworks import ApplyError, apply_to_posting
    from coop_apply.models import Job

    ctx = browser.new_context()
    page = ctx.new_page()
    job = Job(id="ww:1", source="waterlooworks", title="Some Posting", company="X",
              url=(fixtures / "fake_ww_dashboard.html").as_uri())
    asked = []
    with pytest.raises(ApplyError):
        apply_to_posting(ctx, page, job, WaterlooWorksConfig(), None,
                         confirm=lambda q: asked.append(q) or False, log=lambda *_: None)
    assert any("open that posting" in q for q in asked)
    ctx.close()


def test_unknown_prescreening_question_is_handed_to_the_user(browser):
    import pytest

    from coop_apply.apply.waterlooworks import ApplyError, get_through_prescreening

    ctx = browser.new_context()
    page = ctx.new_page()
    html = """<label for=c>Which campus would you prefer?</label>
      <select id=c><option>Select</option><option>Downtown</option></select>
      <button onclick="if (c.value !== 'Select') document.body.innerHTML = 'Application Options - Create Custom Application Package'">Next</button>"""
    page.set_content(html)
    asked = []

    def user_answers(q):
        asked.append(q)
        page.select_option("#c", "Downtown")
        page.click("text=Next")
        return True

    get_through_prescreening(ctx, page, WaterlooWorksConfig(), user_answers, log=lambda *_: None)
    assert len(asked) == 1 and "Which campus" in asked[0]

    page.set_content(html)
    with pytest.raises(ApplyError):  # user says No -> stop cleanly, nothing submitted
        get_through_prescreening(ctx, page, WaterlooWorksConfig(), lambda q: False, log=lambda *_: None)
    ctx.close()


def test_leftover_form_tabs_are_closed_and_not_reused(browser, fixtures, tmp_path):
    """An unfinished form for another job left open must not be picked up, and the form
    tab this application opens is closed afterwards."""
    from coop_apply.apply.waterlooworks import apply_to_posting
    from coop_apply.models import Job

    ctx = browser.new_context()
    page = ctx.new_page()
    stale = ctx.new_page()  # e.g. "Technology Support Student" form from an earlier attempt
    stale.set_content("<h1>Submit Application</h1><p>Technology Support Student (ID: 492242)</p>"
                      "<select><option>Select</option><option>No</option></select><button>Next</button>")
    cfg = WaterlooWorksConfig(resume_document="Mohamed Abbas Geological Engineering")
    job = Job(id="ww:492254", source="waterlooworks", title="Mapper", company="MappedIn Inc",
              url=(fixtures / "fake_ww_real.html").as_uri())
    letter = tmp_path / "cl.pdf"
    letter.write_bytes(b"%PDF-1.4")
    sent = {}

    def confirm(q):
        if "Submit it now?" in q:  # peek at the form being submitted
            form = ctx.pages[-1]
            sent["title"] = form.inner_text("body")
        return True

    assert apply_to_posting(ctx, page, job, cfg, letter, confirm, log=lambda *_: None) is True
    assert "Mapper (ID: 492254)" in sent["title"] and "Technology Support" not in sent["title"]
    assert ctx.pages == [page]  # stale tab and the new form tab are both gone
    ctx.close()


def test_form_for_a_different_posting_is_rejected(browser):
    import pytest

    from coop_apply.apply.waterlooworks import ApplyError, _apply_steps
    from coop_apply.models import Job

    ctx = browser.new_context()
    page = ctx.new_page()
    html = ("<div class=modal><h2>Mapper</h2><button onclick=\"window.open('about:blank').document.write("
            "'<h1>Submit Application</h1><p>Technology Support Student (ID: 492242)</p>')\">"
            "<i>playlist_add</i></button></div>")
    page.set_content(html)
    job = Job(id="ww:492254", source="waterlooworks", title="Mapper", company="MappedIn", url="about:blank")
    page.goto = lambda *a, **k: None  # stay on the content set above
    with pytest.raises(ApplyError, match="different posting"):
        _apply_steps(ctx, page, job, WaterlooWorksConfig(), None, lambda q: True, False, lambda *_: None, None)
    ctx.close()
