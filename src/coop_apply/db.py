"""SQLite store for jobs, match scores and application status."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Job, MatchResult, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    resume_hash TEXT NOT NULL,
    score INTEGER NOT NULL,
    data TEXT NOT NULL,
    scored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    status TEXT NOT NULL,
    cover_letter_path TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # ---- jobs ----
    def upsert_job(self, job: Job) -> bool:
        """Insert or refresh a job. Returns True if it was new."""
        now = _now()
        existing = self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE jobs SET data = ?, last_seen = ? WHERE id = ?",
                (job.model_dump_json(), now, job.id),
            )
        else:
            self.conn.execute(
                "INSERT INTO jobs (id, source, data, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
                (job.id, job.source, job.model_dump_json(), now, now),
            )
        self.conn.commit()
        return not existing

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row["data"]) if row else None

    def rename_job(self, old_id: str, new_id: str) -> None:
        """Move a job (with its score and application status) to a new ID."""
        job = self.get_job(old_id)
        if not job or self.get_job(new_id):
            return
        job.id = new_id
        self.conn.execute("UPDATE jobs SET id = ?, data = ? WHERE id = ?", (new_id, job.model_dump_json(), old_id))
        self.conn.execute("UPDATE matches SET job_id = ? WHERE job_id = ?", (new_id, old_id))
        self.conn.execute("UPDATE applications SET job_id = ? WHERE job_id = ?", (new_id, old_id))
        self.conn.commit()

    def find_unnumbered_ww_job(self, title: str, company: str) -> Job | None:
        """An older WaterlooWorks job saved without its real posting number, matched by title + company."""
        for job in self.all_jobs():
            pid = job.id.split(":", 1)[1]
            if job.source == "waterlooworks" and not pid.isdigit() and job.title == title and job.company == company:
                return job
        return None

    def all_jobs(self) -> list[Job]:
        return [Job.model_validate_json(r["data"]) for r in self.conn.execute("SELECT data FROM jobs")]

    # ---- matches ----
    def save_match(self, job_id: str, resume_hash: str, match: MatchResult):
        self.conn.execute(
            "INSERT OR REPLACE INTO matches (job_id, resume_hash, score, data, scored_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, resume_hash, match.score, match.model_dump_json(), _now()),
        )
        self.conn.commit()

    def get_match(self, job_id: str, resume_hash: str | None = None) -> MatchResult | None:
        row = self.conn.execute("SELECT data, resume_hash FROM matches WHERE job_id = ?", (job_id,)).fetchone()
        if not row or (resume_hash and row["resume_hash"] != resume_hash):
            return None
        return MatchResult.model_validate_json(row["data"])

    def ranked(self, min_score: int = 0, statuses: list[str] | None = None) -> list[tuple[Job, MatchResult, str]]:
        rows = self.conn.execute(
            """
            SELECT j.data AS job, m.data AS match, COALESCE(a.status, 'new') AS status
            FROM matches m JOIN jobs j ON j.id = m.job_id
            LEFT JOIN applications a ON a.job_id = j.id
            WHERE m.score >= ?
            ORDER BY m.score DESC
            """,
            (min_score,),
        ).fetchall()
        out = [
            (Job.model_validate_json(r["job"]), MatchResult.model_validate_json(r["match"]), r["status"])
            for r in rows
        ]
        if statuses:
            out = [t for t in out if t[2] in statuses]
        return out

    # ---- applications ----
    def set_status(self, job_id: str, status: Status, cover_letter_path: str | None = None, notes: str | None = None):
        current = self.conn.execute("SELECT cover_letter_path, notes FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        if current:
            cover_letter_path = cover_letter_path or current["cover_letter_path"]
            notes = notes if notes is not None else current["notes"]
        self.conn.execute(
            "INSERT OR REPLACE INTO applications (job_id, status, cover_letter_path, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, status.value, cover_letter_path, notes, _now()),
        )
        self.conn.commit()

    def get_application(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
