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
