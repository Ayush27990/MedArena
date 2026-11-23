"""
Automatic MCQ drafts (every 15 minutes by default).

Every AUTO_INTERVAL_MIN minutes the bot asks Gemini for ONE exam-level MCQ,
rotating through the subjects and alternating NEET PG / USMLE level, and sends
it to the admin's private chat with one button per group.

Nothing is posted anywhere until the admin taps a group button (or "All groups").
The MCQ is then posted as a quiz poll to the chosen group(s) and saved
(approved) in the database for future quizzes. "Skip" discards the draft and
saves nothing.

Admin command (private chat):
  /auto            status
  /auto on | off   resume / pause (until the bot restarts)
  /auto now        make one draft right now

Optional environment variables:
  AUTO_GENERATE      1 (default) or 0 - start switched on or off
  AUTO_INTERVAL_MIN  minutes between drafts (default 15)
  AUTO_MAX_PENDING   stop making new drafts while this many wait for you (default 3)
  AUTO_SUBJECTS      comma-separated subjects (default: all subjects in config.py)
  AUTO_LEVELS        comma-separated, alternated (default: NEET PG,USMLE)
  AUTO_ADMIN_ID      who receives the drafts (default: first id in ADMIN_IDS)
"""

import os
import time
import uuid
import random
import logging
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes
from config import ADMIN_IDS, SUBJECTS
from services.gemini_service import generate_mcqs, GeminiError
from services.database import search_mcqs
from handlers.generate import _drafts, _preview_text, _target_chats, _post_to_groups, _clear_buttons

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


AUTO_INTERVAL_MIN = _env_int("AUTO_INTERVAL_MIN", 15)
AUTO_MAX_PENDING = _env_int("AUTO_MAX_PENDING", 3)
AUTO_START_ON = os.getenv("AUTO_GENERATE", "1").strip().lower() not in ("0", "off", "false", "no")
DRAFT_TTL_SECONDS = 24 * 3600
FAILS_BEFORE_ALERT = 3


def _subjects() -> list[str]:
    raw = [s.strip() for s in os.getenv("AUTO_SUBJECTS", "").split(",") if s.strip()]
    return raw or [s for s in SUBJECTS if s != "Other"]


def _levels() -> list[str]:
    raw = [s.strip() for s in os.getenv("AUTO_LEVELS", "NEET PG,USMLE").split(",") if s.strip()]
    return raw or ["NEET PG"]


