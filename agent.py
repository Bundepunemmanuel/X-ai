"""
agent.py — the main background loop: scan for threads, classify, draft,
queue for approval (or auto-post if trusted), respect pacing caps.
Runs continuously in a background thread started by main.py.
"""

import time
import threading
import random

import config
import storage
import llm
from browser import XBrowser, jittered_delay

# "build in public" style search queries — threads where dropping a project
# link is genuinely on-topic, not random hijacking.
SEARCH_QUERIES = [
    "\"drop your project\"",
    "\"what are you building\"",
    "\"drop your url\"",
    "\"what are you working on\"",
    "\"share your startup\"",
]


def _can_post_reply_now() -> bool:
    if storage.replies_in_last_30_min() >= config.MAX_REPLIES_PER_30MIN:
        return False
    counts = storage.get_today_counts()
    if counts["replies_posted"] >= config.MAX_REPLIES_PER_DAY:
        return False
    return True


def _can_post_mention_today() -> bool:
    counts = storage.get_today_counts()
    return counts.get("mentions_posted", 0) < 5  # soft ceiling on Kairo mentions specifically


def _can_post_original_today() -> bool:
    counts = storage.get_today_counts()
    return counts["original_posts_posted"] < config.MAX_ORIGINAL_POSTS_PER_DAY


def _style_key_for(draft_type: str) -> str:
    return draft_type  # 'question' or 'mention' — kept simple, could get more granular later


def scan_and_draft(xb: XBrowser):
    """One scan cycle: search for candidate threads, classify, draft, queue."""
    if not storage.is_active():
        print("[agent] paused, skipping scan")
        return

    query = random.choice(SEARCH_QUERIES)
    print(f"[agent] scanning: {query}")
    candidates = xb.search_recent_posts(query, max_results=10)

    for post in candidates:
        if storage.has_seen_thread(post["url"]):
            continue
        storage.mark_thread_seen(post["url"])

        thread = xb.read_thread(post["url"])
        existing_replies_text = "\n".join(thread["replies"][:5])

        # if the post itself links to a product, try to read it for context
        commenter_page_content = ""
        # (left as best-effort: reading link would need URL extraction from post text/DOM,
        #  wired in read_thread's article locator if a link element is present)

        classification = llm.classify_thread(
            thread["post_text"] or post["text"], existing_replies_text, commenter_page_content
        )

        if classification["action"] == "skip":
            print(f"[agent] skipping {post['url']}: {classification['reasoning']}")
            continue

        if classification["action"] == "mention" and not _can_post_mention_today():
            print("[agent] mention cap reached today, downgrading to question or skipping")
            continue

        draft_text = llm.draft_reply(
            classification["action"],
            thread["post_text"] or post["text"],
            existing_replies_text,
            classification.get("pain_quote"),
        )

        if not draft_text:
            print(f"[agent] empty draft for {post['url']}, skipping")
            continue

        style_key = _style_key_for(classification["action"])
        draft_id = storage.add_draft(
            thread_url=post["url"],
            author_handle=post["handle"],
            author_name=post["name"],
            original_post=thread["post_text"] or post["text"],
            context_snippet=existing_replies_text[:300],
            pain_quote=classification.get("pain_quote"),
            draft_type=classification["action"],
            draft_text=draft_text,
            style_key=style_key,
        )
        storage.log_activity(f"New draft ({classification['action']}) for {post['handle']}")

        # auto-post if this style is trusted
        if storage.is_auto_post_enabled(style_key) and _can_post_reply_now():
            _post_draft(xb, draft_id)
            jittered_delay()


def _post_draft(xb: XBrowser, draft_id: int):
    draft = storage.get_draft(draft_id)
    if not draft:
        return
    success = xb.post_reply(draft["thread_url"], draft["draft_text"])
    if success:
        storage.mark_draft_posted(draft_id)
        storage.increment_counter("replies_posted")
        if draft["draft_type"] == "mention":
            storage.increment_counter("mentions_posted")
        storage.log_activity(f"Auto-posted reply to {draft['author_handle']}")
    else:
        storage.log_activity(f"Failed to post reply to {draft['author_handle']}")


