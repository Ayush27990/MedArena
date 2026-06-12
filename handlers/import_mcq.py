"""
MCQ Import Handler
Handles: Telegram polls, forwarded polls, text MCQs, PDFs, images
"""

import hashlib
import logging
import base64
import io
from telegram import Update, Message
from telegram.ext import ContextTypes
from config import ADMIN_IDS, SOURCE_CHAT_IDS
from services.database import insert_mcq, upsert_user
from services.ai_service import (
    categorize_mcq, parse_text_mcq, parse_pdf_text, ocr_image_to_mcqs
)

logger = logging.getLogger(__name__)

# In-memory store: chat_id -> {question, options, correct} waiting for explanation
_pending_explanation: dict = {}


def _make_hash(question: str, option_a: str) -> str:
    """Dedup hash from question + first option."""
    raw = (question.strip().lower() + option_a.strip().lower())
    return hashlib.md5(raw.encode()).hexdigest()


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _is_monitored_chat(chat_id: int) -> bool:
    return not SOURCE_CHAT_IDS or chat_id in SOURCE_CHAT_IDS


async def _process_mcq_dict(mcq: dict, source_type: str, chat_id: int,
                             imported_by: int, auto_approve: bool) -> bool:
    """Categorize + insert a single MCQ. Returns True if inserted."""
    q = mcq.get("question", "").strip()
    opts = mcq.get("options", [])

    if not q or len(opts) < 4:
        return False

    # Pad options to 4
    while len(opts) < 4:
        opts.append("")

    option_a, option_b, option_c, option_d = opts[0], opts[1], opts[2], opts[3]
    option_e = opts[4] if len(opts) > 4 else None

    correct_raw = mcq.get("correct") or "A"
    correct = str(correct_raw).upper()[:1]
    if correct not in "ABCDE":
        correct = "A"

    # Use provided explanation as-is — DO NOT let AI override it
    explanation = mcq.get("explanation") or ""

    # AI categorization ONLY for subject/topic/difficulty — NOT for answer or explanation
    cat = await categorize_mcq(q, opts)

    # Only fix question text if there were clear typos, never change the answer
    # Do NOT use cat["question_fixed"] — it can corrupt the question
    # Do NOT use cat["explanation"] if we already have one
    if not explanation and cat.get("explanation"):
        explanation = cat["explanation"]

    data = {
        "question": q,
        "option_a": option_a,
        "option_b": option_b,
        "option_c": option_c,
        "option_d": option_d,
        "option_e": option_e,
        "correct": correct,
        "explanation": explanation,
        "subject": cat["subject"],
        "topic": cat["topic"],
        "difficulty": cat["difficulty"],
        "source_type": source_type,
        "source_chat": chat_id,
        "imported_by": imported_by,
        "approved": auto_approve,
        "hash": _make_hash(q, option_a),
    }

    inserted_id = await insert_mcq(data)
    return inserted_id is not None


# ─── Poll Handler ────────────────────────────────────────────────────

async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    chat_id = msg.chat_id
    is_private = msg.chat.type == "private"

    if not is_private and not _is_monitored_chat(chat_id):
        return

    poll = msg.poll
    if not poll:
        return

    question = poll.question.strip()
    options = [o.text.strip() for o in poll.options]
    correct_idx = poll.correct_option_id
    explanation = poll.explanation or ""

    if not question or len(options) < 2:
        return

    correct_letter = chr(65 + correct_idx) if correct_idx is not None else None

    imported_by = msg.from_user.id if msg.from_user else 0
    auto_approve = _is_admin(imported_by)

    mcq = {
        "question": question,
        "options": options,
        "correct": correct_letter or "A",
        "explanation": explanation,
    }

    # Store in pending so next text message can update explanation
    _pending_explanation[chat_id] = {
        "question": question,
        "option_a": options[0] if options else "",
        "hash": _make_hash(question, options[0] if options else ""),
    }

    ok = await _process_mcq_dict(mcq, "poll", chat_id, imported_by, auto_approve)

    if is_private:
        if ok:
            status = "✅ Added & approved" if auto_approve else "📥 Added — pending admin review"
            correct_display = f"Correct: *{correct_letter}*" if correct_letter else "⚠️ No correct answer marked"
            await msg.reply_text(
                f"{status}\n\n"
                f"📌 *{question[:80]}*\n"
                f"{correct_display}\n\n"
                f"💡 Send the explanation as your next message to update it.",
                parse_mode="Markdown"
            )
        else:
            await msg.reply_text(
                f"⚠️ *Duplicate* — this question already exists.\n\n"
                f"📌 _{question[:60]}_",
                parse_mode="Markdown"
            )


