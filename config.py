"""
config.py — all environment variables and tunable settings live here.
Nothing else in the project should call os.environ directly; import from here instead.
"""

import os

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

# ─── Pacing (soft review-surfacing limits, not hard technical caps) ──────
MIN_REPLIES_PER_DAY = int(os.environ.get("MIN_REPLIES_PER_DAY", 10))
MAX_REPLIES_PER_DAY = int(os.environ.get("MAX_REPLIES_PER_DAY", 30))
MAX_REPLIES_PER_30MIN = int(os.environ.get("MAX_REPLIES_PER_30MIN", 15))
MAX_ORIGINAL_POSTS_PER_DAY = int(os.environ.get("MAX_ORIGINAL_POSTS_PER_DAY", 1))
MIN_GAP_SECONDS = int(os.environ.get("MIN_GAP_SECONDS", 60))
MAX_GAP_SECONDS = int(os.environ.get("MAX_GAP_SECONDS", 8 * 60))

SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", 20 * 60))

# ─── Storage ──────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "assistant.db")

# ─── Misc ─────────────────────────────────────────────────────────────────
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


def validate():
    """Call at startup — fail loudly for what's truly required to run at all."""
    if not GEMINI_API_KEY:
        raise RuntimeError("Missing required environment variable: GEMINI_API_KEY")
