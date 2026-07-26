"""
config.py — all environment variables and tunable settings live here.
Nothing else in the project should call os.environ directly; import from here instead.
"""

import os

# ─── Credentials ─────────────────────────────────────────────────────────
X_USERNAME = os.environ.get("X_USERNAME", "")
X_PASSWORD = os.environ.get("X_PASSWORD", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ─── Product info (used in reply prompts) ────────────────────────────────
PRODUCT_NAME = os.environ.get("PRODUCT_NAME", "Kairo")
PRODUCT_URL = os.environ.get("PRODUCT_URL", "https://kairo-omega.vercel.app/")
PRODUCT_DESCRIPTION = os.environ.get(
    "PRODUCT_DESCRIPTION",
    "Kairo scans Reddit 24/7 for people already asking for a tool like yours, "
    "scores how good a fit they are, and drafts a reply — so solo founders don't "
    "have to manually dig through Reddit looking for their next user.",
)

# ─── Pacing ───────────────────────────────────────────────────────────────
MIN_REPLIES_PER_DAY = int(os.environ.get("MIN_REPLIES_PER_DAY", 10))
MAX_REPLIES_PER_DAY = int(os.environ.get("MAX_REPLIES_PER_DAY", 30))
MAX_REPLIES_PER_30MIN = int(os.environ.get("MAX_REPLIES_PER_30MIN", 15))  # hard safety ceiling only
MAX_ORIGINAL_POSTS_PER_DAY = int(os.environ.get("MAX_ORIGINAL_POSTS_PER_DAY", 1))
MIN_GAP_SECONDS = int(os.environ.get("MIN_GAP_SECONDS", 60))       # jittered gap lower bound
MAX_GAP_SECONDS = int(os.environ.get("MAX_GAP_SECONDS", 8 * 60))    # jittered gap upper bound, weighted toward higher end

# how often the background loop wakes up to scan for new threads
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", 20 * 60))
# how often it checks its own notifications/DMs
NOTIFICATION_CHECK_INTERVAL_SECONDS = int(os.environ.get("NOTIFICATION_CHECK_INTERVAL_SECONDS", 15 * 60))

# number of approved drafts of a given style before auto-post unlocks for that style
AUTO_POST_APPROVAL_THRESHOLD = int(os.environ.get("AUTO_POST_APPROVAL_THRESHOLD", 5))

# ─── Storage ──────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "assistant.db")
SESSION_STATE_PATH = os.environ.get("SESSION_STATE_PATH", "x_session_state.json")

# ─── Misc ─────────────────────────────────────────────────────────────────
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def validate():
    """Call at startup — fail loudly instead of quietly misbehaving with missing config."""
    missing = []
    if not X_USERNAME:
        missing.append("X_USERNAME")
    if not X_PASSWORD:
        missing.append("X_PASSWORD")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
