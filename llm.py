"""
llm.py — the only file that talks to Gemini.
Everything here is a plain function: give it context, get text or structured data back.
No Playwright, no database access — just prompts in, model output out.
"""

import json
import random
import re
import requests

import config

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

# ─── Voice presets (ported from reply.js) ──────────────────────────────────
VOICE_PRESETS = [
    {
        "name": "blunt-practical",
        "instruction": "Blunt and to the point. Short sentences. No cushioning or preamble.",
    },
    {
        "name": "casual-conversational",
        "instruction": "Casual, like texting a friend. Contractions everywhere. Fine to trail off. Slightly imperfect grammar is fine.",
    },
    {
        "name": "skeptical-but-helpful",
        "instruction": "A little skeptical of easy answers — open with mild pushback or 'tbh' before the actual help. Not cynical, just realistic.",
    },
    {
        "name": "warm-empathetic",
        "instruction": "Warm and validating, like someone who's been through the same thing. One personal-sounding detail, no corporate warmth.",
    },
    {
        "name": "one-liner",
        "instruction": "Just one short line. A quick reaction or a single short question — nothing more. Do not elaborate.",
    },
]

ANTI_FABRICATION_RULE = (
    "CRITICAL: Never invent specific numbers, names, timeframes, or anecdotes about other "
    "founders, customers, or results — you do not know these and must not make them up. "
    "Only reference what the product actually does, in general terms."
)

NO_DECEPTION_RULE = (
    "If the person directly asks whether you are a bot or an AI, do not deny it and do not "
    "dodge — this specific case should be flagged for a human to answer instead of auto-replying."
)


def _call_gemini(prompt: str, temperature: float = 0.8, max_tokens: int = 400) -> str:
    """Low-level Gemini call. Returns raw text, or '' on failure."""
    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={config.GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            },
            timeout=30,
        )
        data = resp.json()
        if "error" in data:
            print(f"[llm] gemini error: {data['error'].get('message')}")
            return ""
        candidates = data.get("candidates", [])
        if not candidates:
            print("[llm] gemini returned no candidates")
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        finish_reason = candidates[0].get("finishReason")
        if finish_reason == "MAX_TOKENS" and not text.strip():
            print("[llm] gemini truncated with no usable content")
            return ""
        return text.strip()
    except Exception as e:
        print(f"[llm] gemini request failed: {e}")
        return ""


def _parse_json(raw: str):
    """Strip markdown fences if present, then parse. Returns None on failure."""
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def pick_voice():
    return random.choice(VOICE_PRESETS)


def word_count(text: str) -> int:
    return len(text.strip().split())


# ─── Humanize pass (ported from reply.js, no model call) ───────────────────
def humanize_text(text: str) -> str:
    result = text
    first_letter = next((c for c in text if c.isalpha()), "")
    is_lowercase_style = first_letter.islower() if first_letter else False

    result = result.replace("\u2018", "'").replace("\u2019", "'")
    result = result.replace("\u201c", '"').replace("\u201d", '"')
    result = result.replace("\u2026", "...")
    result = result.replace("\u00a0", " ")
    result = re.sub(r"^[•●▪◦]\s*", "", result, flags=re.MULTILINE)

    # compound-word hyphen-likes -> plain hyphen
    result = re.sub(r"(\S)[\u2011\u2212](\S)", r"\1-\2", result)

    # true dashes -> split into sentences or comma, depending on context
    def dash_repl(m):
        next_char = m.group(1)
        return (". " if not is_lowercase_style else ". ") + next_char

    result = re.sub(r"\s*(?:—|–|‒|―|⸺|⸻|－|--)\s*(\S)", dash_repl, result)

    # semicolons -> split into sentences
    result = re.sub(r"\s*;\s*(\S)", r". \1", result)

    # arrow notation
    result = result.replace("→", ">")

    result = re.sub(r"\s+,", ",", result)
    result = re.sub(r"\.\s*\.", ".", result)
    result = re.sub(r"\s{2,}", " ", result).strip()
    return result


def too_clean_score(text: str, product_name: str = None) -> int:
    score = 0
    if ";" in text:
        score += 1
    if "→" in text:
        score += 1
    if re.search(r"\([^)]+,\s*[^)]+\)", text):
        score += 1
    if re.search(r"[—–‒―⸺⸻－]", text):
        score += 1

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    sentences = [s for s in sentences if s]
    if sentences and all(re.search(r"[.!?]$", s.strip()) for s in sentences) and len(sentences) > 1:
        score += 1

    if not re.search(r"\b(won't|don't|it's|can't|didn't|isn't|i'm|that's|there's|wasn't)\b", text, re.I):
        score += 1
    if not re.search(r"\b(lol|tbh|ngl|yeah|honestly|kinda|gonna|idk|anyway)\b", text, re.I):
        score += 1

    if product_name:
        escaped = re.escape(product_name)
        if re.search(rf"{escaped}[^.!?]*\b(so you|which means|meaning you|so now)\b", text, re.I):
            score += 3

    return score


