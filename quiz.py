"""
Group Quiz Handler
Supports: multi-user group quizzes with timer and leaderboard
"""

import uuid
import asyncio
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.database import (
    get_mcqs_for_quiz, create_quiz_session, get_quiz_session,
    update_quiz_session, record_answer, add_xp, get_mcq_by_id
)
from config import (
    SUBJECTS, DIFFICULTIES, DEFAULT_QUESTION_TIME,
    DEFAULT_QUESTIONS_PER_QUIZ, XP_CORRECT, XP_WRONG, XP_SPEED_BONUS,
    BATTLE_BONUS_SPEED_SECONDS
)

logger = logging.getLogger(__name__)

# In-memory state for active question timers
_active_timers: dict = {}
_question_start_times: dict = {}


def _format_question(mcq, q_num: int, total: int, time_per_q: int) -> tuple[str, InlineKeyboardMarkup]:
    letters = ["A", "B", "C", "D", "E"]
    opts = [mcq["option_a"], mcq["option_b"], mcq["option_c"], mcq["option_d"]]
    if mcq.get("option_e"):
        opts.append(mcq["option_e"])

    text = (
        f"❓ *Question {q_num}/{total}*\n"
        f"⏱ {time_per_q}s | 📖 {mcq.get('subject','') or ''}\n\n"
        f"{mcq['question']}\n\n"
    )
    for i, opt in enumerate(opts):
        text += f"*{letters[i]}\.* {opt}\n"

    keyboard = []
    row = []
    for i, letter in enumerate(letters[:len(opts)]):
        row.append(InlineKeyboardButton(
            letter, callback_data=f"quiz_ans_{letter}_{mcq['id']}"
        ))
        if len(row) == 2 or i == len(opts) - 1:
            keyboard.append(row)
            row = []

    keyboard.append([
        InlineKeyboardButton("🔖 Bookmark", callback_data=f"quiz_bookmark_{mcq['id']}")
    ])

    return text, InlineKeyboardMarkup(keyboard)


# ─── Menu ────────────────────────────────────────────────────────────

