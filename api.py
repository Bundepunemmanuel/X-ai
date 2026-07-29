"""
api.py — dashboard-facing HTTP endpoints. Talks to storage.py for data and
agent.py for browser-backed reads (URL fetch, search) and intent-link building.
Note: "approve" no longer posts anything server-side — it returns a tap-to-send
X link for the operator's phone to open and confirm.
"""

import re

from fastapi import APIRouter
from pydantic import BaseModel

import storage
import agent
import llm
import config

router = APIRouter()

URL_PATTERN = re.compile(r"https?://\S+")


# ─── Feed: pending drafts + approve/edit/skip ──────────────────────────────
@router.get("/api/feed")
def get_feed():
    drafts = storage.get_pending_drafts()
    counts = storage.get_today_counts()
    return {
        "drafts": drafts,
        "counts": counts,
        "active": storage.is_active(),
        "recent_activity": storage.get_recent_activity(10),
        "scan": storage.get_scan_stats(),
        "scan_interval_seconds": config.SCAN_INTERVAL_SECONDS,
    }


class ApproveRequest(BaseModel):
    edited_text: str | None = None


@router.post("/api/drafts/{draft_id}/approve")
def approve(draft_id: int, body: ApproveRequest):
    success, error, intent_url = agent.approve_draft(draft_id, edited_text=body.edited_text)
    return {"success": success, "error": error, "intent_url": intent_url}


@router.post("/api/drafts/{draft_id}/skip")
def skip(draft_id: int):
    agent.skip_draft(draft_id)
    return {"success": True}


@router.post("/api/drafts/approve-all")
def approve_all():
    """Approves every currently pending draft in one go, returning each one's
    intent URL (or the answer-saved result for knowledge questions) so the
    frontend can open them all for the operator to tap through."""
    pending = storage.get_pending_drafts()
    results = []
    for draft in pending:
        if draft["draft_type"] == "knowledge_question":
            continue  # these need a typed answer, can't be batch-approved blindly
        success, error, intent_url = agent.approve_draft(draft["id"])
        results.append({
            "id": draft["id"],
            "success": success,
            "error": error,
            "intent_url": intent_url,
            "handle": draft["author_handle"],
        })
    return {"results": results}


# ─── Chat panel ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str


@router.post("/api/chat")
def chat(body: ChatRequest):
    storage.add_chat_message("user", body.message)

    # direct, verifiable action: "like <url>" — returns a tap-to-like link,
    # bypassing the LLM entirely so there's no chance of a false claim.
    urls = URL_PATTERN.findall(body.message)
    if urls and re.search(r"\blike\b", body.message, re.I):
        intent_url = agent.like_intent_for_chat(urls[0])
        if intent_url:
            reply = f"Tap this to like it: {intent_url}"
            storage.log_activity(f"Provided like link via chat: {urls[0]}")
        else:
            reply = "Couldn't find a valid post ID in that link."
        storage.add_chat_message("assistant", reply)
        return {"reply": reply}

    activity = storage.get_recent_activity(15)
    activity_text = "\n".join(f"- {a['message']}" for a in activity) or "No activity yet."
    activity_text = (
        "Mode: read-only browsing (search, reading posts, chat) is fully automatic. "
        "Posting/replying/liking require opening a link on your phone and tapping "
        "X's own send button — no server-side login is used.\n\n" + activity_text
    )

    history = storage.get_chat_history(10)
    history_text = "\n".join(f"{h['role']}: {h['content']}" for h in history)

    page_content = ""
    if urls:
        page_content = agent.fetch_url_for_chat(urls[0])

    knowledge_base = storage.get_knowledge_base()

    turn = llm.chat_turn(body.message, activity_text, history_text, page_content, knowledge_base)

    if turn.get("new_kairo_fact"):
        storage.append_knowledge(turn["new_kairo_fact"])
        storage.log_activity(f"Learned something new about {llm.config.PRODUCT_NAME} from chat")

    if turn.get("reply"):
        reply = turn["reply"]
    elif turn.get("needs_search") and turn.get("query"):
        results = agent.search_web_for_chat(turn["query"])
        search_results_text = (
            "\n".join(f"- {r['title']}: {r['snippet']}" for r in results)
            if results else "(search ran but returned no usable results)"
        )
        reply = llm.chat_respond(body.message, activity_text, history_text, page_content, search_results_text, knowledge_base)
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
