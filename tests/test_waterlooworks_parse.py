from coop_apply.apply.waterlooworks import document_choices, needs_employer_website, required_documents
from coop_apply.config import WaterlooWorksConfig
from coop_apply.sources.waterlooworks import job_from_posting, parse_label_values, posting_id_from


def test_parse_label_values_next_line_and_inline(fixtures):
    text = (fixtures / "ww_posting.txt").read_text()
    fields = parse_label_values(text)
    assert fields["Job Title"] == "Software Developer Intern - Platform"
    assert fields["Organization"] == "Maple Robotics Inc."
    assert "Build REST APIs" in fields["Job Responsibilities"]
    assert fields["Application Deadline"] == "Oct 20, 2026 09:00 AM"
    inline = parse_label_values("Job Title: Data Analyst\nOrganization: Bank")
    assert inline == {"Job Title": "Data Analyst", "Organization": "Bank"}


def test_job_from_posting(fixtures):
    text = (fixtures / "ww_posting.txt").read_text()
    job = job_from_posting(text, ["123456", "x", "y"], url="https://waterlooworks.uwaterloo.ca/p")
    assert job.id == "ww:123456"
    assert job.title == "Software Developer Intern - Platform"
    assert job.company == "Maple Robotics Inc."
    assert job.location == "Waterloo, Ontario, Canada"
    assert "Required Skills:" in job.description and "Docker" in job.description
    assert job.extra["Work Term"] == "2027 - Winter"
    assert not needs_employer_website(job)
    assert required_documents(job) == ["Résumé", "Grade Reports", "Cover Letter"]


def test_posting_id_sources():
    assert posting_id_from(["", "Title"], href="/jobs.htm?postingId=987654") == "987654"
    assert posting_id_from(["123456", "Title"]) == "123456"
    assert posting_id_from(["Title only"]) is None


def test_employer_website_detection():
    job = job_from_posting(
        "Job Title: X\nOrganization: Y\nApplication Method: Through Employer Website\n"
        "If by Website, go to: https://careers.example.com/1"
    )
    assert needs_employer_website(job)
    assert job.apply_url == "https://careers.example.com/1"


def test_document_choices(fixtures):
    job = job_from_posting((fixtures / "ww_posting.txt").read_text())
    cfg = WaterlooWorksConfig(resume_document="Resume F26", transcript_document="Grades", work_history_document="WH")
    choices = document_choices(job, cfg, "CL - Maple")
    assert list(choices.values()) == ["Resume F26", "CL - Maple", "Grades"]  # no work history required
