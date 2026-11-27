"""
Send the bot a POLL, some TEXT, an IMAGE or a PDF in a private chat (admins only).
It writes related MCQs with Gemini, then you choose which group(s) get them.

  poll   -> new MCQs on the same concept as the poll
  text   -> a pasted MCQ, or any notes/topic (25+ characters) -> new MCQs based on it
  image  -> Gemini reads the picture TOGETHER with its caption
  PDF    -> Gemini reads the whole file (typed, scanned or handwritten notes)
            and writes new MCQs; the PDF does not need to contain any MCQs

How many drafts (environment variables, optional):
  SOURCE_DRAFT_COUNT  poll / text / image  (default 4)
  PDF_DRAFT_COUNT     PDF                  (default 5)

Messages from non-admins and from groups/channels still go through the old
importer (handlers/import_mcq.py). To switch admin DMs back to the old
"import straight into the database" behaviour, set ADMIN_DM_MODE=import.
"""

import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import ADMIN_IDS
from services.gemini_service import generate_mcqs, generate_image_mcqs, GeminiError
from services.database import add_bank_image
from handlers.generate import send_drafts, download_file, pick_layout, NoTargetGroups
from handlers import import_mcq

logger = logging.getLogger(__name__)

ADMIN_DM_MODE = os.getenv("ADMIN_DM_MODE", "draft").strip().lower()
LEVEL = "NEET PG"
MIN_TEXT_CHARS = 25
SOURCE_TOPIC = "the concept tested in the source material below"
MAX_PDF_BYTES = 20 * 1024 * 1024      # Telegram does not let bots download bigger files


def _count(name: str, default: int) -> int:
    try:
        return min(10, max(1, int(os.getenv(name, "").strip() or default)))
    except ValueError:
        return default


SOURCE_DRAFT_COUNT = _count("SOURCE_DRAFT_COUNT", 4)
PDF_DRAFT_COUNT = _count("PDF_DRAFT_COUNT", 5)


def _is_admin_dm(update: Update) -> bool:
    msg, user = update.message, update.effective_user
    return bool(
        ADMIN_DM_MODE != "import"
        and msg and user
        and user.id in ADMIN_IDS
        and msg.chat.type == "private"
    )


async def _send_batch(context, status, admin_id: int, mcqs: list[dict], *, kind: str, wanted: int,
                      topic: str = SOURCE_TOPIC, extra_line: str = "", **kwargs) -> None:
    header = f"📥 From your {kind} · {LEVEL}\n\n"
    try:
        delivered = await send_drafts(
            context, admin_id, mcqs, topic=topic, level=LEVEL, header=header, **kwargs)
    except NoTargetGroups:
        await status.edit_text("❌ No group is set up. Add TARGET_CHAT_IDS in the host's environment variables.")
        return
    except Exception:
        logger.exception("Could not send the drafts")
        await status.edit_text("❌ Could not send the drafts. Please try again.")
        return
    text = f"✅ {delivered} draft(s) ready below."
    if delivered < wanted:
        text += f" (You asked for {wanted}; only {delivered} came out usable.)"
    await status.edit_text(text + (f"\n{extra_line}" if extra_line else ""))


async def _draft_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           source: str, kind: str, avoid: list[str]) -> None:
    msg = update.message
    status = await msg.reply_text(f"🤖 Reading your {kind} and writing {SOURCE_DRAFT_COUNT} MCQs…")
    try:
        mcqs, _ = await generate_mcqs(SOURCE_TOPIC, SOURCE_DRAFT_COUNT, level=LEVEL, avoid=avoid, source=source)
    except GeminiError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception:
        logger.exception("source draft crashed")
        await status.edit_text("❌ Something went wrong while generating. Please try again.")
        return
    await _send_batch(context, status, msg.chat_id, mcqs, kind=kind, wanted=SOURCE_DRAFT_COUNT,
                      source=source, avoid=avoid)


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
    status = await msg.reply_text(
        "🖼 Reading your image" + (" together with its caption" if caption else "") + " with Gemini…")

    try:
        image = await download_file(context.bot, photo.file_id)
        results = await generate_image_mcqs(image, SOURCE_DRAFT_COUNT, hint=caption, level=LEVEL)
    except GeminiError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception:
        logger.exception("image draft crashed")
        await status.edit_text("❌ Something went wrong while reading the image. Please try again.")
        return

    mcqs = []
    for res in results:
        mcq = {k: res[k] for k in ("question", "options", "correct", "explanation", "subject", "topic")}
        if res["uses_image"]:
            mcq["image_file_id"] = photo.file_id
            mcq["layout"] = pick_layout(mcq["question"])
        mcqs.append(mcq)

    extra = ""
    first_visual = next((r for r in results if r["uses_image"]), None)
    if first_visual:
        try:
            await add_bank_image(photo.file_id, photo.file_unique_id,
                                 first_visual["subject"], first_visual["topic"], user.id)
            extra = "💾 Image saved in the image bank (used by /auto and battle mode)."
        except Exception:
            logger.exception("Could not save the image in the bank")

    kind = ("image" if first_visual else "image (text page)") + (" + caption" if caption else "")
    await _send_batch(context, status, msg.chat_id, mcqs, kind=kind, wanted=SOURCE_DRAFT_COUNT,
                      hint=caption, avoid=[], topic=results[0]["topic"] or "image",
                      extra_line=extra, source_image_id=photo.file_id)


# ─── PDF ─────────────────────────────────────────────────────

async def source_pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin_dm(update) or not update.message.document:
        return await import_mcq.handle_document(update, context)

    msg = update.message
    doc = msg.document
    if doc.file_size and doc.file_size > MAX_PDF_BYTES:
        await msg.reply_text("❌ This PDF is over 20 MB, which Telegram does not let bots download. "
                             "Please split or compress it.")
        return
    caption = (msg.caption or "").strip()
    status = await msg.reply_text(
        f"📄 Reading your PDF and writing {PDF_DRAFT_COUNT} MCQs… this can take a minute or two.")
    try:
        pdf = await download_file(context.bot, doc.file_id)
        mcqs, _ = await generate_mcqs(
            "the most high-yield points taught in the attached PDF", PDF_DRAFT_COUNT,
            level=LEVEL, pdf=pdf, hint=caption)
    except GeminiError as e:
        await status.edit_text(f"❌ {e}")
        return
    except Exception:
        logger.exception("PDF draft crashed")
        await status.edit_text("❌ Something went wrong while reading the PDF. Please try again.")
        return
    await _send_batch(context, status, msg.chat_id, mcqs, kind="PDF", wanted=PDF_DRAFT_COUNT,
                      topic="the most high-yield points taught in the attached PDF",
                      hint=caption, pdf_file_id=doc.file_id)
