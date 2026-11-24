"""
Automatic MCQ drafts (every 15 minutes by default).

Every AUTO_INTERVAL_MIN minutes the bot asks Gemini for ONE exam-level MCQ,
rotating through the subjects and alternating NEET PG / USMLE level, and sends
it to the admin's private chat with one button per group.

Some drafts (AUTO_IMAGE_PERCENT, default 30%) are IMAGE questions: the bot takes a real
image from the image bank (every image you send the bot is saved there), Gemini looks at
it and writes the question, and the group gets it as photo + quiz poll.
Every draft also has Reassess / Regenerate buttons (Gemini or Groq).

Nothing is posted anywhere until the admin taps a group button (or "All groups").
The MCQ is then posted as a quiz poll to the chosen group(s) and saved
(approved) in the database for future quizzes. "Skip" discards the draft and
saves nothing.

Admin command (private chat):
  /auto            status
  /auto on | off   resume / pause (until the bot restarts)
  /auto now        make one draft right now
  /auto image      make one IMAGE draft right now

Optional environment variables:
  AUTO_GENERATE      1 (default) or 0 - start switched on or off
  AUTO_INTERVAL_MIN  minutes between drafts (default 15)
  AUTO_MAX_PENDING   stop making new drafts while this many wait for you (default 3)
  AUTO_IMAGE_PERCENT chance (0-100) that a draft is an image question (default 30)
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
from telegram import Update
from telegram.ext import Application, ContextTypes
from config import ADMIN_IDS, SUBJECTS
from services.gemini_service import generate_mcqs, generate_image_mcq, GeminiError
from services.database import search_mcqs, pick_bank_images, bump_bank_image, count_bank_images
from handlers.generate import (
    _drafts, _preview_text, _target_chats, _post_to_groups, _clear_buttons,
    send_single_draft, download_file, pick_layout, NoTargetGroups,
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


AUTO_INTERVAL_MIN = _env_int("AUTO_INTERVAL_MIN", 15)
AUTO_MAX_PENDING = _env_int("AUTO_MAX_PENDING", 3)
try:
    AUTO_IMAGE_PERCENT = min(100, max(0, int(os.getenv("AUTO_IMAGE_PERCENT", "").strip() or 30)))
except ValueError:
    AUTO_IMAGE_PERCENT = 30
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
    for batch in [b for b, d in drafts.items() if d.get("flow") == "single" and (
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


async def _image_draft(state: dict, bot, level: str):
    """Try to build an IMAGE question from the image bank. Returns (mcq, label) or None."""
    rows = await pick_bank_images(1)
    if not rows:
        return None
    row = rows[0]
    try:
        image = await download_file(bot, row["file_id"])
    except Exception:
        logger.exception("Could not download image-bank image %s", row["id"])
        return None
    res = await generate_image_mcq(image, level=level, avoid=list(state["recent"]))
    await bump_bank_image(row["id"])

    mcq = {k: res[k] for k in ("question", "options", "correct", "explanation", "subject", "topic")}
    if res["uses_image"]:
        mcq["image_file_id"] = row["file_id"]
        mcq["layout"] = pick_layout(mcq["question"])
    label = ("🖼 Image · " if res["uses_image"] else "") + (res["subject"] or row["subject"] or "Mixed")
    return mcq, label


async def make_auto_draft(context: ContextTypes.DEFAULT_TYPE, force: bool = False,
                          want_image: bool | None = None) -> str:
    """Create one draft and DM it to the admin. Returns a short status line.
    want_image: True = must try an image question, None = random by AUTO_IMAGE_PERCENT."""
    admin_id = _admin_id()
    if not admin_id:
        return "ADMIN_IDS is not set."
    if not _target_chats():
        return "No group is set up. Add TARGET_CHAT_IDS in the host's environment variables."

    state = _state(context)
    _prune(context)
    if not force and _pending_count(context) >= AUTO_MAX_PENDING:
        return f"Paused: {AUTO_MAX_PENDING} draft(s) are waiting for your approval."

    level = _levels()[state["count"] % len(_levels())]
    if want_image is None:
        want_image = random.randint(1, 100) <= AUTO_IMAGE_PERCENT

    mcq, label, subject, avoid = None, None, None, []

    if want_image:
        try:
            made = await _image_draft(state, context.bot, level)
            if made:
                mcq, label = made
                subject = mcq.get("subject") or "Image"
            elif force:
                return "The image bank is empty. Send me some images first (any photo you send me is saved)."
        except GeminiError as e:
            logger.warning(f"Auto image draft failed, using a text question instead: {e}")
            if force:
                return await _record_failure(context, state, admin_id, str(e))
        except Exception:
            logger.exception("auto image draft crashed; using a text question instead")

    if mcq is None:
        subject = _next_subject(state)
        label = subject
        topic = f"{subject} (choose one high-yield topic from the standard syllabus; avoid the most obvious one)"
        avoid = await _recent_questions(state, subject)
        try:
            mcqs, _ = await generate_mcqs(topic, 1, level=level, avoid=avoid)
        except GeminiError as e:
            return await _record_failure(context, state, admin_id, str(e))
        except Exception as e:
            logger.exception("auto draft crashed")
            return await _record_failure(context, state, admin_id, type(e).__name__)
        mcq = mcqs[0]
        mcq["subject"] = subject
        draft_topic = topic
    else:
        draft_topic = subject

    state["fails"] = 0
    state["count"] += 1
    state["recent"].append(mcq["question"])

    header = f"⏰ Auto draft · {label} · {level}\n👇 Tap the group to send it to\n\n"
    try:
        await send_single_draft(
            context, admin_id, mcq, topic=draft_topic, level=level, header=header,
            avoid=avoid or list(state["recent"]), auto=True)
    except NoTargetGroups:
        return "No group is set up. Add TARGET_CHAT_IDS in the host's environment variables."
    except Exception as e:
        logger.error(f"Could not DM the admin {admin_id}: {e}")
        return "Could not message the admin. Open the bot in Telegram and press Start first."
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
    elif arg in ("now", "image"):
        working = await msg.reply_text("🤖 Making a draft…")
        result = await make_auto_draft(context, force=True, want_image=True if arg == "image" else None)
        await working.edit_text("✅ Draft sent above." if result == "ok" else f"❌ {result}")
    else:
        try:
            bank = await count_bank_images()
        except Exception:
            bank = "?"
        await msg.reply_text(
            f"⏰ Auto drafts: {'ON' if state['enabled'] else 'OFF'}\n"
            f"Every {AUTO_INTERVAL_MIN} min · {len(_subjects())} subjects · levels: {', '.join(_levels())}\n"
            f"Waiting for your approval: {_pending_count(context)}/{AUTO_MAX_PENDING}\n"
            f"Drafted since last restart: {state['count']}\n"
            f"🖼 Image questions: {AUTO_IMAGE_PERCENT}% of drafts · images in bank: {bank}\n\n"
            "/auto on · /auto off · /auto now · /auto image")


def schedule_auto(app: Application) -> None:
    if app.job_queue is None:
        logger.warning("JobQueue is missing (install python-telegram-bot[job-queue]); auto drafts disabled.")
        return
    app.job_queue.run_repeating(auto_job, interval=AUTO_INTERVAL_MIN * 60, first=60, name="auto_mcq")
    logger.info(f"Auto drafts scheduled every {AUTO_INTERVAL_MIN} min (start {'ON' if AUTO_START_ON else 'OFF'})")