def approve_draft(xb: XBrowser, draft_id: int, edited_text: str = None):
    """Called from the API when the user taps Approve (with optional edited text)."""
    draft = storage.get_draft(draft_id)
    if not draft:
        return False
    text_to_post = edited_text if edited_text else draft["draft_text"]

    if not _can_post_reply_now():
        storage.log_activity("Pacing cap reached — approval queued but not posted yet")
        return False

    success = xb.post_reply(draft["thread_url"], text_to_post)
    if success:
        storage.update_draft_status(draft_id, "approved", draft_text=text_to_post)
        storage.mark_draft_posted(draft_id)
        storage.increment_counter("replies_posted")
        if draft["draft_type"] == "mention":
            storage.increment_counter("mentions_posted")
        storage.record_approval(draft["style_key"])
        storage.log_activity(f"Posted reply to {draft['author_handle']} (approved)")
    return success


def skip_draft(draft_id: int):
    storage.update_draft_status(draft_id, "skipped")


def check_notifications(xb: XBrowser):
    """Look for replies to the assistant's own posts and reactive DMs."""
    if not storage.is_active():
        return

    mentions = xb.get_new_mentions()
    for m in mentions:
        if storage.has_seen_thread(m["url"]):
            continue
        storage.mark_thread_seen(m["url"])

        result = llm.draft_followup_reply(
            conversation_history="(original exchange context not retained across sessions yet)",
            their_last_message=m["text"],
        )

        if result["needs_human"]:
            storage.add_draft(
                thread_url=m["url"], author_handle=m["handle"], author_name=m["name"],
                original_post=m["text"], context_snippet="", pain_quote=None,
                draft_type="followup", draft_text="[Needs your personal response — they asked something that shouldn't be auto-answered]",
                style_key="followup",
            )
            storage.log_activity(f"Flagged for you: reply from {m['handle']} needs a human")
            continue

        if result["status"] == "closed" or not result["reply"]:
            continue

        storage.add_draft(
            thread_url=m["url"], author_handle=m["handle"], author_name=m["name"],
            original_post=m["text"], context_snippet="", pain_quote=None,
            draft_type="followup", draft_text=result["reply"], style_key="followup",
        )
        storage.log_activity(f"New follow-up draft for {m['handle']}")

    dms = xb.get_new_dm_conversations()
    for dm in dms:
        if storage.has_seen_thread(dm["conversation_url"]):
            continue
        storage.mark_thread_seen(dm["conversation_url"])

        result = llm.draft_followup_reply(
            conversation_history="(DM thread)", their_last_message=dm["last_message"],
        )
        if result["needs_human"] or result["status"] == "closed" or not result["reply"]:
            if result["needs_human"]:
                storage.log_activity("Flagged for you: a DM needs a human response")
            continue

        storage.add_draft(
            thread_url=dm["conversation_url"], author_handle="(DM)", author_name="(DM)",
            original_post=dm["last_message"], context_snippet="", pain_quote=None,
            draft_type="dm", draft_text=result["reply"], style_key="dm",
        )
        storage.log_activity("New DM reply draft")


def maybe_post_original(xb: XBrowser):
    if not storage.is_active() or not _can_post_original_today():
        return
    text = llm.draft_original_post()
    if not text:
        return
    storage.add_draft(
        thread_url="(original post)", author_handle="(you)", author_name="(you)",
        original_post="", context_snippet="", pain_quote=None,
        draft_type="original_post", draft_text=text, style_key="original_post",
    )
    storage.log_activity("New original post draft ready for review")


# module-level reference so api.py can act on approvals immediately,
# using the same live browser session the background loop owns.
_xb_instance = None


def get_browser():
    return _xb_instance


def run_loop():
    """Entry point for the background thread. Runs forever until process exit."""
    global _xb_instance
    storage.init_db()
    config.validate()

    xb = XBrowser()
    xb.start()
    _xb_instance = xb
    print("[agent] browser started, entering main loop")

    last_scan = 0
    last_notification_check = 0
    last_original_post_check = 0

    try:
        while True:
            now = time.time()

            if now - last_scan > config.SCAN_INTERVAL_SECONDS:
                try:
                    scan_and_draft(xb)
                except Exception as e:
                    print(f"[agent] scan error: {e}")
                last_scan = now

            if now - last_notification_check > config.NOTIFICATION_CHECK_INTERVAL_SECONDS:
                try:
                    check_notifications(xb)
                except Exception as e:
                    print(f"[agent] notification check error: {e}")
                last_notification_check = now

            if now - last_original_post_check > 6 * 60 * 60:  # check every 6 hours
                try:
                    maybe_post_original(xb)
                except Exception as e:
                    print(f"[agent] original post error: {e}")
                last_original_post_check = now

            time.sleep(30)
    finally:
        xb.stop()


def start_background_thread():
    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    return thread
