"""Configuration loaded from ``config.toml`` (see ``config.example.toml``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProfileConfig:
    resume_path: str = "resume.pdf"
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    school: str = "University of Waterloo"
    cover_letter_notes: str = ""


@dataclass
class SearchConfig:
    min_score: int = 70
    max_llm_scores: int = 60
    prefilter_keep: int = 150
    exclude_keywords: list[str] = field(default_factory=lambda: ["senior", "staff", "principal", "manager"])
    locations: list[str] = field(default_factory=list)


@dataclass
class ClaudeConfig:
    model: str = "claude-opus-5-5"
    effort_parse: str = "medium"
    effort_rank: str = "low"
    effort_letter: str = "high"
    workers: int = 4


@dataclass
class WaterlooWorksSelectors:
    # Best-effort defaults. WaterlooWorks changes its markup; run `coop-apply ww-debug`
    # on the page in question and adjust these in config.toml if scraping breaks.
    row: str = "table tbody tr"
    link: str = "td a"
    detail: str = "[role='dialog'], .modal__content, #postingDiv, main"
    close_detail: str = "[role='dialog'] button[aria-label*='lose'], .modal__close"
    next_page: str = "a[aria-label*='Next' i]:not(.disabled), button[aria-label*='Next' i]:not([disabled])"
    apply_button: str = "button:has-text('Apply'), a:has-text('Apply')"
    submit_button: str = "button:has-text('Submit'), input[type=submit][value*='Submit' i]"
    upload_button: str = "button:has-text('Upload'), a:has-text('Upload')"


@dataclass
class WaterlooWorksConfig:
    base_url: str = "https://waterlooworks.uwaterloo.ca"
    postings_url: str = "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm"
    documents_url: str = "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/documents.htm"
    max_pages: int = 10
    resume_document: str = ""
    transcript_document: str = ""
    work_history_document: str = ""
    headless: bool = False
    selectors: WaterlooWorksSelectors = field(default_factory=WaterlooWorksSelectors)


@dataclass
class ExternalConfig:
    title_keywords: list[str] = field(default_factory=lambda: ["intern", "co-op", "coop", "student", "new grad"])
    greenhouse: list[str] = field(default_factory=list)
    lever: list[str] = field(default_factory=list)
    ashby: list[str] = field(default_factory=list)


@dataclass
class ApplyConfig:
    max_per_run: int = 10
    auto_submit: bool = False
    headless: bool = False


@dataclass
class Config:
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    waterlooworks: WaterlooWorksConfig = field(default_factory=WaterlooWorksConfig)
    external: ExternalConfig = field(default_factory=ExternalConfig)
    apply: ApplyConfig = field(default_factory=ApplyConfig)
    data_dir: Path = Path(".coop_apply")
    output_dir: Path = Path("output")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"


def _fill(obj, data: dict, where: str):
    for key, value in data.items():
        if not hasattr(obj, key):
            raise ValueError(f"Unknown config key [{where}] {key}")
        current = getattr(obj, key)
        if isinstance(value, dict) and not isinstance(current, dict):
            _fill(current, value, f"{where}.{key}")
        else:
            setattr(obj, key, value)


def load_config(path: str | Path = "config.toml") -> Config:
    cfg = Config()
    path = Path(path)
    if path.exists():
        data = tomllib.loads(path.read_text())
        paths = data.pop("paths", {})
        _fill(cfg, data, "root")
        if "data_dir" in paths:
            cfg.data_dir = Path(paths["data_dir"])
        if "output_dir" in paths:
            cfg.output_dir = Path(paths["output_dir"])
    return cfg
