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
    success = agent.approve_draft(xb, draft_id, edited_text=body.edited_text)
    return {"success": success}


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

    activity = storage.get_recent_activity(15)
    activity_text = "\n".join(f"- {a['message']}" for a in activity) or "No activity yet."

    history = storage.get_chat_history(10)
    history_text = "\n".join(f"{h['role']}: {h['content']}" for h in history)

    # if the message contains a URL, actually fetch it now (synchronously) rather
    # than letting the model claim it's "scanning in the background" — it has no
    # background task system, so that would be a hallucinated capability.
    page_content = ""
    urls = URL_PATTERN.findall(body.message)
    if urls:
        page_content = agent.fetch_url_for_chat(urls[0])

    reply = llm.chat_respond(body.message, activity_text, history_text, page_content)
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
