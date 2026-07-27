"""
agent.py — the main background loop: scan for threads, classify, draft,
queue for approval (or auto-post if trusted), respect pacing caps.
Runs continuously in a background thread started by main.py.

THREADING NOTE: Playwright's sync API is not safe to call from multiple threads.
The XBrowser instance lives entirely inside the background loop's thread. Any other
thread (like FastAPI's request handlers in api.py) that needs the browser to do
something must go through submit_browser_task() below, which queues the work and
lets the loop's own thread execute it, rather than touching Playwright directly.
"""

import time
import threading
import queue
import random
import json

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

_task_queue = queue.Queue()


def submit_browser_task(fn, *args, timeout=90, **kwargs):
    """Call from any thread other than the agent loop's own thread. Queues fn(*args, **kwargs)
    to run inside the browser thread, blocks until it completes, and returns its result
    (or raises whatever exception it raised)."""
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
    candidates = xb.search_recent_posts(query, max_results=5)  # kept small to limit Gemini calls per cycle

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
        time.sleep(5)  # space out Gemini calls so a burst of candidates doesn't trip the RPM ceiling

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
        time.sleep(5)  # same spacing after the drafting call

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
    draft_type = draft["draft_type"]

    if draft_type == "original_post":
        success = xb.post_original(draft["draft_text"])
    elif draft_type == "dm":
        success = xb.send_dm(draft["thread_url"], draft["draft_text"])
    else:
        success = xb.post_reply(draft["thread_url"], draft["draft_text"])

    if success:
        storage.mark_draft_posted(draft_id)
        if draft_type == "original_post":
            storage.increment_counter("original_posts_posted")
        else:
            storage.increment_counter("replies_posted")
            if draft_type == "mention":
                storage.increment_counter("mentions_posted")
        storage.log_activity(f"Auto-posted ({draft_type}) for {draft['author_handle']}")
    else:
        storage.log_activity(f"Failed to auto-post ({draft_type}) for {draft['author_handle']}")


def approve_draft(xb: XBrowser, draft_id: int, edited_text: str = None):
    """Called from the API when the user taps Approve (with optional edited text).
    Returns (success: bool, error_message: str or None).
    NOTE: xb is passed in for reference/nullability checks, but the actual Playwright
    call is routed through submit_browser_task so it runs on the correct thread."""
    draft = storage.get_draft(draft_id)
    if not draft:
        return False, "Draft not found"
    text_to_post = edited_text if edited_text else draft["draft_text"]
    draft_type = draft["draft_type"]

    # pacing check differs by type — original posts have their own daily cap,
    # not the reply pacing cap
    if draft_type == "original_post":
        if not _can_post_original_today():
            return False, "Original post cap reached for today"
    else:
        if not _can_post_reply_now():
            storage.log_activity("Pacing cap reached — approval queued but not posted yet")
            return False, "Reply pacing cap reached (15 per 30 min, or daily limit) — try again shortly"

    try:
        if draft_type == "original_post":
            success = submit_browser_task(xb.post_original, text_to_post)
        elif draft_type == "dm":
            success = submit_browser_task(xb.send_dm, draft["thread_url"], text_to_post)
        else:
            success = submit_browser_task(xb.post_reply, draft["thread_url"], text_to_post)
    except Exception as e:
        error_msg = f"Browser action failed: {e}"
        storage.log_activity(f"Failed to post ({draft_type}) for {draft['author_handle']}: {e}")
        return False, error_msg

    if not success:
        real_reason = xb.last_error or "no specific reason captured — check Render logs"
        storage.log_activity(f"Failed to post ({draft_type}) for {draft['author_handle']}: {real_reason}")
        return False, real_reason

    storage.update_draft_status(draft_id, "approved", draft_text=text_to_post)
    storage.mark_draft_posted(draft_id)

    if draft_type == "original_post":
        storage.increment_counter("original_posts_posted")
    else:
        storage.increment_counter("replies_posted")
        if draft_type == "mention":
            storage.increment_counter("mentions_posted")

    storage.record_approval(draft["style_key"])
    storage.log_activity(f"Posted ({draft_type}) for {draft['author_handle']} (approved)")
    return True, None


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
        time.sleep(5)

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
        time.sleep(5)
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


def search_web_for_chat(query: str):
    """Thread-safe wrapper api.py can call to run a real web search through the live browser."""
    xb = get_browser()
    if xb is None:
        return []
    try:
        return submit_browser_task(xb.web_search, query)
    except Exception as e:
        print(f"[agent] search task failed: {e}")
        return []


