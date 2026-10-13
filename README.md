# coop-apply

A command-line assistant for the co-op job hunt. It:

1. **Finds jobs** on WaterlooWorks (from your own logged-in browser session) and on external company boards (Greenhouse, Lever, Ashby).
2. **Reads your resume** (PDF, DOCX, TXT or MD) and turns it into a structured profile with Claude.
3. **Ranks every posting** against that profile: first a free keyword prefilter, then Claude scores the best ones from 0 to 100 with reasons and gaps.
4. **Writes a tailored cover letter** for each job you pick (.txt and .pdf), using only facts from your resume.
5. **Applies:**
   - **WaterlooWorks:** uploads the cover letter to your Documents, builds the application package (resume, cover letter, grade report), and submits once you confirm.
   - **Company websites:** Claude reads each page of the application form, fills it from your resume and facts, uploads your resume and cover letter, and submits once you confirm (or on its own with auto-apply).

```
fetch ──► rank ──► list / show ──► letter ──► apply
  └──────────────── run (all of it, with a y/N before each job) ─────────┘
```

## Easiest way: the app (Windows)

1. Install Python from python.org (tick **"Add python.exe to PATH"** in the installer).
2. Download this repository as a ZIP and unzip it.
3. Double-click **`setup.bat`** once. It installs everything.
4. Double-click **`Start Co-op Apply.bat`** whenever you want to use it. The app opens in your browser. Keep the black window open while you use it.

The **Setup** tab walks you through your API key, resume, preferences and the WaterlooWorks login. The **Jobs** tab is used in order:

1. **Find jobs** collects WaterlooWorks postings. **🔎 Top companies in my field** has Claude search the web for the leading employers in your discipline and their current co-op postings on their own websites (takes a few minutes; roughly $1 of API use).
2. **Rank against my resume** scores the new postings.
3. **Apply to next best job** applies to your single highest-scoring job you haven't applied to yet.

Click any job to see why it fits, write or edit its cover letter, apply, or skip it. When the tool needs a decision, such as "Submit this application?", a Yes/No box pops up.

**✍️ Cover letter from a job description.** Paste any posting (or a link to it) and Claude reads it, checks your fit and writes a tailored cover letter, with the same preview / ask-for-changes / proofread tools. If you include the link, **Apply** works on it too.

**Applying on company websites.** For jobs outside WaterlooWorks, Claude reads each page of the application form (every field, dropdown, checkbox and upload, in any frame) and fills it from your resume, contact details and the **Facts for application forms** box in Setup, uploads your resume and cover letter, and clicks through Next until the site confirms the application. It writes short answers to "why this company" questions from your resume, picks "prefer not to say" for demographic questions, and never invents facts: a required question it has no facts for, a sign-in / account page (unless it can continue as a guest) or a CAPTCHA you'd have to solve is handed to you. The "protected by reCAPTCHA" badge most forms carry doesn't count. You approve the final Submit unless auto-apply is on.

It presses Submit **once**: it then waits for the site to answer (up to a minute while files upload) and only presses it again if the site points out a field to fix (e.g. "please enter a valid phone number"), or to answer the site's own "Are you sure you want to submit?" box. An application only counts as submitted when the site says it was received; if Submit was pressed but no confirmation appeared, the job is marked **needs you** with "check your email before applying again", never "please finish it".

**Auto-apply switch.** While it's on, the tool ranks any new jobs, then applies to your shortlisted jobs (WaterlooWorks, and company websites unless you turn that off in Setup), best first, submitting without asking. It skips postings whose deadline has passed, waits a little between jobs, and stops after **Max applications per auto-apply run** submissions, when you run out of shortlisted jobs, or when you switch it off. Anything only you can do - a sign-in, a question your facts don't answer, a missing button - marks that job **needs you** (with the reason) and it moves on; filter the list by "needs you" to finish those. Unusual WaterlooWorks pre-screening questions are answered by Claude from your facts when it can.

Problems that would hit every job the same way stop the run instead of marking job after job: if the Claude API can't be reached (no internet, a bad key, no credit), auto-apply stops and leaves your jobs as they were; if your WaterlooWorks login has expired, it skips WaterlooWorks jobs (untouched) and carries on with company websites. WaterlooWorks jobs are also skipped until you set your resume's WaterlooWorks document name in Setup. Jobs you made with **Cover letter from a job description** are never auto-applied; click Apply on them yourself.

On Mac or Linux, or after a manual install, run `coop-apply gui` to start the same app.

## Setup (command line)

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

All state lives in `.coop_apply/jobs.sqlite3`. A job moves through `new → shortlisted → prepared → submitted` (or `skipped` / `failed` / `needs_you`).

## Safety and honest limitations

- **You stay in the loop.** By default every submission waits for your yes. `apply.auto_submit = true` removes the WaterlooWorks confirmation; `--auto-submit` on the command line removes it for company websites too, as does the auto-apply switch in the app. Use them only once you trust your setup, because a bad application goes to employers under your name.
- **Your credentials are never handled.** You log in yourself in the browser window. The session cookies are stored in `.coop_apply/browser-profile/` (git-ignored). Treat that folder like a password.
- **Check the WaterlooWorks rules.** Read the WaterlooWorks / Co-operative Education terms of use before automating anything. Keep `apply.max_per_run` modest. Applying to roles you haven't read can hurt you, so skim `coop-apply show` output before saying yes.
- **WaterlooWorks has no API, so the tool depends on its page layout.** Posting details are parsed from visible text (labels like `Job Title:` and `Required Skills:`), which tolerates most redesigns. Finding rows, opening postings, paging, and the Apply/Upload/Submit buttons use CSS selectors in `[waterlooworks.selectors]`. **These defaults were tested against a mock page, not the live site.** On your first run, watch the browser. If something isn't found, run `coop-apply ww-debug` on that page; it saves the HTML and a screenshot to `.coop_apply/debug/`. Then adjust the selectors in `config.toml`.
- **Some WaterlooWorks postings say "apply through employer website".** These are routed to the external filler using the URL in the posting.
- **Company forms vary.** The form filler was tested on mock career pages, not live Greenhouse / Lever / Workday sites. Watch the first few applications. Questions your facts don't answer and CAPTCHAs are left for you.

## Vercel

The repository includes a tiny static info page (`site/`, `vercel.json`, `.vercelignore`) so a Vercel project connected to this repo deploys successfully. The app itself can't run on a web host: it drives your own browser for the WaterlooWorks login and keeps your files on your computer.

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
  gui.py, gui.html     the browser app (`coop-apply gui`), localhost-only with a per-launch token
  config.py            config.toml schema
  resume.py            resume -> ResumeProfile (Claude, structured output)
  matcher.py           prefilter + Claude fit scoring
  cover_letter.py      Claude cover letter + PDF rendering
  llm.py               Anthropic SDK wrapper (structured outputs, refusal fallback, errors)
  db.py                SQLite store
  sources/waterlooworks.py   Playwright scraper + text parser
  sources/external.py        Greenhouse / Lever / Ashby JSON APIs
  apply/waterlooworks.py     upload cover letter, build package, submit
  apply/formfill.py          Claude reads and fills company application forms, page by page
  apply/external.py          rule-based prefill (name, email, resume...) used before Claude
```
