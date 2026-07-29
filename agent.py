"""
agent.py — main background loop: scan threads (read-only), classify, draft,
queue for review. "Approving" a draft no longer posts server-side — it
generates an X "intent" link (a URL that opens X with the action pre-filled)
for the operator to open on their own phone and confirm with one tap, since
that's the only step that ever needed a real, trusted login.

THREADING NOTE: Playwright's sync API is not safe to call from multiple threads.
The XBrowser instance lives entirely inside the background loop's thread. Any
other thread (like FastAPI's request handlers in api.py) that needs the browser
to do something must go through submit_browser_task() below.
"""

import time
import threading
import queue
import random
import re
from urllib.parse import quote

import config
import storage
import llm
from browser import XBrowser, jittered_delay

SEARCH_QUERIES = [
    "\"drop your project\"",
    "\"what are you building\"",
    "\"drop your url\"",
    "\"what are you working on\"",
    "\"share your startup\"",
]

_task_queue = queue.Queue()
_xb_instance = None


def get_browser():
    return _xb_instance


def submit_browser_task(fn, *args, timeout=90, **kwargs):
    """Call from any thread other than the agent loop's own thread. Queues fn(*args)
    to run inside the browser thread, blocks until it completes, and returns its result."""
    result_holder = {}
    done_event = threading.Event()

    def _wrapped():
        try:
            result_holder["value"] = fn(*args, **kwargs)
        except Exception as e:
            result_holder["error"] = e
        finally:
            done_event.set()

    _task_queue.put(_wrapped)
    if not done_event.wait(timeout=timeout):
        raise TimeoutError("Browser task timed out — the agent loop may be busy or unresponsive")
    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder.get("value")


# ─── Tap-to-send intent link builders (no login needed, no code posts anything) ─
def _extract_status_id(thread_url: str):
    match = re.search(r"/status/(\d+)", thread_url or "")
    return match.group(1) if match else None


def build_reply_intent_url(thread_url: str, text: str) -> str:
    status_id = _extract_status_id(thread_url)
    if not status_id:
        return None
    return f"https://x.com/intent/tweet?in_reply_to={status_id}&text={quote(text)}"


def build_like_intent_url(thread_url: str) -> str:
    status_id = _extract_status_id(thread_url)
    if not status_id:
        return None
    return f"https://x.com/intent/like?tweet_id={status_id}"


def build_post_intent_url(text: str) -> str:
    return f"https://x.com/intent/tweet?text={quote(text)}"


# ─── Pacing ──────────────────────────────────────────────────────────────
def _can_review_reply_now() -> bool:
    """Soft pacing guidance — since actual sending now happens on the operator's
    own phone (outside our control), this just limits how many drafts we surface
    for review at once, not a hard technical cap."""
    if storage.replies_in_last_30_min() >= config.MAX_REPLIES_PER_30MIN:
        return False
    counts = storage.get_today_counts()
    if counts["replies_posted"] >= config.MAX_REPLIES_PER_DAY:
        return False
    return True


def _can_review_mention_today() -> bool:
    counts = storage.get_today_counts()
    return counts.get("mentions_posted", 0) < 5


def _can_review_original_today() -> bool:
    counts = storage.get_today_counts()
    return counts["original_posts_posted"] < config.MAX_ORIGINAL_POSTS_PER_DAY


def scan_and_draft(xb: XBrowser):
    """One scan cycle: search for candidate threads, classify, draft, queue for review."""
    if not storage.is_active():
        print("[agent] paused, skipping scan")
        storage.set_scan_status("Paused")
        return

    query = random.choice(SEARCH_QUERIES)
    print(f"[agent] scanning: {query}")
    storage.set_scan_status(f'🔍 Searching: "{query}"...')
    candidates = xb.search_recent_posts(query, max_results=5)

    total = len(candidates)
    storage.set_scan_status(f"Found {total} candidate{'s' if total != 1 else ''}, checking each..." if total else "No candidates found this cycle")

    new_drafts = 0
    for i, post in enumerate(candidates, start=1):
        if storage.has_seen_thread(post["url"]):
            continue
        storage.mark_thread_seen(post["url"])

        storage.set_scan_status(f"Reading thread {i}/{total} ({post['handle']})...")
        thread = xb.read_thread(post["url"])
        existing_replies_text = "\n".join(thread["replies"][:5])

        knowledge_base = storage.get_knowledge_base()

        storage.set_scan_status(f"Classifying thread {i}/{total}...")
        classification = llm.classify_thread(
            thread["post_text"] or post["text"], existing_replies_text, "", knowledge_base
        )
        time.sleep(5)  # spacing so a burst of candidates doesn't trip Gemini's RPM ceiling

        if classification["action"] == "skip":
            print(f"[agent] skipping {post['url']}: {classification['reasoning']}")
            continue

        if classification["action"] == "ask_operator":
            storage.add_draft(
                thread_url=post["url"],
                author_handle=post["handle"],
                author_name=post["name"],
                original_post=thread["post_text"] or post["text"],
                context_snippet=existing_replies_text[:300],
                pain_quote=classification.get("pain_quote"),
                draft_type="knowledge_question",
                draft_text=classification.get("operator_question") or "Need more info to draft this one.",
                style_key="knowledge_question",
            )
            storage.log_activity(f"Question for you about {config.PRODUCT_NAME}, re: {post['handle']}'s post")
            new_drafts += 1
            continue

        if classification["action"] == "mention" and not _can_review_mention_today():
            print("[agent] mention review cap reached today, skipping")
            continue

        storage.set_scan_status(f"Drafting reply for thread {i}/{total}...")
        draft_text = llm.draft_reply(
            classification["action"],
            thread["post_text"] or post["text"],
            existing_replies_text,
            classification.get("pain_quote"),
            knowledge_base,
        )
        time.sleep(5)

        if not draft_text:
            print(f"[agent] empty draft for {post['url']}, skipping")
            continue

        storage.add_draft(
            thread_url=post["url"],
            author_handle=post["handle"],
            author_name=post["name"],
            original_post=thread["post_text"] or post["text"],
            context_snippet=existing_replies_text[:300],
            pain_quote=classification.get("pain_quote"),
            draft_type=classification["action"],
            draft_text=draft_text,
            style_key=classification["action"],
        )
        storage.log_activity(f"New draft ({classification['action']}) for {post['handle']}")
        new_drafts += 1

    storage.set_scan_status(f"Scan complete — {new_drafts} new draft{'s' if new_drafts != 1 else ''} added")
    storage.record_scan_completed()


