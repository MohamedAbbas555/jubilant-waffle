import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures():
    return FIXTURES


@pytest.fixture
def profile():
    from coop_apply.models import Experience, Project, ResumeProfile

    return ResumeProfile(
        name="Jane Doe",
        email="jane@uwaterloo.ca",
        phone="519-555-0100",
        links=["https://github.com/janedoe", "https://linkedin.com/in/janedoe"],
        program="BASc Computer Engineering, University of Waterloo",
        academic_level="2B",
        summary="Computer engineering student with backend experience.",
        skills=["Python", "SQL", "Docker", "Go"],
        experiences=[Experience(title="Backend Developer Intern", organization="Shopify", dates="2026",
                                highlights=["Built Python REST APIs serving 1M requests/day"])],
        projects=[Project(name="RoboNav", description="ROS path planner", technologies=["C++", "ROS"])],
        target_roles=["backend software developer", "data engineer"],
    )


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch(**_launch_kwargs())
            b.close()
        return True
    except Exception:
        return False


def _launch_kwargs():
    exe = os.environ.get("COOP_APPLY_CHROMIUM")
    return {"executable_path": exe} if exe else {}


@pytest.fixture(scope="session")
def browser():
    if not _chromium_available():
        pytest.skip("Chromium for Playwright not available")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(**_launch_kwargs())
        yield b
        b.close()
