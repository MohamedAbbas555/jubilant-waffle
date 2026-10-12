from coop_apply.config import SearchConfig
from coop_apply.db import Store
from coop_apply.matcher import keyword_score, prefilter, profile_terms
from coop_apply.models import Job, MatchResult, Status, Verdict


def job(i, title, desc, loc="Waterloo"):
    return Job(id=f"gh:t:{i}", source="greenhouse", title=title, company="Co", location=loc, description=desc)


def test_prefilter_ranks_by_overlap_and_applies_filters(profile):
    jobs = [
        job(1, "Marketing Intern", "Social media campaigns"),
        job(2, "Backend Intern", "Python, SQL and Docker microservices in Go"),
        job(3, "Senior Backend Engineer", "Python SQL Docker Go"),
        job(4, "Data Intern", "SQL dashboards", loc="London, UK"),
    ]
    cfg = SearchConfig(exclude_keywords=["senior"], locations=["Waterloo", "Toronto"])
    ranked = [j.id for j, _ in prefilter(jobs, profile, cfg)]
    assert ranked[0] == "gh:t:2"
    assert "gh:t:3" not in ranked and "gh:t:4" not in ranked


def test_keyword_score_handles_symbols(profile):
    terms = profile_terms(profile)
    assert "c++" in terms
    assert keyword_score(job(1, "Robotics", "Modern C++ and ROS"), terms) >= 2
    assert keyword_score(job(2, "Robotics", "C and assembly"), terms) == 0


def test_store_roundtrip():
    s = Store(":memory:")
    j = job(1, "Backend Intern", "Python")
    assert s.upsert_job(j) is True
    assert s.upsert_job(j) is False
    m = MatchResult(score=88, verdict=Verdict.strong, reasons=["Python"], gaps=[], keywords=["APIs"])
    s.save_match(j.id, "hash1", m)
    assert s.get_match(j.id, "hash1").score == 88
    assert s.get_match(j.id, "other-resume") is None
    s.set_status(j.id, Status.prepared, cover_letter_path="/tmp/cl.pdf")
    s.set_status(j.id, Status.submitted)
    app = s.get_application(j.id)
    assert app["status"] == "submitted" and app["cover_letter_path"] == "/tmp/cl.pdf"
    assert [(r[0].id, r[2]) for r in s.ranked(80)] == [(j.id, "submitted")]
    assert s.ranked(90) == []


def test_rename_job_keeps_score_and_status():
    s = Store(":memory:")
    old = Job(id="ww:06a37c7a4e", source="waterlooworks", title="Mapper", company="MappedIn")
    s.upsert_job(old)
    s.save_match(old.id, "h", MatchResult(score=80, verdict=Verdict.good, reasons=[], gaps=[], keywords=[]))
    s.set_status(old.id, Status.prepared, cover_letter_path="cl.pdf")
    assert s.find_unnumbered_ww_job("Mapper", "MappedIn").id == old.id
    s.rename_job(old.id, "ww:492254")
    assert s.get_job(old.id) is None and s.get_job("ww:492254").id == "ww:492254"
    assert s.get_match("ww:492254").score == 80
    assert s.get_application("ww:492254")["cover_letter_path"] == "cl.pdf"
    assert s.find_unnumbered_ww_job("Mapper", "MappedIn") is None


def test_next_best_job_order_and_filters():
    from coop_apply.cli import next_best_job
    from coop_apply.config import Config

    s = Store(":memory:")
    cfg = Config()
    cfg.search.min_score = 60
    rows = [  # id, source, score, status, extra
        ("ww:1", "waterlooworks", 90, Status.submitted, {}),
        ("gh:a:2", "greenhouse", 85, Status.shortlisted, {}),
        ("ww:3", "waterlooworks", 80, Status.shortlisted, {"Application Method": "Through Employer Website",
                                                            "_apply_url": "https://employer.example/apply"}),
        ("ww:4", "waterlooworks", 75, Status.prepared, {}),
        ("ww:5", "waterlooworks", 50, Status.shortlisted, {}),
    ]
    for jid, src, score, status, extra in rows:
        link = extra.pop("_apply_url", "https://jobs.example/apply" if src != "waterlooworks" else "")
        s.upsert_job(Job(id=jid, source=src, title=jid, company="C", extra=extra, apply_url=link, url=link))
        s.save_match(jid, "h", MatchResult(score=score, verdict=Verdict.good, reasons=[], gaps=[], keywords=[]))
        s.set_status(jid, status)
    assert next_best_job(cfg, s)[0].id == "gh:a:2"  # best one not applied to yet
    assert next_best_job(cfg, s, exclude={"gh:a:2"})[0].id == "ww:3"
    assert next_best_job(cfg, s, waterlooworks_only=True)[0].id == "ww:4"  # auto-apply: WW-only, in-site
    assert next_best_job(cfg, s, exclude={"gh:a:2", "ww:3", "ww:4"}) == (None, None)  # 50 is below cutoff