def approve_draft(draft_id: int, edited_text: str = None):
    """Called from the API when the user taps Approve. Returns
    (success: bool, error: str or None, intent_url: str or None).
    For normal drafts: generates the tap-to-send link, doesn't post anything itself.
    For knowledge_question drafts: the "edited_text" IS the operator's answer —
    saved to the knowledge base instead of building any X link."""
    draft = storage.get_draft(draft_id)
    if not draft:
        return False, "Draft not found", None
    draft_type = draft["draft_type"]

    if draft_type == "knowledge_question":
        answer = (edited_text or "").strip()
        if not answer:
            return False, "Type an answer before saving", None
        storage.append_knowledge(f"Q: {draft['draft_text']} — A: {answer}")
        storage.update_draft_status(draft_id, "approved", draft_text=answer)
        storage.log_activity(f"Learned something new about {config.PRODUCT_NAME} from your answer")
        return True, None, None

    text_to_post = edited_text if edited_text else draft["draft_text"]

    if draft_type == "original_post":
        intent_url = build_post_intent_url(text_to_post)
    else:
        intent_url = build_reply_intent_url(draft["thread_url"], text_to_post)

    if not intent_url:
        return False, "Could not build a link for this draft (missing post ID)", None

    storage.update_draft_status(draft_id, "approved", draft_text=text_to_post)
    storage.mark_draft_posted(draft_id)

    if draft_type == "original_post":
        storage.increment_counter("original_posts_posted")
    else:
        storage.increment_counter("replies_posted")
        if draft_type == "mention":
            storage.increment_counter("mentions_posted")

    storage.log_activity(f"Ready to send ({draft_type}) for {draft['author_handle']} — opened on your phone")
    return True, None, intent_url


def skip_draft(draft_id: int):
    storage.update_draft_status(draft_id, "skipped")


def maybe_queue_original_post():
    if not storage.is_active() or not _can_review_original_today():
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


def like_intent_for_chat(url: str) -> str:
    """Used by the chat panel's 'like <url>' command — returns a tap-to-like
    link instead of trying to like it server-side."""
    return build_like_intent_url(url)


def search_web_for_chat(query: str):
    xb = get_browser()
    if xb is None:
        return []
    try:
        return submit_browser_task(xb.web_search, query)
    except Exception as e:
        print(f"[agent] search task failed: {e}")
        return []


def fetch_url_for_chat(url: str) -> str:
    xb = get_browser()
    if xb is None:
        return "(browser isn't ready yet, try again shortly)"
    try:
        return submit_browser_task(xb.read_external_link, url)
    except Exception as e:
        return f"(could not load that page: {e})"


def run_loop():
    """Entry point for the background thread. Runs forever until process exit."""
    global _xb_instance
    storage.init_db()
    config.validate()

    xb = XBrowser()
    xb.start()
    _xb_instance = xb
    print("[agent] browser started (read-only), entering main loop")

    last_scan = 0
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

            if now - last_original_post_check > 6 * 60 * 60:
                try:
                    maybe_queue_original_post()
                except Exception as e:
                    print(f"[agent] original post error: {e}")
                last_original_post_check = now

            # drain queued cross-thread browser tasks (chat URL fetches / searches)
            while True:
                try:
                    task = _task_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    task()
                except Exception as e:
                    print(f"[agent] queued browser task failed: {e}")

            time.sleep(3)
    finally:
        xb.stop()


def start_background_thread():
    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    return thread
