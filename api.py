"""
api.py — all dashboard-facing HTTP endpoints, mounted onto the FastAPI app in main.py.
Talks to storage.py for data and agent.py for actions that need the live browser.
"""

import re

from fastapi import APIRouter
from pydantic import BaseModel

import storage
import agent
import llm

router = APIRouter()

URL_PATTERN = re.compile(r"https?://\S+")


# ─── Feed: pending drafts + approve/edit/skip ──────────────────────────────
@router.get("/api/feed")
def get_feed():
    drafts = storage.get_pending_drafts()
    counts = storage.get_today_counts()
    xb = agent.get_browser()
    return {
        "drafts": drafts,
        "counts": counts,
        "active": storage.is_active(),
        "x_logged_in": bool(xb and xb.logged_in),
        "recent_activity": storage.get_recent_activity(10),
    }


class ApproveRequest(BaseModel):
    edited_text: str | None = None


@router.post("/api/drafts/{draft_id}/approve")
def approve(draft_id: int, body: ApproveRequest):
    xb = agent.get_browser()
    if xb is None:
        return {"success": False, "error": "browser not ready yet, try again shortly"}
    success, error = agent.approve_draft(xb, draft_id, edited_text=body.edited_text)
    return {"success": success, "error": error}


@router.post("/api/drafts/{draft_id}/skip")
def skip(draft_id: int):
    agent.skip_draft(draft_id)
    return {"success": True}


# ─── Chat panel ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str


@router.post("/api/chat")
def chat(body: ChatRequest):
    storage.add_chat_message("user", body.message)

    # direct, verifiable action: "like <url>" — bypasses the LLM entirely for the
    # confirmation, since this is exactly the kind of "prove it's real" action that
    # should never be answered by a guess. It shows up on the actual X account.
    urls = URL_PATTERN.findall(body.message)
    if urls and re.search(r"\blike\b", body.message, re.I):
        success, error = agent.like_url_for_chat(urls[0])
        if success:
            reply = f"Done — liked {urls[0]}. Check your X account's Likes tab to confirm."
            storage.log_activity(f"Liked a post via chat command: {urls[0]}")
        else:
            reply = f"Couldn't like that post: {error}"
            storage.log_activity(f"Failed to like {urls[0]} via chat: {error}")
        storage.add_chat_message("assistant", reply)
        return {"reply": reply}

    activity = storage.get_recent_activity(15)
    activity_text = "\n".join(f"- {a['message']}" for a in activity) or "No activity yet."

    xb = agent.get_browser()
    login_status = "Currently logged into X and active." if (xb and xb.logged_in) else "Not currently logged into X — scanning/posting is paused, only chat and URL-reading work."
    activity_text = f"X login status: {login_status}\n\n{activity_text}"

    history = storage.get_chat_history(10)
    history_text = "\n".join(f"{h['role']}: {h['content']}" for h in history)

    # if the message contains a URL, actually fetch it now (synchronously) rather
    # than letting the model claim it's "scanning in the background" — it has no
    # background task system, so that would be a hallucinated capability.
    page_content = ""
    urls = URL_PATTERN.findall(body.message)
    if urls:
        page_content = agent.fetch_url_for_chat(urls[0])

    # single combined call: decides if search is needed AND writes the reply if not,
    # keeping the common case down to 1 Gemini call instead of 2.
    turn = llm.chat_turn(body.message, activity_text, history_text, page_content)

    if turn.get("reply"):
        reply = turn["reply"]
    elif turn.get("needs_search") and turn.get("query"):
        results = agent.search_web_for_chat(turn["query"])
        search_results_text = (
            "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)
            if results else "(search ran but returned no usable results)"
        )
        reply = llm.chat_respond(body.message, activity_text, history_text, page_content, search_results_text)
    else:
        reply = "Sorry, I couldn't process that — try again?"
    storage.add_chat_message("assistant", reply)
    return {"reply": reply}


@router.get("/api/chat/history")
def chat_history():
    return {"messages": storage.get_chat_history(50)}


# ─── Settings ────────────────────────────────────────────────────────────────
class ActiveRequest(BaseModel):
    active: bool


@router.post("/api/settings/active")
def set_active(body: ActiveRequest):
    storage.set_setting("active", "1" if body.active else "0")
    return {"active": body.active}


@router.get("/api/settings/style-trust")
def style_trust():
    return {"styles": storage.get_style_trust()}
