"""Applying to jobs: WaterlooWorks (``waterlooworks``) and company websites (``formfill``)."""


class ApplyError(RuntimeError):
    """The application couldn't be completed; nothing (more) was submitted."""


class NeedsYou(ApplyError):
    """Only you can do the next step (sign in / create an account, a CAPTCHA, a question
    we have no facts for...). Auto-apply marks the job 'needs you' and moves on."""


class StopRun(RuntimeError):
    """Something that will fail the same way for every job (e.g. logged out of WaterlooWorks):
    stop instead of marking job after job. Job statuses are left as they were."""
