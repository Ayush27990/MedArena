"""
Revision Handler
- Wrong Questions Bank
- Bookmarks
- Topic-wise revision quizzes
"""

import json
import uuid
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.database import (
    get_wrong_questions, get_bookmarks, get_mcqs_for_quiz,
    get_mcq_by_id, create_quiz_session, update_quiz_session,
    toggle_bookmark
)
from config import SUBJECTS, DEFAULT_QUESTION_TIME

logger = logging.getLogger(__name__)


async def revision_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("❌ Wrong Bank", callback_data="rev_wrong"),
            InlineKeyboardButton("🔖 Bookmarks", callback_data="rev_bookmarks"),
        ],
        [
            InlineKeyboardButton("📚 By Subject", callback_data="rev_by_subject"),
        ],
    ]
    text = (
        "🔄 *Smart Revision*\n\n"
        "Choose what to revise:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def wrong_bank_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    mcqs = await get_wrong_questions(user.id, limit=20)

    if not mcqs:
        msg = "🎉 Your wrong bank is empty — no mistakes recorded yet!\n\nStart a quiz to track your progress."
        if update.message:
            await update.message.reply_text(msg)
        return

    question_ids = [m["id"] for m in mcqs]
    session_id = f"rev_wrong_{user.id}_{uuid.uuid4().hex[:6]}"

    await create_quiz_session({
        "session_id": session_id,
        "chat_id": update.effective_chat.id,
        "created_by": user.id,
        "num_questions": len(question_ids),
        "time_per_q": DEFAULT_QUESTION_TIME,
        "question_ids": question_ids,
    })
    await update_quiz_session(session_id,
        status="active",
        participants=[user.id],
        scores=json.dumps({str(user.id): 0})
    )

    context.user_data["active_session"] = session_id
    context.user_data["q_index"] = 0
    context.user_data["session_qs"] = question_ids

    from handlers.quiz import _send_question
    msg_text = f"🔄 *Wrong Bank Revision*\n{len(question_ids)} questions you got wrong previously."
    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, parse_mode="Markdown")

    await _send_question(update, context, session_id, 0, question_ids)


async def bookmarks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    mcqs = await get_bookmarks(user.id)

    if not mcqs:
        msg = "🔖 You haven't bookmarked any questions yet.\n\nDuring a quiz, tap 🔖 Bookmark to save a question."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    lines = [f"🔖 *Your Bookmarks* ({len(mcqs)} questions)\n"]
    for i, m in enumerate(mcqs[:10]):
        q_short = m["question"][:60] + "..." if len(m["question"]) > 60 else m["question"]
        lines.append(f"{i+1}. {q_short}\n   📖 {m.get('subject') or '?'} | {m.get('difficulty') or '?'}")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ Quiz Bookmarks", callback_data="rev_quiz_bookmarks"),
    ]])

    text = "\n\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=kb
        )


async def revision_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user

    if data == "rev_menu":
        return await revision_handler(update, context)

    if data == "rev_wrong":
        return await wrong_bank_handler(update, context)

    if data == "rev_bookmarks":
        return await bookmarks_handler(update, context)

    if data == "rev_by_subject":
        kb = [[InlineKeyboardButton(s, callback_data=f"rev_sub_{s}")] for s in SUBJECTS[:10]]
        kb.append([InlineKeyboardButton("More ▶", callback_data="rev_sub_more")])
        await query.edit_message_text(
            "📚 Revise by Subject — choose:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "rev_sub_more":
        kb = [[InlineKeyboardButton(s, callback_data=f"rev_sub_{s}")] for s in SUBJECTS[10:]]
        kb.append([InlineKeyboardButton("◀ Back", callback_data="rev_by_subject")])
        await query.edit_message_text(
            "📚 Revise by Subject:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("rev_sub_"):
        subject = data[8:]
        mcqs = await get_mcqs_for_quiz(subject=subject, limit=20)
        if not mcqs:
            await query.edit_message_text(f"❌ No MCQs found for *{subject}*.", parse_mode="Markdown")
            return

        question_ids = [m["id"] for m in mcqs]
        session_id = f"rev_{user.id}_{uuid.uuid4().hex[:6]}"

        await create_quiz_session({
            "session_id": session_id,
            "chat_id": update.effective_chat.id,
            "created_by": user.id,
            "subject": subject,
            "num_questions": len(question_ids),
            "time_per_q": DEFAULT_QUESTION_TIME,
            "question_ids": question_ids,
        })
        await update_quiz_session(session_id,
            status="active",
            participants=[user.id],
            scores=json.dumps({str(user.id): 0})
        )

        context.user_data["active_session"] = session_id
        context.user_data["q_index"] = 0
        context.user_data["session_qs"] = question_ids

        await query.edit_message_text(
            f"📚 *{subject} Revision*\n{len(question_ids)} questions loaded.",
            parse_mode="Markdown"
        )

        from handlers.quiz import _send_question
        await _send_question(update, context, session_id, 0, question_ids)
        return

    if data == "rev_quiz_bookmarks":
        mcqs = await get_bookmarks(user.id)
        if not mcqs:
            await query.edit_message_text("No bookmarks yet.")
            return

        question_ids = [m["id"] for m in mcqs]
        session_id = f"rev_bk_{user.id}_{uuid.uuid4().hex[:6]}"

        await create_quiz_session({
            "session_id": session_id,
            "chat_id": update.effective_chat.id,
            "created_by": user.id,
            "num_questions": len(question_ids),
            "time_per_q": DEFAULT_QUESTION_TIME,
            "question_ids": question_ids,
        })
        await update_quiz_session(session_id,
            status="active",
            participants=[user.id],
            scores=json.dumps({str(user.id): 0})
        )

        context.user_data["active_session"] = session_id
        context.user_data["q_index"] = 0
        context.user_data["session_qs"] = question_ids

        await query.edit_message_text(
            f"🔖 *Bookmarks Quiz*\n{len(question_ids)} questions.",
            parse_mode="Markdown"
        )

        from handlers.quiz import _send_question
        await _send_question(update, context, session_id, 0, question_ids)
