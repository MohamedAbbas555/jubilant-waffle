"""Persistent Playwright browser session.

WaterlooWorks sits behind UW single sign-on with Duo 2FA. We never handle your
password: you log in yourself once in the opened browser window, and the session
cookies are kept in ``.coop_apply/browser-profile`` for later runs.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def browser_context(profile_dir: Path, headless: bool = False):
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if exe := os.environ.get("COOP_APPLY_CHROMIUM"):
        kwargs["executable_path"] = exe
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1400, "height": 900},
            accept_downloads=True,
            **kwargs,
        )
        ctx.set_default_timeout(20_000)
        try:
            yield ctx
        finally:
            ctx.close()


def first_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()
