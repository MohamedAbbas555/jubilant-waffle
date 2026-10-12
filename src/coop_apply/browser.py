"""Persistent Playwright browser session.

WaterlooWorks sits behind UW single sign-on with Duo 2FA. We never handle your
password: you log in yourself once in the opened browser window, and the session
cookies are kept in ``.coop_apply/browser-profile`` for later runs.

From the command line each command opens the browser and closes it when done. The app
(`coop-apply gui`) calls ``keep_browser_open()`` instead: one browser window stays open
between tasks, so an unfinished application form never disappears on you. Playwright
objects belong to the thread that created them, so the app runs all its tasks on one
worker thread.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

_keep_open = {"on": False}
_local = threading.local()


def keep_browser_open(on: bool = True) -> None:
    _keep_open["on"] = on


def _launch(pw, profile_dir: Path, headless: bool):
    kwargs = {}
    if exe := os.environ.get("COOP_APPLY_CHROMIUM"):
        kwargs["executable_path"] = exe
    ctx = pw.chromium.launch_persistent_context(
        str(profile_dir),
        headless=headless,
        viewport={"width": 1400, "height": 900},
        accept_downloads=True,
        **kwargs,
    )
    ctx.set_default_timeout(20_000)
    return ctx


def _shared_context(profile_dir: Path, headless: bool):
    """The long-lived browser for this thread; relaunched if you closed its window."""
    from playwright.sync_api import sync_playwright

    state = getattr(_local, "state", None)
    if state and not state["closed"]:
        return state["ctx"]
    pw = state["pw"] if state else sync_playwright().start()
    ctx = _launch(pw, profile_dir, headless)
    state = {"pw": pw, "ctx": ctx, "closed": False}
    ctx.on("close", lambda *_: state.update(closed=True))
    _local.state = state
    return ctx


@contextmanager
def browser_context(profile_dir: Path, headless: bool = False):
    profile_dir.mkdir(parents=True, exist_ok=True)
    if _keep_open["on"]:
        yield _shared_context(profile_dir, headless)
        return
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _launch(p, profile_dir, headless)
        try:
            yield ctx
        finally:
            ctx.close()


def first_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()
