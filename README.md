# coop-apply

A command-line assistant for the co-op job hunt. It:

1. **Finds jobs** on WaterlooWorks (from your own logged-in browser session) and on external company boards (Greenhouse, Lever, Ashby).
2. **Reads your resume** (PDF, DOCX, TXT or MD) and turns it into a structured profile with Claude.
3. **Ranks every posting** against that profile: first a free keyword prefilter, then Claude scores the best ones from 0 to 100 with reasons and gaps.
4. **Writes a tailored cover letter** for each job you pick (.txt and .pdf), using only facts from your resume.
5. **Applies:**
   - **WaterlooWorks:** uploads the cover letter to your Documents, builds the application package (resume, cover letter, grade report), and submits once you confirm.
   - **External sites:** opens the form, fills in your name, contact details, links, resume and cover letter, and hands the browser back to you for custom questions and the Submit click.

```
fetch ──► rank ──► list / show ──► letter ──► apply
  └──────────────── run (all of it, with a y/N before each job) ─────────┘
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...      # https://console.anthropic.com
coop-apply init                         # writes config.toml
```

Edit `config.toml`:

- `profile.resume_path`: path to your resume.
- `waterlooworks.resume_document`, `transcript_document`: the names of documents you've **already uploaded** to WaterlooWorks, exactly as they appear there. The tool picks them by name when it builds each package.
- `external.greenhouse` / `lever` / `ashby`: board tokens for companies you want to watch. The token is the slug in the company's careers URL, e.g. `boards.greenhouse.io/`**`stripe`**.
- `profile.cover_letter_notes`: the term you're applying for and anything you want emphasized.

## Usage

```bash
coop-apply resume              # parse your resume (cached until the file changes)
coop-apply login               # log in to WaterlooWorks once (SSO + Duo); the session is saved
coop-apply fetch               # WaterlooWorks + external boards  (--ww / --external for one)
coop-apply rank                # score new postings against your resume
coop-apply list                # ranked shortlist  (--min-score 0 shows everything)
coop-apply show ww:123456      # the posting, its fit score, reasons and gaps
coop-apply letter ww:123456    # write a cover letter -> output/
coop-apply apply ww:123456 gh:stripe:4012345
coop-apply run                 # fetch -> rank -> letter + apply for the top matches
coop-apply mark ww:123456 skipped
```

Job IDs are prefixed by source: `ww:` WaterlooWorks, `gh:` Greenhouse, `lever:`, `ashby:`.

## How it decides what's a good fit

- **Prefilter (free):** postings whose title contains `search.exclude_keywords` (for example "senior") are dropped. If you set `search.locations`, postings elsewhere are dropped too. The rest are ranked by how many of your skills and technologies appear in them.
- **Claude scoring:** the top `search.max_llm_scores` postings are scored with `claude.effort_rank = "low"` to keep costs down. Your profile goes in a cached system prompt, so repeat scoring is cheap. Postings at or above `search.min_score` are marked `shortlisted`.
- **Cover letters:** these use a higher effort setting. The prompt forbids inventing employers, metrics or skills. If the posting asks for something you lack, the letter leaves it out or frames it honestly.

All state lives in `.coop_apply/jobs.sqlite3`. A job moves through `new → shortlisted → prepared → submitted` (or `skipped` / `failed`).

## Safety and honest limitations

- **You stay in the loop.** By default every WaterlooWorks submission waits for your `y` in the terminal, and external forms are always submitted by you in the browser. `--auto-submit` / `apply.auto_submit = true` removes the WaterlooWorks confirmation. Use it only once you trust your setup, because a bad package goes to employers under your name.
- **Your credentials are never handled.** You log in yourself in the browser window. The session cookies are stored in `.coop_apply/browser-profile/` (git-ignored). Treat that folder like a password.
- **Check the WaterlooWorks rules.** Read the WaterlooWorks / Co-operative Education terms of use before automating anything. Keep `apply.max_per_run` modest. Applying to roles you haven't read can hurt you, so skim `coop-apply show` output before saying yes.
- **WaterlooWorks has no API, so the tool depends on its page layout.** Posting details are parsed from visible text (labels like `Job Title:` and `Required Skills:`), which tolerates most redesigns. Finding rows, opening postings, paging, and the Apply/Upload/Submit buttons use CSS selectors in `[waterlooworks.selectors]`. **These defaults were tested against a mock page, not the live site.** On your first run, watch the browser. If something isn't found, run `coop-apply ww-debug` on that page; it saves the HTML and a screenshot to `.coop_apply/debug/`. Then adjust the selectors in `config.toml`.
- **Some WaterlooWorks postings say "apply through employer website".** These are routed to the external filler using the URL in the posting.
- **External forms vary.** Custom questions, EEO surveys and CAPTCHAs are left for you to complete.

## Development

```bash
pytest
```

The tests run without network access or an API key:
- A local fake Messages API checks the Claude calls: request shape, structured output, effort, caching and the refusal fallback.
- A fake WaterlooWorks page (table, modal and pagination) and a fake application form run in real headless Chromium. Set `COOP_APPLY_CHROMIUM=/path/to/chrome` if Playwright's bundled browser isn't installed.

Layout:

```
src/coop_apply/
  cli.py               commands
  config.py            config.toml schema
  resume.py            resume -> ResumeProfile (Claude, structured output)
  matcher.py           prefilter + Claude fit scoring
  cover_letter.py      Claude cover letter + PDF rendering
  llm.py               Anthropic SDK wrapper (structured outputs, refusal fallback, errors)
  db.py                SQLite store
  sources/waterlooworks.py   Playwright scraper + text parser
  sources/external.py        Greenhouse / Lever / Ashby JSON APIs
  apply/waterlooworks.py     upload cover letter, build package, submit
  apply/external.py          generic form filler
```