def like_url_for_chat(url: str):
    """Thread-safe wrapper: actually likes a post via the live browser session.
    Returns (success: bool, error: str or None) — real result, not an LLM guess."""
    xb = get_browser()
    if xb is None:
        return False, "browser isn't ready yet"
    if not xb.logged_in:
        return False, "not logged into X yet"
    try:
        success = submit_browser_task(xb.like_post, url)
        return success, (None if success else (xb.last_error or "unknown failure"))
    except Exception as e:
        return False, str(e)


def import_session(cookie_json_str: str):
    """Accepts pasted cookie data (either a bare array of cookie objects, like what
    Cookie-Editor exports, or a full Playwright storage_state object) and writes it
    to the session file in the format Playwright expects, then reloads the live
    browser session immediately. Returns (success: bool, message: str)."""
    try:
        data = json.loads(cookie_json_str)
    except Exception as e:
        return False, f"That doesn't look like valid JSON: {e}"

    # normalize into Playwright's storage_state shape: {"cookies": [...], "origins": []}
    if isinstance(data, list):
        cookies = data
    elif isinstance(data, dict) and "cookies" in data:
        cookies = data["cookies"]
    else:
        return False, "Unrecognized format — expected a cookie array or a storage_state object with a 'cookies' key"

    normalized = []
    for c in cookies:
        if "name" not in c or "value" not in c:
            continue
        normalized.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".x.com"),
            "path": c.get("path", "/"),
            "expires": c.get("expires", c.get("expirationDate", -1)) or -1,
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True),
            "sameSite": c.get("sameSite", "Lax") if c.get("sameSite") in ("Strict", "Lax", "None") else "Lax",
        })

    if not normalized:
        return False, "No usable cookies found in that data"

    storage_state = {"cookies": normalized, "origins": []}
    try:
        with open(config.SESSION_STATE_PATH, "w") as f:
            json.dump(storage_state, f)
    except Exception as e:
        return False, f"Could not save session file: {e}"

    xb = get_browser()
    if xb is None:
        return True, "Session saved, but browser isn't ready yet to reload it — it'll be picked up on next restart"

    try:
        logged_in = submit_browser_task(xb.reload_session)
    except Exception as e:
        return False, f"Session saved but reload failed: {e}"

    if logged_in:
        storage.log_activity("Session imported successfully — now logged into X")
        return True, f"Imported {len(normalized)} cookies and reloaded — now logged into X!"
    else:
        storage.log_activity("Session imported but still not logged in — cookies may be incomplete/expired")
        return False, f"Imported {len(normalized)} cookies, but still not showing as logged in — the session may be missing key cookies or already expired"


def fetch_url_for_chat(url: str) -> str:
    """Thread-safe wrapper api.py can call to fetch a URL through the live browser session."""
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
    print("[agent] browser started (usable for page reads immediately)")

    xb.try_login()  # non-fatal — logs and continues either way
    if xb.logged_in:
        print("[agent] X login successful, entering main loop")
        storage.log_activity("Logged into X successfully — scanning and posting are active")
    else:
        print("[agent] X login not active — scanning/posting to X paused, "
              "but chat and URL-reading still work. Will retry login periodically.")
        storage.log_activity("Not logged into X yet — scanning/posting paused, chat still works")

    last_scan = 0
    last_notification_check = 0
    last_original_post_check = 0
    last_login_retry = time.time()

    try:
        while True:
            now = time.time()

            if not xb.logged_in and now - last_login_retry > 10 * 60:  # retry every 10 min
                print("[agent] retrying X login")
                xb.try_login()
                last_login_retry = now

            if xb.logged_in and now - last_scan > config.SCAN_INTERVAL_SECONDS:
                try:
                    scan_and_draft(xb)
                except Exception as e:
                    print(f"[agent] scan error: {e}")
                last_scan = now

            if xb.logged_in and now - last_notification_check > config.NOTIFICATION_CHECK_INTERVAL_SECONDS:
                try:
                    check_notifications(xb)
                except Exception as e:
                    print(f"[agent] notification check error: {e}")
                last_notification_check = now

            if xb.logged_in and now - last_original_post_check > 6 * 60 * 60:  # check every 6 hours
                try:
                    maybe_post_original(xb)
                except Exception as e:
                    print(f"[agent] original post error: {e}")
                last_original_post_check = now

            # run any queued cross-thread browser tasks (e.g. chat asking to fetch a URL,
            # or a dashboard approval) right here, in this thread, since Playwright must
            # stay single-threaded. Checked frequently (every ~3s below) so these don't
            # sit waiting behind a full 30s scan cycle.
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
