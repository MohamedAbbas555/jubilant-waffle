import json

from coop_apply.config import ExternalConfig
from coop_apply.sources.external import fetch_external


def test_fetch_external_parses_and_filters(fixtures):
    payloads = {
        "greenhouse": json.loads((fixtures / "greenhouse.json").read_text()),
        "lever": json.loads((fixtures / "lever.json").read_text()),
        "ashbyhq": json.loads((fixtures / "ashby.json").read_text()),
    }

    def fake_fetch(url):
        return next(v for k, v in payloads.items() if k in url)

    cfg = ExternalConfig(greenhouse=["stripe"], lever=["acme"], ashby=["cohere"])
    jobs = list(fetch_external(cfg, fetch=fake_fetch, log=lambda *_: None))
    ids = [j.id for j in jobs]
    assert ids == ["gh:stripe:4012345", "lever:acme:abc-123", "ashby:cohere:u-1"]

    gh = jobs[0]
    assert gh.company == "Stripe" and gh.location == "Toronto, ON"
    assert "payments" in gh.description and "- Python" in gh.description and "<" not in gh.description

    lever = jobs[1]
    assert lever.apply_url.endswith("/apply")
    assert "Requirements" in lever.description and "pandas" in lever.description
    assert lever.extra["team"] == "Analytics"


def test_fetch_external_survives_errors():
    import urllib.error

    def boom(url):
        raise urllib.error.URLError("down")

    logs = []
    assert list(fetch_external(ExternalConfig(greenhouse=["x"]), fetch=boom, log=logs.append)) == []
    assert "down" in logs[0]
