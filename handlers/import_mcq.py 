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

    explanation = mcq.get("explanation")

    # AI categorization
    cat = await categorize_mcq(q, opts)
    if cat.get("question_fixed"):
        q = cat["question_fixed"]
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
    """Handle Telegram quiz polls (native polls with type=quiz)."""
    msg = update.message or update.channel_post
    if not msg:
        return

    chat_id = msg.chat_id
    if not _is_monitored_chat(chat_id):
        return

    poll = msg.poll
    if not poll:
        return

    question = poll.question
    options = [o.text for o in poll.options]
    correct_idx = poll.correct_option_id  # may be None for regular polls
    explanation = poll.explanation

    if len(options) < 4:
        return

    correct_letter = chr(65 + correct_idx) if correct_idx is not None else "A"

    imported_by = msg.from_user.user_id if msg.from_user else 0
    auto_approve = _is_admin(imported_by)

    mcq = {
        "question": question,
        "options": options,
        "correct": correct_letter,
        "explanation": explanation,
    }

    ok = await _process_mcq_dict(mcq, "poll", chat_id, imported_by, auto_approve)
    if ok and msg.chat.type == "private":
        status = "✅ Added & approved" if auto_approve else "📥 Added — pending admin review"
        await msg.reply_text(f"{status}\n\n📌 *{question[:60]}*", parse_mode="Markdown")


# ─── Text MCQ Handler ────────────────────────────────────────────────

async def handle_text_mcq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse text messages that look like MCQs."""
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    if not _is_monitored_chat(chat_id) and msg.chat.type != "private":
        return

    text = msg.text.strip()
    imported_by = msg.from_user.id if msg.from_user else 0

    # Only auto-import in private or if from admin
    if msg.chat.type != "private" and not _is_admin(imported_by):
        return

    await msg.reply_text("🔍 Parsing MCQ(s) from text...")

    mcqs = await parse_text_mcq(text)
    if not mcqs:
        await msg.reply_text("❌ Could not extract any MCQs from this text. Check the format.")
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
    """Extract MCQs from PDF files."""
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

        # Extract text from PDF using pypdf
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(bytes(pdf_bytes)))
            text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        except ImportError:
            await processing_msg.edit_text("❌ PDF library not installed. Run: pip install pypdf")
            return

        if not text.strip():
            await processing_msg.edit_text("❌ Could not extract text from this PDF (may be scanned).")
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
        await processing_msg.edit_text("❌ Error processing PDF. Please try again.")


# ─── Image/OCR Handler ────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OCR image to extract MCQs."""
    msg = update.message
    if not msg or not msg.photo:
        return

    imported_by = msg.from_user.id if msg.from_user else 0
    if not _is_admin(imported_by) and msg.chat.type != "private":
        return

    processing_msg = await msg.reply_text("🔎 Running OCR on image...")

    try:
        # Get highest resolution photo
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        img_b64 = base64.b64encode(bytes(img_bytes)).decode()

        mcqs = await ocr_image_to_mcqs(img_b64)
        if not mcqs:
            await processing_msg.edit_text(
                "❌ No MCQs detected in this image.\n"
                "Tip: Make sure the image is clear and the text is readable."
            )
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