def casualize_reply(original: str) -> str:
    prompt = f"""Rewrite this reply to sound like a real person typed it quickly on their phone, not composed carefully.

Original: "{original}"

Rules:
- Keep the same core point, just make it sound rougher/quicker
- No dashes, no semicolons, no arrow notation
- Contractions are good, a little rambling is fine
- Don't add new claims or facts

Write only the rewritten reply. Nothing else."""
    raw = _call_gemini(prompt, temperature=0.9, max_tokens=200)
    return raw if raw else original


def _post_process(reply: str, product_name: str = None) -> str:
    if not reply:
        return reply
    if too_clean_score(reply, product_name) >= 3:
        reply = casualize_reply(reply)
    return humanize_text(reply)


# ─── Classification: does this thread deserve a reply, and what kind? ──────
def classify_thread(post_text: str, existing_replies: str, commenter_page_content: str = "") -> dict:
    """
    Returns: {"action": "question" | "mention" | "skip", "reasoning": str, "pain_quote": str or None}
    """
    prompt = f"""You are deciding how (or whether) to reply to this X/Twitter thread, on behalf of
someone who builds tools for indie founders. Their product is {config.PRODUCT_NAME}: {config.PRODUCT_DESCRIPTION}

The core rule: HELP FIRST. Only suggest the product if the person has expressed a real pain point
it genuinely solves. Never force a mention into an unrelated thread. It's completely fine to decide
no reply is warranted at all.

Original post: "{post_text}"

Existing replies in the thread: "{existing_replies or 'none yet'}"

{"Commenter's own product page content: " + commenter_page_content[:800] if commenter_page_content else ""}

Decide one of three actions:
- "question": ask a genuine, curious clarifying question (e.g. "what problem does it solve?",
  "is it only web app?") — use this when there's no clear pain point yet to respond to
- "mention": the person has clearly expressed a pain point that {config.PRODUCT_NAME} solves —
  respond to their book response helpfully, and it's natural to mention {config.PRODUCT_NAME}
- "skip": this thread isn't a good fit at all (irrelevant, already well-covered, nothing to add)

Respond with ONLY a JSON object, no markdown, no explanation:
{{"action": "question" or "mention" or "skip", "reasoning": "one short sentence why", "pain_quote": "the exact phrase that reveals their pain, or null"}}"""

    raw = _call_gemini(prompt, temperature=0.4, max_tokens=250)
    parsed = _parse_json(raw)
    if not parsed or parsed.get("action") not in ("question", "mention", "skip"):
        return {"action": "skip", "reasoning": "classification failed", "pain_quote": None}
    return parsed


# ─── Drafting a reply ──────────────────────────────────────────────────────
def draft_reply(action: str, post_text: str, existing_replies: str, pain_quote: str = None) -> str:
    voice = pick_voice()

    length_variance_rule = (
        "Vary your length like a real person would: sometimes a genuinely short one-or-two-line "
        "reply (a quick reaction or a simple question) is the right call — don't default to a "
        "fuller multi-sentence response every time. Only go longer when there's actually more to say."
    )

    if action == "question":
        prompt = f"""Write a short, genuinely curious reply to this X post. You're not pitching
anything — just asking a real clarifying question, the way a curious builder would.

Original post: "{post_text}"
Existing replies already in the thread: "{existing_replies or 'none yet'}"

Voice for this reply: {voice['instruction']}

Rules:
- STRICT LENGTH LIMIT: 1-25 words. This is a genuine short question, not an essay.
- {length_variance_rule}
- Don't repeat a question already asked in the existing replies
- No hashtags, emojis, or bullet points
- Sound like a real curious person, not a marketer
- Do not mention {config.PRODUCT_NAME} at all in this reply

Write only the reply text. Nothing else."""
        reply = _call_gemini(prompt, temperature=0.85, max_tokens=100)
        return _post_process(reply)

    elif action == "mention":
        prompt = f"""Write a reply to this X thread. The person has expressed this pain point:
"{pain_quote}"

Original post: "{post_text}"
Existing replies: "{existing_replies or 'none yet'}"

Product: {config.PRODUCT_NAME} — {config.PRODUCT_DESCRIPTION}
Product URL: {config.PRODUCT_URL}

Voice for this reply: {voice['instruction']}

Rules:
- STRICT LENGTH LIMIT: 15-70 words. Hard cap, do not exceed.
- {length_variance_rule}
- {ANTI_FABRICATION_RULE}
- Respond to their specific pain point first — genuinely relate to it
- Mention {config.PRODUCT_NAME} once, naturally, only because it actually fits what they said
- You decide how to reference it: the full URL, "linked in my bio", or just the name if it's
  easy to find — whichever reads most natural for this specific reply. Don't always pick the same one.
- No hashtags, emojis, or bullet points
- Never start with: I, Hey, Great, Wow, As someone
- Do NOT structure this as setup → problem → solution → benefit. That clean arc reads as
  marketing copy even with casual wording. Pick ONE thing to say, don't lay out a full pitch.
- Avoid: "game changer", "streamline", "leverage", "at the end of the day", "it sounds like"
- Include one of: a trailing-off thought, a self-directed caveat about the product's own limits,
  or slightly inconsistent capitalization. Pick only one, don't force a tidy closing line.

Write only the reply text. Nothing else."""
        reply = _call_gemini(prompt, temperature=0.85, max_tokens=200)
        return _post_process(reply, config.PRODUCT_NAME)

    return ""


