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