async def quiz_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📚 By Subject", callback_data="quiz_filter_subject"),
            InlineKeyboardButton("🎯 By Difficulty", callback_data="quiz_filter_diff"),
        ],
        [
            InlineKeyboardButton("🎲 Random Quiz", callback_data="quiz_random"),
        ],
    ]
    text = (
        "🧠 *Quiz Mode*\n\n"
        "Choose how you want to practice:"
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


async def start_group_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to start a group quiz session."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    keyboard = [
        [InlineKeyboardButton(sub, callback_data=f"quiz_gs_{sub}")]
        for sub in SUBJECTS[:8]
    ]
    keyboard.append([InlineKeyboardButton("🎲 All Subjects", callback_data="quiz_gs_ALL")])

    await update.message.reply_text(
        "🏥 *Group Quiz Setup*\n\nSelect a subject:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["setup_chat"] = chat_id
    context.user_data["setup_user"] = user.id


async def join_quiz_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /joingame SESSION_ID")
        return

    session_id = args[0]
    session = await get_quiz_session(session_id)
    if not session:
        await update.message.reply_text("❌ Session not found.")
        return
    if session["status"] != "waiting":
        await update.message.reply_text("❌ This quiz has already started or ended.")
        return

    user = update.effective_user
    participants = list(session["participants"] or [])
    if user.id in participants:
        await update.message.reply_text("You've already joined this quiz!")
        return

    participants.append(user.id)
    await update_quiz_session(session_id, participants=participants)
    await update.message.reply_text(
        f"✅ Joined quiz `{session_id}`\n"
        f"👥 Players: {len(participants)}\n\n"
        "Waiting for admin to start...",
        parse_mode="Markdown"
    )


# ─── Quiz Callbacks ──────────────────────────────────────────────────

async def quiz_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user

    # Menu navigation
    if data == "quiz_menu":
        return await quiz_menu_handler(update, context)

    if data == "quiz_random":
        await _run_solo_quiz(update, context, user.id, subject=None, difficulty=None)
        return

    if data == "quiz_filter_subject":
        kb = [[InlineKeyboardButton(s, callback_data=f"quiz_sub_{s}")] for s in SUBJECTS[:10]]
        kb.append([InlineKeyboardButton("More ▶", callback_data="quiz_sub_more")])
        await query.edit_message_text(
            "📚 Select Subject:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "quiz_sub_more":
        kb = [[InlineKeyboardButton(s, callback_data=f"quiz_sub_{s}")] for s in SUBJECTS[10:]]
        kb.append([InlineKeyboardButton("◀ Back", callback_data="quiz_filter_subject")])
        await query.edit_message_text(
            "📚 Select Subject:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("quiz_sub_"):
        subject = data[9:]
        context.user_data["quiz_subject"] = subject
        kb = [[InlineKeyboardButton(d, callback_data=f"quiz_diff_{d}")] for d in DIFFICULTIES]
        kb.append([InlineKeyboardButton("Any Difficulty", callback_data="quiz_diff_ANY")])
        await query.edit_message_text(
            f"📚 Subject: *{subject}*\n\nSelect difficulty:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "quiz_filter_diff":
        kb = [[InlineKeyboardButton(d, callback_data=f"quiz_diff_{d}")] for d in DIFFICULTIES]
        await query.edit_message_text(
            "🎯 Select Difficulty:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("quiz_diff_"):
        diff = data[10:]
        diff = None if diff == "ANY" else diff
        subject = context.user_data.get("quiz_subject")
        await query.edit_message_text("⏳ Loading questions...")
        await _run_solo_quiz(update, context, user.id, subject=subject, difficulty=diff)
        return

    # Answer submission
    if data.startswith("quiz_ans_"):
        await _handle_answer(update, context)
        return

    if data.startswith("quiz_next_"):
        await _send_next_question(update, context)
        return

    if data.startswith("quiz_bookmark_"):
        mcq_id = int(data.split("_")[-1])
        from services.database import toggle_bookmark
        added = await toggle_bookmark(user.id, mcq_id)
        await query.answer("🔖 Bookmarked!" if added else "🗑 Bookmark removed")
        return

    # Group session setup
    if data.startswith("quiz_gs_"):
        subject = data[8:]
        if subject == "ALL":
            subject = None
        context.user_data["gs_subject"] = subject
        kb = [[InlineKeyboardButton(str(n), callback_data=f"quiz_gn_{n}")]
              for n in [5, 10, 15, 20, 30]]
        await query.edit_message_text(
            f"Subject: *{subject or 'All'}*\n\nHow many questions?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("quiz_gn_"):
        n = int(data[8:])
        context.user_data["gs_num"] = n
        kb = [[InlineKeyboardButton(str(t) + "s", callback_data=f"quiz_gt_{t}")]
              for t in [15, 20, 30, 45, 60]]
        await query.edit_message_text(
            f"Questions: *{n}*\n\nSeconds per question?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("quiz_gt_"):
        t = int(data[8:])
        await _launch_group_quiz(update, context, t)
        return

    if data.startswith("quiz_start_"):
        session_id = data[11:]
        await _begin_quiz(update, context, session_id)
        return


# ─── Solo Quiz Flow ──────────────────────────────────────────────────

async def _run_solo_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         user_id: int, subject=None, difficulty=None,
                         num_q=DEFAULT_QUESTIONS_PER_QUIZ):
    mcqs = await get_mcqs_for_quiz(subject=subject, difficulty=difficulty, limit=num_q)
    if not mcqs:
        msg = "❌ No MCQs found for these filters. Try different settings or add more MCQs."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    session_id = f"solo_{user_id}_{uuid.uuid4().hex[:6]}"
    question_ids = [m["id"] for m in mcqs]

    await create_quiz_session({
        "session_id": session_id,
        "chat_id": update.effective_chat.id,
        "created_by": user_id,
        "subject": subject,
        "difficulty": difficulty,
        "num_questions": len(question_ids),
        "time_per_q": DEFAULT_QUESTION_TIME,
        "question_ids": question_ids,
    })
    await update_quiz_session(session_id,
        status="active",
        participants=[user_id],
        current_q=0,
        scores=json.dumps({str(user_id): 0})
    )
    context.user_data["active_session"] = session_id
    context.user_data["q_index"] = 0
    context.user_data["session_qs"] = question_ids

    await _send_question(update, context, session_id, 0, question_ids)


async def _send_question(update, context, session_id, q_index, question_ids):
    mcq_id = question_ids[q_index]
    mcq = await get_mcq_by_id(mcq_id)
    if not mcq:
        return

    _question_start_times[f"{session_id}_{q_index}"] = datetime.utcnow().timestamp()

    total = len(question_ids)
    session = await get_quiz_session(session_id)
    time_per_q = session["time_per_q"] if session else DEFAULT_QUESTION_TIME

    text, keyboard = _format_question(mcq, q_index + 1, total, time_per_q)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="MarkdownV2", reply_markup=keyboard
        )
    else:
        await update.effective_chat.send_message(
            text, parse_mode="MarkdownV2", reply_markup=keyboard
        )


async def _handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    parts = query.data.split("_")
    # quiz_ans_LETTER_MCQID
    chosen = parts[2]
    mcq_id = int(parts[3])

    mcq = await get_mcq_by_id(mcq_id)
    if not mcq:
        await query.answer("❌ Question not found.")
        return

    session_id = context.user_data.get("active_session")
    q_index = context.user_data.get("q_index", 0)
    question_ids = context.user_data.get("session_qs", [])

    # Calculate time taken
    start_key = f"{session_id}_{q_index}"
    start_time = _question_start_times.get(start_key, datetime.utcnow().timestamp())
    time_taken = datetime.utcnow().timestamp() - start_time

    is_correct = chosen == mcq["correct"]
    xp_gain = 0

    if is_correct:
        xp_gain = XP_CORRECT
        if time_taken <= BATTLE_BONUS_SPEED_SECONDS:
            xp_gain += XP_SPEED_BONUS
    else:
        xp_gain = XP_WRONG

    await record_answer(user.id, mcq_id, chosen, is_correct,
                        time_taken, session_id or "solo")
    await add_xp(user.id, xp_gain)

    # Build answer feedback
    letters = ["A", "B", "C", "D", "E"]
    opts = [mcq["option_a"], mcq["option_b"], mcq["option_c"], mcq["option_d"]]
    if mcq.get("option_e"):
        opts.append(mcq["option_e"])

    result_icon = "✅" if is_correct else "❌"
    correct_text = opts[letters.index(mcq["correct"])]
    explanation = mcq.get("explanation") or "_No explanation available_"

    feedback = (
        f"{result_icon} *{'Correct!' if is_correct else 'Wrong!'}*\n"
        f"Your answer: *{chosen}*\n"
        f"Correct answer: *{mcq['correct']}* — {correct_text}\n\n"
        f"📖 *Explanation:*\n{explanation}\n\n"
        f"⚡ XP: {'+' if xp_gain >= 0 else ''}{xp_gain}"
    )

    # Next question / finish buttons
    next_index = q_index + 1
    if next_index < len(question_ids):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"Next ➡ ({next_index+1}/{len(question_ids)})",
                callback_data=f"quiz_next_{next_index}"
            ),
            InlineKeyboardButton("🔖 Bookmark", callback_data=f"quiz_bookmark_{mcq_id}")
        ]])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏁 See Results", callback_data=f"quiz_result_{session_id}")
        ]])

    context.user_data["q_index"] = next_index

    await query.edit_message_text(
        feedback, parse_mode="Markdown", reply_markup=kb
    )


