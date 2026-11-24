"""
Send the bot a POLL, some TEXT or an IMAGE in a private chat (admins only)
and it writes a related MCQ with Gemini, then asks YOU which group to send it to
(same buttons as the auto drafts: send to a group, reassess, regenerate, skip).

  poll   -> new MCQ on the same concept as the poll
  text   -> a pasted MCQ, or any notes/topic (25+ characters) -> new MCQ based on it
  image  -> Gemini looks at the picture:
              medical picture  -> image-based MCQ (photo + quiz poll in the group)
              page of text     -> normal MCQ on the same concept
            Medical pictures are also saved in the image bank, so /auto and battle mode
            can write more questions from them later.

Messages from non-admins, from groups/channels, and PDFs still go through the old
importer (handlers/import_mcq.py). To switch these admin DMs back to the old
"import straight into the database" behaviour, set ADMIN_DM_MODE=import.
"""

import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import ADMIN_IDS
from services.gemini_service import generate_mcqs, generate_image_mcq, GeminiError
from services.database import add_bank_image
from handlers.generate import send_single_draft, download_file, pick_layout, NoTargetGroups
from handlers import import_mcq

logger = logging.getLogger(__name__)

ADMIN_DM_MODE = os.getenv("ADMIN_DM_MODE", "draft").strip().lower()
LEVEL = "NEET PG"
MIN_TEXT_CHARS = 25
SOURCE_TOPIC = "the concept tested in the source material below"


def _is_admin_dm(update: Update) -> bool:
    msg, user = update.message, update.effective_user
    return bool(
        ADMIN_DM_MODE != "import"
        and msg and user
        and user.id in ADMIN_IDS
        and msg.chat.type == "private"
    )


async def _send_draft(context, status, admin_id: int, mcq: dict, *, kind: str,
                      source: str | None = None, hint: str = "", avoid: list[str] | None = None,
                      topic: str = SOURCE_TOPIC, extra_line: str = "") -> None:
    header = f"📥 From your {kind} · {LEVEL}\n👇 Tap the group to send it to\n\n"
    try:
        await send_single_draft(
            context, admin_id, mcq, topic=topic, level=LEVEL, header=header,
            source=source, hint=hint, avoid=avoid)
    except NoTargetGroups:
        await status.edit_text("❌ No group is set up. Add TARGET_CHAT_IDS in the host's environment variables.")
        return
    except Exception:
        logger.exception("Could not send the draft")
        await status.edit_text("❌ Could not send the draft. Please try again.")
        return
    await status.edit_text("✅ Draft ready below." + (f"\n{extra_line}" if extra_line else ""))


async def _draft_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           source: str, kind: str, avoid: list[str]) -> None:
    msg = update.message
    status = await msg.reply_text(f"🤖 Reading your {kind} and writing a related MCQ…")
    try:
        mcqs, _ = await generate_mcqs(SOURCE_TOPIC, 1, level=LEVEL, avoid=avoid, source=source)
    except GeminiError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception:
        logger.exception("source draft crashed")
        await status.edit_text("❌ Something went wrong while generating. Please try again.")
        return
    await _send_draft(context, status, msg.chat_id, mcqs[0], kind=kind, source=source, avoid=avoid)


# ─── Poll ────────────────────────────────────────────────────

def _poll_to_source(poll) -> str:
    lines = [f"Question: {poll.question.strip()}"]
    for letter, opt in zip("ABCDE", poll.options):
        lines.append(f"{letter}. {opt.text.strip()}")
    if poll.correct_option_id is not None:
        lines.append(f"Correct option (marked in the poll): {'ABCDE'[poll.correct_option_id]}")
    if poll.explanation:
        lines.append(f"Explanation: {poll.explanation.strip()}")
    return "\n".join(lines)


async def source_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_dm(update) or not update.message.poll:
        return await import_mcq.handle_poll(update, context)
    poll = update.message.poll
    await _draft_from_text(update, context, _poll_to_source(poll), "poll", [poll.question.strip()])


# ─── Text ────────────────────────────────────────────────────

async def source_text_mcq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A pasted MCQ (starts with '1.', 'Q.', 'Question'...)."""
    if not _is_admin_dm(update):
        return await import_mcq.handle_text_mcq(update, context)
    if await import_mcq.handle_explanation_text(update, context):
        return
    text = update.message.text.strip()
    await _draft_from_text(update, context, text, "text", [text[:120]])


async def handle_source_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Any other admin DM text (notes / a topic). Returns True if it was used."""
    if not _is_admin_dm(update) or not update.message.text:
        return False
    text = update.message.text.strip()
    if text.startswith("/") or len(text) < MIN_TEXT_CHARS:
        return False
    await _draft_from_text(update, context, text, "text", [])
    return True


# ─── Image ───────────────────────────────────────────────────

async def source_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_dm(update) or not update.message.photo:
        return await import_mcq.handle_photo(update, context)

    msg, user = update.message, update.effective_user
    caption = (msg.caption or "").strip()
    photo = msg.photo[-1]
    status = await msg.reply_text("🖼 Looking at your image with Gemini…")

    try:
        image = await download_file(context.bot, photo.file_id)
        res = await generate_image_mcq(image, hint=caption, level=LEVEL)
    except GeminiError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception:
        logger.exception("image draft crashed")
        await status.edit_text("❌ Something went wrong while reading the image. Please try again.")
        return

    mcq = {k: res[k] for k in ("question", "options", "correct", "explanation", "subject", "topic")}
    extra = ""
    kind = "image"
    if res["uses_image"]:
        mcq["image_file_id"] = photo.file_id
        mcq["layout"] = pick_layout(mcq["question"])
        try:
            await add_bank_image(photo.file_id, photo.file_unique_id, res["subject"], res["topic"], user.id)
            extra = "💾 Image saved in the image bank (used by /auto and battle mode)."
        except Exception:
            logger.exception("Could not save the image in the bank")
    else:
        kind = "image (text page)"

    await _send_draft(context, status, msg.chat_id, mcq, kind=kind, hint=caption,
                      avoid=[], topic=res["topic"] or "image", extra_line=extra)
