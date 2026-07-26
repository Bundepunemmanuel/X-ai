"""
storage.py — the only file that touches the database.
Everything else calls these functions instead of writing SQL directly.
"""

import sqlite3
import json
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_url TEXT NOT NULL,
    author_handle TEXT,
    author_name TEXT,
    original_post TEXT,
    context_snippet TEXT,
    pain_quote TEXT,
    draft_type TEXT NOT NULL,          -- 'question' | 'mention' | 'followup' | 'dm'
    draft_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'skipped' | 'posted' | 'edited'
    style_key TEXT,                     -- e.g. 'question' or 'mention' — used for auto-post trust tracking
    created_at REAL NOT NULL,
    decided_at REAL,
    posted_at REAL
);

CREATE TABLE IF NOT EXISTS daily_counters (
    day TEXT PRIMARY KEY,               -- 'YYYY-MM-DD'
    replies_posted INTEGER NOT NULL DEFAULT 0,
    original_posts_posted INTEGER NOT NULL DEFAULT 0,
    mentions_posted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS style_trust (
    style_key TEXT PRIMARY KEY,
    approved_count INTEGER NOT NULL DEFAULT 0,
    auto_post_enabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,                 -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_threads (
    thread_url TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
"""


@contextmanager
def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # sensible defaults
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('active', '1')"
        )


# ─── Threads already seen (avoid re-processing the same thread every scan) ─
def has_seen_thread(thread_url: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_threads WHERE thread_url = ?", (thread_url,)
        ).fetchone()
        return row is not None


def mark_thread_seen(thread_url: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_threads (thread_url, seen_at) VALUES (?, ?)",
            (thread_url, time.time()),
        )


# ─── Drafts ────────────────────────────────────────────────────────────────
def add_draft(
    thread_url, author_handle, author_name, original_post,
    context_snippet, pain_quote, draft_type, draft_text, style_key,
):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO drafts
               (thread_url, author_handle, author_name, original_post, context_snippet,
                pain_quote, draft_type, draft_text, style_key, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (thread_url, author_handle, author_name, original_post, context_snippet,
             pain_quote, draft_type, draft_text, style_key, time.time()),
        )
        return cur.lastrowid


def get_pending_drafts(limit=50):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM drafts WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_draft(draft_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None


def update_draft_status(draft_id, status, draft_text=None):
    with get_db() as conn:
        if draft_text is not None:
            conn.execute(
                "UPDATE drafts SET status = ?, draft_text = ?, decided_at = ? WHERE id = ?",
                (status, draft_text, time.time(), draft_id),
            )
        else:
            conn.execute(
                "UPDATE drafts SET status = ?, decided_at = ? WHERE id = ?",
                (status, time.time(), draft_id),
            )


def mark_draft_posted(draft_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE drafts SET status = 'posted', posted_at = ? WHERE id = ?",
            (time.time(), draft_id),
        )


# ─── Daily counters ─────────────────────────────────────────────────────────
def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def get_today_counts():
    day = _today()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_counters WHERE day = ?", (day,)
        ).fetchone()
        if not row:
            conn.execute("INSERT INTO daily_counters (day) VALUES (?)", (day,))
            return {"replies_posted": 0, "original_posts_posted": 0, "mentions_posted": 0}
        return dict(row)


def increment_counter(field: str):
    day = _today()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO daily_counters (day) VALUES (?) ON CONFLICT(day) DO NOTHING", (day,)
        )
        conn.execute(
            f"UPDATE daily_counters SET {field} = {field} + 1 WHERE day = ?", (day,)
        )


def replies_in_last_30_min():
    cutoff = time.time() - 30 * 60
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM drafts WHERE status = 'posted' AND posted_at > ?",
            (cutoff,),
        ).fetchone()
        return row["c"]


# ─── Style trust (auto-post unlock) ─────────────────────────────────────────
def record_approval(style_key: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO style_trust (style_key, approved_count) VALUES (?, 1) "
            "ON CONFLICT(style_key) DO UPDATE SET approved_count = approved_count + 1",
            (style_key,),
        )
        row = conn.execute(
            "SELECT approved_count FROM style_trust WHERE style_key = ?", (style_key,)
        ).fetchone()
        if row and row["approved_count"] >= config.AUTO_POST_APPROVAL_THRESHOLD:
            conn.execute(
                "UPDATE style_trust SET auto_post_enabled = 1 WHERE style_key = ?",
                (style_key,),
            )


def is_auto_post_enabled(style_key: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT auto_post_enabled FROM style_trust WHERE style_key = ?", (style_key,)
        ).fetchone()
        return bool(row and row["auto_post_enabled"])


def get_style_trust():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM style_trust").fetchall()
        return [dict(r) for r in rows]


# ─── Chat history ───────────────────────────────────────────────────────────
def add_chat_message(role: str, content: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_history (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, time.time()),
        )


def get_chat_history(limit=50):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_history ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ─── Activity log (shown in dashboard footer) ───────────────────────────────
def log_activity(message: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO activity_log (message, created_at) VALUES (?, ?)",
            (message, time.time()),
        )


def get_recent_activity(limit=20):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Settings (pause/resume, auto-post global toggle) ───────────────────────
def get_setting(key: str, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def is_active() -> bool:
    return get_setting("active", "1") == "1"