def _admin_id() -> int | None:
    raw = os.getenv("AUTO_ADMIN_ID", "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return ADMIN_IDS[0] if ADMIN_IDS else None


def _auto_buttons(batch: str, data: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, cid in enumerate(data["targets"]):
        rows.append([InlineKeyboardButton(f"✅ Send to {data['titles'].get(cid, cid)}"[:60],
                                          callback_data=f"agen|{batch}|{i}")])
    if len(data["targets"]) > 1:
        rows.append([InlineKeyboardButton("✅ Send to ALL groups", callback_data=f"agen|{batch}|all")])
    rows.append([InlineKeyboardButton("🗑 Skip", callback_data=f"gen_no|{batch}|0")])
    return InlineKeyboardMarkup(rows)


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("auto_state", {
        "enabled": AUTO_START_ON, "queue": [], "count": 0,
        "fails": 0, "recent": deque(maxlen=12), "titles": {},
    })


def _next_subject(state: dict) -> str:
    """Shuffled rotation: every subject is used once before any repeats."""
    if not state["queue"]:
        state["queue"] = _subjects()
        random.shuffle(state["queue"])
    return state["queue"].pop()


def _pending_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    return sum(
        1 for d in _drafts(context).values()
        if d.get("auto") and any(i["status"] is None for i in d["items"])
    )


def _prune(context: ContextTypes.DEFAULT_TYPE) -> None:
    drafts = _drafts(context)
    now = time.time()
    for batch in [b for b, d in drafts.items() if d.get("auto") and (
            now - d.get("created", now) > DRAFT_TTL_SECONDS
            or all(i["status"] is not None for i in d["items"]))]:
        drafts.pop(batch, None)


async def _recent_questions(state: dict, subject: str) -> list[str]:
    """Existing + recently drafted questions, so Gemini avoids repeating a concept."""
    seen = [q for q in state["recent"]]
    try:
        rows = await search_mcqs(subject=subject, limit=15)
        seen += [r["question"] for r in rows]
    except Exception:
        logger.exception("Could not load existing questions for the avoid list")
    return seen


async def _group_titles(context: ContextTypes.DEFAULT_TYPE, targets: list[int]) -> dict:
    cache = _state(context)["titles"]
    for cid in targets:
        if cid not in cache:
            try:
                cache[cid] = (await context.bot.get_chat(cid)).title or str(cid)
            except Exception:
                cache[cid] = str(cid)
    return {cid: cache[cid] for cid in targets}


async def make_auto_draft(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> str:
    """Create one draft and DM it to the admin. Returns a short status line."""
    admin_id = _admin_id()
    if not admin_id:
        return "ADMIN_IDS is not set."
    targets = _target_chats()
    if not targets:
        return "No group is set up. Add TARGET_CHAT_IDS in the host's environment variables."

    state = _state(context)
    _prune(context)
    if not force and _pending_count(context) >= AUTO_MAX_PENDING:
        return f"Paused: {AUTO_MAX_PENDING} draft(s) are waiting for your approval."

    subject = _next_subject(state)
    level = _levels()[state["count"] % len(_levels())]
    topic = f"{subject} (choose one high-yield topic from the standard syllabus; avoid the most obvious one)"

    try:
        mcqs, _ = await generate_mcqs(topic, 1, level=level,
                                      avoid=await _recent_questions(state, subject))
    except GeminiError as e:
        return await _record_failure(context, state, admin_id, str(e))
    except Exception as e:
        logger.exception("auto draft crashed")
        return await _record_failure(context, state, admin_id, type(e).__name__)

    state["fails"] = 0
    state["count"] += 1
    mcq = mcqs[0]
    state["recent"].append(mcq["question"])

    batch = uuid.uuid4().hex[:6]
    titles = await _group_titles(context, targets)
    data = {
        "admin": admin_id, "chat_id": admin_id, "topic": subject,
        "items": [{"mcq": mcq, "status": None, "msg_id": None}],
        "targets": targets, "titles": titles, "selected": set(),
        "auto": True, "created": time.time(),
    }
    _drafts(context)[batch] = data

    header = f"⏰ Auto draft · {subject} · {level}\n👇 Tap the group to send it to\n\n"
    try:
        sent = await context.bot.send_message(
            admin_id, header + _preview_text(0, 1, mcq), reply_markup=_auto_buttons(batch, data))
    except Exception as e:
        _drafts(context).pop(batch, None)
        logger.error(f"Could not DM the admin {admin_id}: {e}")
        return "Could not message the admin. Open the bot in Telegram and press Start first."
    data["items"][0]["msg_id"] = sent.message_id
    return "ok"


async def auto_send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped a group button on an auto draft: post there and save to the database."""
    q = update.callback_query
    if not q or not q.data:
        return
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Admins only.", show_alert=True)
        return
    parts = q.data.split("|")
    if len(parts) != 3:
        await q.answer()
        return
    _, batch, which = parts
    data = _drafts(context).get(batch)
    if not data:
        await q.answer("This draft expired (the bot restarted). Wait for the next one.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    item = data["items"][0]
    if item["status"]:
        await q.answer("Already handled.")
        return
    try:
        chats = list(data["targets"]) if which == "all" else [data["targets"][int(which)]]
    except (ValueError, IndexError):
        await q.answer()
        return

    await q.answer("Sending…")
    ok, failed = await _post_to_groups(context, item["mcq"], q.from_user.id, chats)
    if ok:
        item["status"] = "sent"
        names = ", ".join(str(data["titles"].get(c, c)) for c in chats if c not in failed)
        tail = f"\n\n✅ Sent to {names}"
        if failed:
            tail += f"\n⚠️ Could not post to: {', '.join(map(str, failed))} (is the bot an admin there?)"
        await _clear_buttons(context, data["chat_id"], item["msg_id"], _preview_text(0, 1, item["mcq"]) + tail)
    else:
        await context.bot.send_message(
            data["chat_id"], "⚠️ Could not post. Make sure the bot is in the group and allowed to post polls, then tap again.")


async def _record_failure(context, state: dict, admin_id: int, reason: str) -> str:
    state["fails"] += 1
    logger.warning(f"Auto draft failed ({state['fails']} in a row): {reason}")
    if state["fails"] == FAILS_BEFORE_ALERT:
        try:
            await context.bot.send_message(
                admin_id, f"⚠️ Auto drafts have failed {FAILS_BEFORE_ALERT} times in a row.\n"
                          f"Last error: {reason}\nI'll keep trying quietly.")
        except Exception:
            pass
    return f"Failed: {reason}"


async def auto_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _state(context)["enabled"]:
        return
    result = await make_auto_draft(context)
    if result != "ok":
        logger.info(f"Auto draft skipped: {result}")


async def auto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not msg or not user:
        return
    if user.id not in ADMIN_IDS:
        await msg.reply_text("⛔ Only admins can use /auto.")
        return
    if msg.chat.type != "private":
        await msg.reply_text("Please use /auto in a private chat with me.")
        return

    state = _state(context)
    arg = (context.args[0].lower() if context.args else "status")

    if arg == "on":
        state["enabled"] = True
        await msg.reply_text(f"✅ Auto drafts ON — one every {AUTO_INTERVAL_MIN} min.")
    elif arg == "off":
        state["enabled"] = False
        await msg.reply_text("⏸ Auto drafts paused (they resume if the bot restarts; "
                             "set AUTO_GENERATE=0 on the host to keep them off).")
    elif arg == "now":
        working = await msg.reply_text("🤖 Making a draft…")
        result = await make_auto_draft(context, force=True)
        await working.edit_text("✅ Draft sent above." if result == "ok" else f"❌ {result}")
    else:
        await msg.reply_text(
            f"⏰ Auto drafts: {'ON' if state['enabled'] else 'OFF'}\n"
            f"Every {AUTO_INTERVAL_MIN} min · {len(_subjects())} subjects · levels: {', '.join(_levels())}\n"
            f"Waiting for your approval: {_pending_count(context)}/{AUTO_MAX_PENDING}\n"
            f"Drafted since last restart: {state['count']}\n\n"
            "/auto on · /auto off · /auto now")


def schedule_auto(app: Application) -> None:
    if app.job_queue is None:
        logger.warning("JobQueue is missing (install python-telegram-bot[job-queue]); auto drafts disabled.")
        return
    app.job_queue.run_repeating(auto_job, interval=AUTO_INTERVAL_MIN * 60, first=60, name="auto_mcq")
    logger.info(f"Auto drafts scheduled every {AUTO_INTERVAL_MIN} min (start {'ON' if AUTO_START_ON else 'OFF'})")
j