# ─── Explanation capture Handler ─────────────────────────────────────

async def handle_explanation_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from handle_text_mcq. If the previous message was a poll,
    treat this text as the explanation for it. Returns True if handled.
    """
    msg = update.message
    if not msg or not msg.text:
        return False

    chat_id = msg.chat_id
    pending = _pending_explanation.get(chat_id)
    if not pending:
        return False

    text = msg.text.strip()
    # If text looks like an explanation (not a new MCQ)
    if text.startswith(("1.", "Q.", "Question")) or len(text) < 10:
        return False

    # Update explanation in database by hash
    try:
        from services.database import update_mcq_explanation_by_hash
        await update_mcq_explanation_by_hash(pending["hash"], text)
        _pending_explanation.pop(chat_id, None)
        logger.info(f"Updated explanation for question hash {pending['hash']}")
        return True
    except Exception as e:
        logger.error(f"Failed to update explanation: {e}")
        return False


# ─── Text MCQ Handler ────────────────────────────────────────────────

async def handle_text_mcq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse text messages that look like MCQs, or capture explanation."""
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    if not _is_monitored_chat(chat_id) and msg.chat.type != "private":
        return

    imported_by = msg.from_user.id if msg.from_user else 0

    if msg.chat.type != "private" and not _is_admin(imported_by):
        # Try to capture as explanation for previous poll
        await handle_explanation_text(update, context)
        return

    # First check if this is an explanation for a previous poll
    if await handle_explanation_text(update, context):
        return

    text = msg.text.strip()
    await msg.reply_text("🔍 Parsing MCQ(s) from text...")

    mcqs = await parse_text_mcq(text)
    if not mcqs:
        await msg.reply_text("❌ Could not extract any MCQs from this text.")
        return

    auto_approve = _is_admin(imported_by)
    count = 0
    for mcq in mcqs:
        ok = await _process_mcq_dict(mcq, "text", chat_id, imported_by, auto_approve)
        if ok:
            count += 1

    status = "✅ approved" if auto_approve else "pending review"
    await msg.reply_text(
        f"📚 Imported *{count}* MCQ(s) ({status})\n"
        f"{'⚠️ Some were duplicates and skipped.' if count < len(mcqs) else ''}",
        parse_mode="Markdown"
    )


# ─── PDF Handler ────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.document:
        return

    imported_by = msg.from_user.id if msg.from_user else 0
    if not _is_admin(imported_by) and msg.chat.type != "private":
        return

    doc = msg.document
    if not doc.file_name.lower().endswith(".pdf"):
        return

    processing_msg = await msg.reply_text("📄 Processing PDF... this may take a moment.")

    try:
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = await file.download_as_bytearray()

        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(bytes(pdf_bytes)))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            await processing_msg.edit_text("❌ PDF library not installed.")
            return

        if not text.strip():
            await processing_msg.edit_text("❌ Could not extract text from this PDF.")
            return

        mcqs = await parse_pdf_text(text)
        if not mcqs:
            await processing_msg.edit_text("❌ No MCQs found in this PDF.")
            return

        auto_approve = _is_admin(imported_by)
        count = 0
        for mcq in mcqs:
            ok = await _process_mcq_dict(mcq, "pdf", msg.chat_id, imported_by, auto_approve)
            if ok:
                count += 1

        status = "✅ approved" if auto_approve else "pending review"
        await processing_msg.edit_text(
            f"📚 Extracted *{count}* MCQ(s) from PDF ({status})\n"
            f"Total found: {len(mcqs)} | Duplicates skipped: {len(mcqs) - count}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"PDF processing error: {e}")
        await processing_msg.edit_text("❌ Error processing PDF.")


# ─── Image/OCR Handler ────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return

    imported_by = msg.from_user.id if msg.from_user else 0
    if not _is_admin(imported_by) and msg.chat.type != "private":
        return

    processing_msg = await msg.reply_text("🔎 Running OCR on image...")

    try:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        img_b64 = base64.b64encode(bytes(img_bytes)).decode()

        mcqs = await ocr_image_to_mcqs(img_b64)
        if not mcqs:
            await processing_msg.edit_text("❌ No MCQs detected in this image.")
            return

        auto_approve = _is_admin(imported_by)
        count = 0
        for mcq in mcqs:
            ok = await _process_mcq_dict(mcq, "image", msg.chat_id, imported_by, auto_approve)
            if ok:
                count += 1

        status = "✅ approved" if auto_approve else "pending review"
        await processing_msg.edit_text(
            f"📸 Extracted *{count}* MCQ(s) from image ({status})",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Image OCR error: {e}")
        await processing_msg.edit_text("❌ Error processing image.")
