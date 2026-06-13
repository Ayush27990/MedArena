"""
MCQ Import Handler
Handles the 3-step format from channels/groups:
  Step 1: Text message with full question + options
  Step 2: Poll with correct answer
  Step 3: Text message with explanation

Also handles: forwarded polls, PDFs, images
"""

import hashlib
import logging
import base64
import io
import re
from telegram import Update, Message
from telegram.ext import ContextTypes
from config import ADMIN_IDS, SOURCE_CHAT_IDS
from services.database import insert_mcq, update_mcq_explanation_by_hash
from services.ai_service import (
    categorize_mcq, parse_text_mcq, parse_pdf_text, ocr_image_to_mcqs
)

logger = logging.getLogger(__name__)

# In-memory stores per chat_id
_pending_question: dict = {}     # chat_id -> {question, options} from text step
_pending_explanation: dict = {}  # chat_id -> {hash} waiting for explanation text


def _make_hash(question: str, option_a: str) -> str:
    raw = (question.strip().lower() + option_a.strip().lower())
    return hashlib.md5(raw.encode()).hexdigest()


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _is_monitored_chat(chat_id: int) -> bool:
    return not SOURCE_CHAT_IDS or chat_id in SOURCE_CHAT_IDS


def _parse_options_from_text(text: str):
    """
    Extract question and options from text like:
    Full question text here
    A. Option one
    B. Option two
    C. Option three
    D. Option four
    """
    lines = text.strip().split('\n')
    options = []
    question_lines = []
    option_pattern = re.compile(r'^[A-Ea-e][.)]\s*(.+)', re.IGNORECASE)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = option_pattern.match(line)
        if match:
            options.append(match.group(1).strip())
        else:
            if not options:  # Only add to question if we haven't started options yet
                question_lines.append(line)

    question = ' '.join(question_lines).strip()
    return question, options


async def _process_mcq_dict(mcq: dict, source_type: str, chat_id: int,
                             imported_by: int, auto_approve: bool) -> str | None:
    """Categorize + insert a single MCQ. Returns hash if inserted, None otherwise."""
    q = mcq.get("question", "").strip()
    opts = mcq.get("options", [])

    if not q or len(opts) < 2:
        return None

    while len(opts) < 4:
        opts.append("")

    option_a = opts[0]
    option_b = opts[1]
    option_c = opts[2] if len(opts) > 2 else ""
    option_d = opts[3] if len(opts) > 3 else ""
    option_e = opts[4] if len(opts) > 4 else None

    correct_raw = mcq.get("correct")
    if correct_raw is not None:
        correct = str(correct_raw).upper()[:1]
        if correct not in "ABCDE":
            correct = "A"
    else:
        correct = "A"

    explanation = mcq.get("explanation") or ""

    # AI for subject/topic/difficulty ONLY — never touches answer or explanation
    cat = await categorize_mcq(q, opts)
    if not explanation and cat.get("explanation"):
        explanation = cat["explanation"]

    mcq_hash = _make_hash(q, option_a)

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
        "hash": mcq_hash,
    }

    inserted_id = await insert_mcq(data)
    return mcq_hash if inserted_id is not None else None


# ─── Step 1: Text message with question + options ────────────────────

