"""
Automatic MCQ drafts.

Every AUTO_INTERVAL_MIN minutes the bot writes a batch of AUTO_DRAFT_COUNT MCQs
(one subject per batch, rotating; alternating NEET PG / USMLE) and sends them to
the admin's private chat. Nothing is posted until you tap a Send button.
If Gemini fails (for example its daily limit is used up), Groq writes the batch.

Some batches (AUTO_IMAGE_PERCENT) use a picture from the image bank.

Admin command (private chat):
  /auto              status (also shows how long the bot has been running)
  /auto on | off     resume / pause
  /auto every 30     change the interval (minutes)
  /auto now          make a batch right now
  /auto image        make an IMAGE batch right now
  /auto clear        discard all waiting auto drafts

/auto changes last until the bot restarts. For permanent settings use the
environment variables:
  AUTO_GENERATE      1 (default) or 0 - start switched on or off
  AUTO_INTERVAL_MIN  minutes between batches (default 15)
  AUTO_DRAFT_COUNT   questions per batch (default 4)
  AUTO_MAX_PENDING   stop while this many BATCHES wait for you (default 6)
  AUTO_IMAGE_PERCENT chance (0-100) that a batch is image-based (default 30)
  AUTO_SUBJECTS      comma-separated subjects (default: all subjects in config.py)
  AUTO_LEVELS        comma-separated, alternated (default: NEET PG,USMLE)
  AUTO_ADMIN_ID      who receives the drafts (default: first id in ADMIN_IDS)
"""

import os
import time
import random
import logging
from collections import deque
from telegram import Update
from telegram.ext import Application, ContextTypes
from config import ADMIN_IDS, SUBJECTS
from services.gemini_service import generate_mcqs, generate_image_mcqs, GeminiError
from services.database import search_mcqs, pick_bank_images, bump_bank_image, count_bank_images
from handlers.generate import (
    _drafts, _target_chats, _waiting, prune_drafts,
    send_drafts, download_file, pick_layout, NoTargetGroups,
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


AUTO_INTERVAL_MIN = _env_int("AUTO_INTERVAL_MIN", 15)
AUTO_DRAFT_COUNT = min(10, _env_int("AUTO_DRAFT_COUNT", 4))
AUTO_MAX_PENDING = _env_int("AUTO_MAX_PENDING", 6)
STARTED_AT = time.time()
try:
    AUTO_IMAGE_PERCENT = min(100, max(0, int(os.getenv("AUTO_IMAGE_PERCENT", "").strip() or 30)))
except ValueError:
    AUTO_IMAGE_PERCENT = 30
AUTO_START_ON = os.getenv("AUTO_GENERATE", "1").strip().lower() not in ("0", "off", "false", "no")
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
        "fails": 0, "recent": deque(maxlen=24), "interval": AUTO_INTERVAL_MIN,
        "told_full": False, "last_ok": None, "last_result": "no run yet", "running": False,
    })


def _next_subject(state: dict) -> str:
    """Shuffled rotation: every subject is used once before any repeats."""
    if not state["queue"]:
        state["queue"] = _subjects()
        random.shuffle(state["queue"])
    return state["queue"].pop()