async def _send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    next_index = int(query.data.split("_")[-1])
    question_ids = context.user_data.get("session_qs", [])
    session_id = context.user_data.get("active_session")

    if not question_ids or next_index >= len(question_ids):
        await query.edit_message_text("Quiz complete! Use /stats to see your results.")
        return

    context.user_data["q_index"] = next_index
    await _send_question(update, context, session_id, next_index, question_ids)


# ─── Group Quiz Launch ───────────────────────────────────────────────

async def _launch_group_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, time_per_q: int):
    query = update.callback_query
    user = update.effective_user
    chat_id = context.user_data.get("setup_chat") or update.effective_chat.id
    subject = context.user_data.get("gs_subject")
    num_q = context.user_data.get("gs_num", DEFAULT_QUESTIONS_PER_QUIZ)

    mcqs = await get_mcqs_for_quiz(subject=subject, limit=num_q)
    if not mcqs:
        await query.edit_message_text("❌ Not enough MCQs for these settings.")
        return

    session_id = f"grp_{uuid.uuid4().hex[:8]}"
    question_ids = [m["id"] for m in mcqs]

    await create_quiz_session({
        "session_id": session_id,
        "chat_id": chat_id,
        "created_by": user.id,
        "subject": subject,
        "num_questions": num_q,
        "time_per_q": time_per_q,
        "question_ids": question_ids,
    })
    await update_quiz_session(session_id,
        participants=[user.id],
        scores=json.dumps({str(user.id): 0})
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✋ Join", callback_data=f"quiz_join_{session_id}"),
        InlineKeyboardButton("▶ Start Now", callback_data=f"quiz_start_{session_id}"),
    ]])

    await query.edit_message_text(
        f"🏥 *Group Quiz Ready!*\n\n"
        f"📚 Subject: {subject or 'All'}\n"
        f"❓ Questions: {num_q}\n"
        f"⏱ Time per Q: {time_per_q}s\n"
        f"🔑 Session ID: `{session_id}`\n\n"
        f"Others can join with /joingame {session_id}\n"
        f"or tap Join below:",
        parse_mode="Markdown",
        reply_markup=kb
    )


async def _begin_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: str):
    query = update.callback_query
    session = await get_quiz_session(session_id)
    if not session:
        await query.answer("Session not found.")
        return

    await update_quiz_session(session_id, status="active")
    question_ids = list(session["question_ids"])
    context.user_data["active_session"] = session_id
    context.user_data["q_index"] = 0
    context.user_data["session_qs"] = question_ids

    await query.edit_message_text(f"🚀 Quiz starting! {len(question_ids)} questions...")
    await asyncio.sleep(1)
    await _send_question(update, context, session_id, 0, question_ids)