async def handle_channel_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Captures text from monitored channels/groups that looks like an MCQ.
    Saves it as pending, waiting for the poll to provide the correct answer.
    """
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    if not _is_monitored_chat(chat_id):
        return

    text = msg.text.strip()
    question, options = _parse_options_from_text(text)

    if question and len(options) >= 2:
        _pending_question[chat_id] = {
            "question": question,
            "options": options,
            "full_text": text,
        }
        logger.info(f"Saved pending question from chat {chat_id}: {question[:50]}")


# ─── Step 2: Poll with correct answer ────────────────────────────────

async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    When a poll arrives:
    - If there's a pending text question for this chat, use that question + options
      but take correct answer from the poll
    - Otherwise fall back to using poll question directly
    """
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

    correct_idx = poll.correct_option_id
    correct_letter = chr(65 + correct_idx) if correct_idx is not None else None
    poll_explanation = poll.explanation or ""

    imported_by = msg.from_user.id if msg.from_user else 0
    auto_approve = _is_admin(imported_by)

    # Check if we have a pending text question for this chat
    pending = _pending_question.pop(chat_id, None)

    if pending and pending.get("question") and len(pending.get("options", [])) >= 2:
        # Use the full question from text, correct answer from poll
        question = pending["question"]
        options = pending["options"]
        logger.info(f"Matched poll correct answer {correct_letter} to text question: {question[:50]}")
    else:
        # Fall back to poll's own question
        question = poll.question.strip()
        options = [o.text.strip() for o in poll.options]
        logger.info(f"Using poll question directly: {question[:50]}")

    if not question or len(options) < 2:
        return

    mcq = {
        "question": question,
        "options": options,
        "correct": correct_letter if correct_letter is not None else "A",
        "explanation": poll_explanation,
    }

    mcq_hash = await _process_mcq_dict(mcq, "poll", chat_id, imported_by, auto_approve)

    if mcq_hash:
        # Store hash waiting for explanation text
        _pending_explanation[chat_id] = {"hash": mcq_hash}
        logger.info(f"MCQ stored, waiting for explanation. Hash: {mcq_hash}")

    if is_private:
        if mcq_hash:
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


# ─── Step 3: Explanation text ─────────────────────────────────────────

async def handle_explanation_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    If the previous message was a poll, treat this text as the explanation.
    Returns True if handled.
    """
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return False

    chat_id = msg.chat_id
    pending = _pending_explanation.get(chat_id)
    if not pending:
        return False

    text = msg.text.strip()

    # Skip if it looks like a new MCQ question
    if len(text) < 10:
        return False

    # Skip if it looks like option lines (new MCQ incoming)
    option_pattern = re.compile(r'^[A-Ea-e][.)]\s*.+', re.IGNORECASE | re.MULTILINE)
    if len(option_pattern.findall(text)) >= 3:
        return False

    try:
        await update_mcq_explanation_by_hash(pending["hash"], text)
        _pending_explanation.pop(chat_id, None)
        logger.info(f"Updated explanation for hash {pending['hash']}")
        return True
    except Exception as e:
        logger.error(f"Failed to update explanation: {e}")
        return False


# ─── Text MCQ Handler (private chat / admin use) ─────────────────────

async def handle_text_mcq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse text MCQs sent directly to the bot in private chat."""
    msg = update.message
    if not msg or not msg.text:
        return

    imported_by = msg.from_user.id if msg.from_user else 0

    # Only process in private chat or from admins
    if msg.chat.type != "private" and not _is_admin(imported_by):
        return

    # Check if this is an explanation for a previous poll
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
        mcq_hash = await _process_mcq_dict(mcq, "text", msg.chat_id, imported_by, auto_approve)
        if mcq_hash:
            count += 1

    status = "✅ approved" if auto_approve else "pending review"
    await msg.reply_text(
        f"📚 Imported *{count}* MCQ(s) ({status})\n"
        f"{'⚠️ Some were duplicates and skipped.' if count < len(mcqs) else ''}",
        parse_mode="Markdown"
    )


# ─── PDF Handler ─────────────────────────────────────────────────────

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
            mcq_hash = await _process_mcq_dict(mcq, "pdf", msg.chat_id, imported_by, auto_approve)
            if mcq_hash:
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


# ─── Image/OCR Handler ───────────────────────────────────────────────

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
            mcq_hash = await _process_mcq_dict(mcq, "image", msg.chat_id, imported_by, auto_approve)
            if mcq_hash:
                count += 1

        status = "✅ approved" if auto_approve else "pending review"
        await processing_msg.edit_text(
            f"📸 Extracted *{count}* MCQ(s) from image ({status})",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Image OCR error: {e}")
        await processing_msg.edit_text("❌ Error processing image.")