def draft_followup_reply(conversation_history: str, their_last_message: str) -> dict:
    """For when someone replies back to the assistant. Returns {"status": "open"/"closed", "reply": str}"""
    prompt = f"""You're continuing a real conversation on X, not starting one. Here's the exchange so far:

{conversation_history}

Their latest message: "{their_last_message}"

Product context: {config.PRODUCT_NAME} — {config.PRODUCT_DESCRIPTION}

{NO_DECEPTION_RULE}

Decide if this conversation should continue or naturally end, then respond with ONLY JSON, no markdown:
{{"status": "open" or "closed", "reply": "next reply text, or empty string if closed", "needs_human": true or false}}

If they're directly asking whether you're a bot/AI, set needs_human to true and leave reply empty —
a person should answer that one directly, not you.

If status is "open" and needs_human is false, the reply should:
- STRICT LENGTH LIMIT: 5-60 words
- {ANTI_FABRICATION_RULE}
- Directly respond to what they just said — reference specifics
- Not repeat any pitch from earlier in the conversation
- Sound like a real person continuing a conversation
- Respond to ONE thing they said, not a comprehensive follow-up"""

    raw = _call_gemini(prompt, temperature=0.75, max_tokens=250)
    parsed = _parse_json(raw)
    if not parsed:
        return {"status": "open", "reply": "", "needs_human": True}

    reply = parsed.get("reply", "") or ""
    if reply and not parsed.get("needs_human"):
        reply = _post_process(reply, config.PRODUCT_NAME)

    return {
        "status": parsed.get("status", "open"),
        "reply": reply,
        "needs_human": bool(parsed.get("needs_human")),
    }


def draft_original_post(recent_topics: str = "") -> str:
    prompt = f"""Write a single original X post for a solo founder's account, building in public.
Product: {config.PRODUCT_NAME} — {config.PRODUCT_DESCRIPTION}

{"Recently posted about: " + recent_topics if recent_topics else ""}

Rules:
- STRICT LENGTH LIMIT: 10-40 words
- {ANTI_FABRICATION_RULE}
- Sound like a genuine build-in-public update, thought, or observation — not an ad
- Don't force a mention of {config.PRODUCT_NAME} if a genuine observation doesn't need it
- No hashtags

Write only the post text. Nothing else."""
    reply = _call_gemini(prompt, temperature=0.85, max_tokens=100)
    return _post_process(reply, config.PRODUCT_NAME)


# ─── Chat panel: answer questions about the assistant's own activity ───────
def chat_respond(user_message: str, activity_context: str, chat_history: str = "") -> str:
    prompt = f"""You are the X reply assistant itself, talking directly to your operator in a
dashboard chat panel. Be direct and concise, like a capable assistant giving a status update or
taking an instruction — not like a generic chatbot.

Your recent activity:
{activity_context}

Recent conversation:
{chat_history}

Operator's message: "{user_message}"

If they're asking about your activity, answer using the activity log above — don't make anything up.
If they're giving you an instruction (e.g. change pacing, avoid a topic, prioritize something),
acknowledge it clearly and specifically.

Respond in 1-4 sentences. No markdown formatting, just plain text."""
    return _call_gemini(prompt, temperature=0.6, max_tokens=200) or "Sorry, I couldn't process that — try again?"