def _pending_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Auto batches that still have a draft waiting."""
    return sum(1 for d in _drafts(context).values() if d.get("auto") and _waiting(d))


async def _recent_questions(state: dict, subject: str) -> list[str]:
    """Existing + recently drafted questions, so Gemini avoids repeating a concept."""
    seen = [q for q in state["recent"]]
    try:
        rows = await search_mcqs(subject=subject, limit=15)
        seen += [r["question"] for r in rows]
    except Exception:
        logger.exception("Could not load existing questions for the avoid list")
    return seen


async def _image_batch(state: dict, bot, level: str, provider: str = "gemini"):
    """Try to build IMAGE questions from one bank image. Returns (mcqs, label, image_id) or None."""
    rows = await pick_bank_images(1)
    if not rows:
        return None
    row = rows[0]
    try:
        image = await download_file(bot, row["file_id"])
    except Exception:
        logger.exception("Could not download image-bank image %s", row["id"])
        return None
    results = await generate_image_mcqs(
        image, AUTO_DRAFT_COUNT, level=level, avoid=list(state["recent"]), provider=provider)
    await bump_bank_image(row["id"])

    mcqs = []
    for res in results:
        mcq = {k: res[k] for k in ("question", "options", "correct", "explanation", "subject", "topic")}
        if res["uses_image"]:
            mcq["image_file_id"] = row["file_id"]
            mcq["layout"] = pick_layout(mcq["question"])
        mcqs.append(mcq)
    label = "🖼 Image · " + (results[0]["subject"] or row["subject"] or "Mixed")
    return mcqs, label, row["file_id"]


async def make_auto_draft(context: ContextTypes.DEFAULT_TYPE, force: bool = False,
                          want_image: bool | None = None) -> str:
    """Create one batch and DM it to the admin. Returns "ok" or a short status line.
    want_image: True = must try an image batch, None = random by AUTO_IMAGE_PERCENT."""
    state = _state(context)
    if state["running"]:
        return "A batch is already being made."
    state["running"] = True
    try:
        result = await _make_batch(context, state, force, want_image)
    finally:
        state["running"] = False
    state["last_result"] = result
    if result == "ok":
        state["last_ok"] = time.time()
    return result


async def _make_batch(context, state: dict, force: bool, want_image: bool | None) -> str:
    admin_id = _admin_id()
    if not admin_id:
        return "ADMIN_IDS is not set."
    if not _target_chats():
        return "No group is set up. Add TARGET_CHAT_IDS in the host's environment variables."

    prune_drafts(context)
    if not force and _pending_count(context) >= AUTO_MAX_PENDING:
        if not state["told_full"]:
            try:
                await context.bot.send_message(
                    admin_id, f"⏸ Auto drafts are waiting: {AUTO_MAX_PENDING} batches need your decision.\n"
                              "Send or skip them, or use /auto clear. New batches then start again.")
                state["told_full"] = True
            except Exception:
                logger.exception("Could not tell the admin the queue is full")
        return f"Waiting: {AUTO_MAX_PENDING} batch(es) need your approval."
    state["told_full"] = False

    level = _levels()[state["count"] % len(_levels())]
    if want_image is None:
        want_image = random.randint(1, 100) <= AUTO_IMAGE_PERCENT

    mcqs, label, image_id, avoid, topic, used = None, None, None, [], None, ""

    if want_image:
        try:
            made = await _image_batch(state, context.bot, level)
            if made:
                mcqs, label, image_id = made
                topic = mcqs[0].get("subject") or "Image"
            elif force:
                return "The image bank is empty. Send me some images first (any photo you send me is saved)."
        except GeminiError as e:
            logger.warning(f"Auto image batch failed, using text questions instead: {e}")
        except Exception:
            logger.exception("auto image batch crashed; using text questions instead")

    if mcqs is None:
        subject = _next_subject(state)
        label = subject
        topic = f"{subject} (choose high-yield topics from the standard syllabus; avoid the most obvious ones)"
        avoid = await _recent_questions(state, subject)
        errors = []
        for provider in ("gemini", "other"):
            try:
                mcqs, _ = await generate_mcqs(topic, AUTO_DRAFT_COUNT, level=level, avoid=avoid, provider=provider)
                if provider == "other":
                    used = f"✍️ Written by Groq (Gemini failed: {errors[0][:120]})\n"
                break
            except GeminiError as e:
                errors.append(str(e))
            except Exception as e:
                logger.exception("auto draft crashed")
                errors.append(type(e).__name__)
        if mcqs is None:
            return await _record_failure(context, state, admin_id, " | ".join(errors))
        for m in mcqs:
            m["subject"] = subject

    header = f"⏰ Auto draft · {label} · {level}\n{used}\n"
    try:
        await send_drafts(
            context, admin_id, mcqs, topic=topic, level=level, header=header,
            avoid=avoid or list(state["recent"]), auto=True, source_image_id=image_id)
    except NoTargetGroups:
        return "No group is set up. Add TARGET_CHAT_IDS in the host's environment variables."
    except Exception as e:
        logger.error(f"Could not DM the admin {admin_id}: {e}")
        return "Could not message the admin. Open the bot in Telegram and press Start first."
    state["fails"] = 0
    state["count"] += 1
    state["recent"].extend(m["question"] for m in mcqs)
    return "ok"


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
        await msg.reply_text(f"✅ Auto drafts ON: {AUTO_DRAFT_COUNT} questions every {state['interval']} min.")
    elif arg == "off":
        state["enabled"] = False
        await msg.reply_text("⏸ Auto drafts paused (they resume if the bot restarts; "
                             "set AUTO_GENERATE=0 on the host to keep them off).")
    elif arg == "every":
        value = context.args[1] if len(context.args) > 1 else ""
        if not value.isdigit() or not 5 <= int(value) <= 1440:
            await msg.reply_text("Use it like this: /auto every 30  (5 to 1440 minutes)")
        elif context.job_queue is None:
            await msg.reply_text("❌ The scheduler is not installed on the host.")
        else:
            state["interval"], state["enabled"] = int(value), True
            _schedule(context.job_queue, int(value), int(value) * 60)
            await msg.reply_text(f"✅ Auto drafts every {value} min (until restart; "
                                 f"set AUTO_INTERVAL_MIN={value} on the host to keep it).")
    elif arg == "clear":
        drafts = _drafts(context)
        old = [b for b, d in drafts.items() if d.get("auto")]
        for batch in old:
            drafts.pop(batch)
        state["told_full"] = False
        await msg.reply_text(f"🧹 Discarded {len(old)} auto batch(es).")
    elif arg in ("now", "image"):
        working = await msg.reply_text("🤖 Making drafts…")
        result = await make_auto_draft(context, force=True, want_image=True if arg == "image" else None)
        await working.edit_text("✅ Drafts sent above." if result == "ok" else f"❌ {result}")
    else:
        try:
            bank = await count_bank_images()
        except Exception:
            bank = "?"
        if context.job_queue is None:
            next_line = "⚠️ Scheduler is NOT installed (check requirements.txt)"
        else:
            jobs = context.job_queue.get_jobs_by_name("auto_mcq")
            if jobs and jobs[0].next_t:
                mins = max(0, int((jobs[0].next_t.timestamp() - time.time()) // 60))
                next_line = f"Next run in about {mins} min"
            else:
                next_line = "⚠️ No run is scheduled"
        last = "never" if not state["last_ok"] else f"{int((time.time() - state['last_ok']) // 60)} min ago"
        await msg.reply_text(
            f"⏰ Auto drafts: {'ON' if state['enabled'] else 'OFF'}\n"
            f"{AUTO_DRAFT_COUNT} questions every {state['interval']} min · {next_line}\n"
            f"🟢 Bot running for {int((time.time() - STARTED_AT) // 60)} min\n"
            f"Last successful batch: {last}\n"
            f"Last result: {state['last_result']}\n"
            f"Waiting for approval: {_pending_count(context)}/{AUTO_MAX_PENDING} batches\n"
            f"Batches since restart: {state['count']}\n"
            f"🖼 Image batches: {AUTO_IMAGE_PERCENT}% · images in bank: {bank}\n\n"
            "/auto on · off · every 30 · now · image · clear")


def _schedule(job_queue, minutes: int, first: int) -> None:
    for job in job_queue.get_jobs_by_name("auto_mcq"):
        job.schedule_removal()
    # By default a job that starts even 1 second late is silently skipped; allow 5 minutes.
    job_queue.run_repeating(
        auto_job, interval=minutes * 60, first=first, name="auto_mcq",
        job_kwargs={"misfire_grace_time": 300, "coalesce": True})


def schedule_auto(app: Application) -> None:
    if app.job_queue is None:
        logger.warning("JobQueue is missing (install python-telegram-bot[job-queue]); auto drafts disabled.")
        return
    _schedule(app.job_queue, AUTO_INTERVAL_MIN, 60)
    logger.info(f"Auto drafts scheduled every {AUTO_INTERVAL_MIN} min (start {'ON' if AUTO_START_ON else 'OFF'})")
