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
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('active', '1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("kairo_knowledge", DEFAULT_KAIRO_KNOWLEDGE),
        )


# ─── Kairo knowledge base — grows over time, never hardcoded/frozen ─────────
DEFAULT_KAIRO_KNOWLEDGE = """KAIRO — Customer acquisition tool for solo founders.

POSITIONING: "Reddit is leaking customer intent right now." Kairo scans Reddit
24/7, finds people already asking for a tool like yours, scores their buying
intent, and drafts a reply — so founders stop manually searching Reddit for
hours and instead wake up to customers already found.

HOW IT WORKS (4 steps):
1. Paste your site URL — Kairo reads your product, understands your customer,
   maps which subreddits they hang out in.
2. Kairo hunts — scans Reddit every 15 minutes, scores every post for buying
   intent, pain signals, and competitor frustration.
3. You see real leads — each lead labeled (active/passive), scored, with a
   decay timer showing how long the window stays hot.
4. Reply with confidence — Kairo drafts a value-first, human-sounding reply
   calibrated to the signal type. One click opens the Reddit thread.

THE TWO DEMAND TYPES (important — changes how a reply should be written):
- ACTIVE DEMAND ("shopping right now"): posts like "what tool do you use for
  X?", "looking for software that...", "can anyone recommend...". High intent,
  short window. Reply fast, be direct, mentioning the product is natural here.
- PASSIVE DEMAND ("doesn't know you exist yet"): posts like "I hate how long X
  takes", "there has to be a better way", "why is this so expensive?". Lead
  with empathy, add value first, do NOT pitch immediately — the product should
  only come up if it's earned, often not in the first reply at all.

STATS (from the live landing page, for context/color in replies — treat as
illustrative, not guaranteed current numbers to quote as fact to strangers):
~847 posts scanned daily, ~9.2 avg intent score, ~23 min avg lead window,
results in about 2 minutes from pasting a URL.

PRICING:
- Starter: $29/month — 10 leads/day, 3 subreddits monitored, active/passive
  labels, decay timers, "Karma Builder" feature. First month free (limited
  spots).
- Pro: free for 1 month then $49/month — 50 leads/day, 10 subreddits, AI draft
  replies, email alerts for critical leads.
- Unlimited: $99/month — unlimited leads/subreddits, competitor tracking,
  priority support.
- No credit card required to start, upgrade anytime.

TONE/POSITIONING NOTES: "Distribution is the only problem that matters" — most
founders can build, few can distribute. Kairo frames itself as the distribution
unlock, not just a scraping tool. Built specifically for solo founders, not
agencies or enterprises."""


def get_knowledge_base() -> str:
    return get_setting("kairo_knowledge", DEFAULT_KAIRO_KNOWLEDGE)


def append_knowledge(fact: str):
    current = get_knowledge_base()
    updated = current.rstrip() + f"\n\n[Added by operator]: {fact.strip()}"
    set_setting("kairo_knowledge", updated)


# ─── Scan status/stats — proof-of-life for the background loop ─────────────
def set_scan_status(status: str):
    set_setting("scan_status", status)


def get_scan_status() -> str:
    return get_setting("scan_status", "Idle — waiting for next cycle")


def record_scan_completed():
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('scan_count', '0') "
            "ON CONFLICT(key) DO NOTHING"
        )
        row = conn.execute("SELECT value FROM settings WHERE key = 'scan_count'").fetchone()
        count = int(row["value"]) + 1 if row else 1
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('scan_count', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(count),),
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('last_scan_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(time.time()),),
        )


def get_scan_stats() -> dict:
    last_scan_at = get_setting("last_scan_at")
    return {
        "status": get_scan_status(),
        "last_scan_at": float(last_scan_at) if last_scan_at else None,
        "scan_count": int(get_setting("scan_count", "0")),
    }


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
