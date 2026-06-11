"""
Group Quiz Handler - FIXED VERSION
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

_question_start_times: dict = {}


def _escape(text: str) -> str:
    if not text:
        return ""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _build_question_msg(mcq, q_num: int, total: int) -> tuple[str, InlineKeyboardMarkup]:
    letters = ["A", "B", "C", "D", "E"]
    opts = [mcq["option_a"], mcq["option_b"], mcq["option_c"], mcq["option_d"]]
    if mcq.get("option_e"):
        opts.append(mcq["option_e"])

    lines = [
        f"❓ *Question {q_num}/{total}*",
        f"📖 {mcq.get('subject') or 'General'}\n",
        _escape(mcq["question"]) + "\n",
    ]
    for i, opt in enumerate(opts):
        lines.append(f"*{letters[i]}\\.* {_escape(opt)}")

    text = "\n".join(lines)

    kb = [
        [
            InlineKeyboardButton("A", callback_data="quiz_ans_A"),
            InlineKeyboardButton("B", callback_data="quiz_ans_B"),
            InlineKeyboardButton("C", callback_data="quiz_ans_C"),
            InlineKeyboardButton("D", callback_data="quiz_ans_D"),
        ]
    ]
    if mcq.get("option_e"):
        kb.append([InlineKeyboardButton("E", callback_data="quiz_ans_E")])
    kb.append([InlineKeyboardButton("🔖 Bookmark", callback_data="quiz_bookmark")])

    return text, InlineKeyboardMarkup(kb)


async def quiz_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📚 By Subject", callback_data="quiz_filter_subject"),
            InlineKeyboardButton("🎯 By Difficulty", callback_data="quiz_filter_diff"),
        ],
        [InlineKeyboardButton("🎲 Random Quiz", callback_data="quiz_random")],
    ]
    text = "🧠 *Quiz Mode*\n\nChoose how you want to practice:"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def start_group_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data["setup_chat"] = update.effective_chat.id
    context.user_data["setup_user"] = user.id
    kb = [[InlineKeyboardButton(sub, callback_data=f"quiz_gs_{sub}")] for sub in SUBJECTS[:8]]
    kb.append([InlineKeyboardButton("🎲 All Subjects", callback_data="quiz_gs_ALL")])
    await update.message.reply_text(
        "🏥 *Group Quiz Setup*\n\nSelect a subject:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


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
        await update.message.reply_text("❌ Quiz already started or ended.")
        return
    user = update.effective_user
    participants = list(session["participants"] or [])
    if user.id in participants:
        await update.message.reply_text("You already joined!")
        return
    participants.append(user.id)
    await update_quiz_session(session_id, participants=participants)
    await update.message.reply_text(f"✅ Joined! Players: {len(participants)}")


async def quiz_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user

    if data == "quiz_menu":
        return await quiz_menu_handler(update, context)

    if data == "quiz_random":
        await query.edit_message_text("⏳ Loading questions...")
        await _run_solo_quiz(update, context, user.id)
        return

    if data == "quiz_filter_subject":
        kb = [[InlineKeyboardButton(s, callback_data=f"quiz_sub_{s}")] for s in SUBJECTS[:10]]
        kb.append([InlineKeyboardButton("More ▶", callback_data="quiz_sub_more")])
        await query.edit_message_text("📚 Select Subject:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "quiz_sub_more":
        kb = [[InlineKeyboardButton(s, callback_data=f"quiz_sub_{s}")] for s in SUBJECTS[10:]]
        kb.append([InlineKeyboardButton("◀ Back", callback_data="quiz_filter_subject")])
        await query.edit_message_text("📚 Select Subject:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("quiz_sub_"):
        subject = data[9:]
        context.user_data["quiz_subject"] = subject
        kb = [[InlineKeyboardButton(d, callback_data=f"quiz_diff_{d}")] for d in DIFFICULTIES]
        kb.append([InlineKeyboardButton("Any Difficulty", callback_data="quiz_diff_ANY")])
        await query.edit_message_text(
            f"📚 Subject: *{subject}*\n\nSelect difficulty:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "quiz_filter_diff":
        kb = [[InlineKeyboardButton(d, callback_data=f"quiz_diff_{d}")] for d in DIFFICULTIES]
        await query.edit_message_text("🎯 Select Difficulty:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("quiz_diff_"):
        diff = data[10:]
        diff = None if diff == "ANY" else diff
        subject = context.user_data.get("quiz_subject")
        await query.edit_message_text("⏳ Loading questions...")
        await _run_solo_quiz(update, context, user.id, subject=subject, difficulty=diff)
        return

    if data.startswith("quiz_ans_"):
        chosen = data[9:]
        await _handle_answer(update, context, chosen)
        return

    if data == "quiz_next":
        await _send_next_question(update, context)
        return

    if data == "quiz_result":
        await _show_result(update, context)
        return

    if data == "quiz_bookmark":
        mcq_id = context.user_data.get("current_mcq_id")
        if mcq_id:
            from services.database import toggle_bookmark
            added = await toggle_bookmark(user.id, mcq_id)
            await query.answer("🔖 Bookmarked!" if added else "🗑 Removed")
        return

    if data.startswith("quiz_gs_"):
        subject = data[8:]
        if subject == "ALL":
            subject = None
        context.user_data["gs_subject"] = subject
        kb = [[InlineKeyboardButton(str(n), callback_data=f"quiz_gn_{n}")]
              for n in [5, 10, 15, 20, 30]]
        await query.edit_message_text(
            f"Subject: *{subject or 'All'}*\n\nHow many questions?",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("quiz_gn_"):
        n = int(data[8:])
        context.user_data["gs_num"] = n
        kb = [[InlineKeyboardButton(f"{t}s", callback_data=f"quiz_gt_{t}")]
              for t in [15, 20, 30, 45, 60]]
        await query.edit_message_text(
            f"Questions: *{n}*\n\nSeconds per question?",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
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

    if data.startswith("quiz_join_"):
        session_id = data[10:]
        session = await get_quiz_session(session_id)
        if not session or session["status"] != "waiting":
            await query.answer("❌ Can't join this quiz.", show_alert=True)
            return
        participants = list(session["participants"] or [])
        if user.id not in participants:
            participants.append(user.id)
            await update_quiz_session(session_id, participants=participants)
        await query.answer(f"✅ Joined! {len(participants)} players")
        return

    if data == "my_stats":
        from handlers.stats import stats_handler
        return await stats_handler(update, context)

    if data == "leaderboard":
        from handlers.stats import leaderboard_handler
        return await leaderboard_handler(update, context)


async def _run_solo_quiz(update, context, user_id,
                         subject=None, difficulty=None,
                         num_q=DEFAULT_QUESTIONS_PER_QUIZ):
    mcqs = await get_mcqs_for_quiz(subject=subject, difficulty=difficulty, limit=num_q)
    if not mcqs:
        msg = "❌ No approved MCQs found for these filters. Try different settings."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    question_ids = [m["id"] for m in mcqs]
    context.user_data["session_qs"] = question_ids
    context.user_data["q_index"] = 0
    context.user_data["score"] = 0
    context.user_data["total"] = len(question_ids)

    await _send_question_by_index(update, context, 0)


async def _send_question_by_index(update, context, index: int):
    question_ids = context.user_data.get("session_qs", [])
    if index >= len(question_ids):
        await _show_result(update, context)
        return

    mcq_id = question_ids[index]
    mcq = await get_mcq_by_id(mcq_id)
    if not mcq:
        context.user_data["q_index"] = index + 1
        await _send_question_by_index(update, context, index + 1)
        return

    context.user_data["q_index"] = index
    context.user_data["current_mcq_id"] = mcq_id
    context.user_data["current_correct"] = mcq["correct"]
    context.user_data["answered"] = False
    _question_start_times[f"solo_{update.effective_user.id}"] = datetime.utcnow().timestamp()

    total = context.user_data.get("total", len(question_ids))
    text, keyboard = _build_question_msg(mcq, index + 1, total)

    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, parse_mode="MarkdownV2", reply_markup=keyboard
            )
        else:
            await update.effective_chat.send_message(
                text, parse_mode="MarkdownV2", reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Send question error: {e}")
        plain = f"Question {index+1}/{total}\n\n{mcq['question']}\n\nA. {mcq['option_a']}\nB. {mcq['option_b']}\nC. {mcq['option_c']}\nD. {mcq['option_d']}"
        if update.callback_query:
            await update.callback_query.edit_message_text(plain, reply_markup=keyboard)
        else:
            await update.effective_chat.send_message(plain, reply_markup=keyboard)


async def _handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, chosen: str):
    query = update.callback_query
    user = update.effective_user

    if context.user_data.get("answered"):
        await query.answer("⚠️ Already answered!", show_alert=True)
        return

    context.user_data["answered"] = True

    correct = context.user_data.get("current_correct", "A")
    mcq_id = context.user_data.get("current_mcq_id")
    q_index = context.user_data.get("q_index", 0)
    question_ids = context.user_data.get("session_qs", [])

    start_key = f"solo_{user.id}"
    start_time = _question_start_times.get(start_key, datetime.utcnow().timestamp())
    time_taken = datetime.utcnow().timestamp() - start_time

    is_correct = (chosen == correct)
    xp_gain = XP_CORRECT if is_correct else XP_WRONG
    if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS:
        xp_gain += XP_SPEED_BONUS

    if mcq_id:
        try:
            await record_answer(user.id, mcq_id, chosen, is_correct, time_taken, f"solo_{user.id}")
            await add_xp(user.id, xp_gain)
        except Exception as e:
            logger.error(f"Record answer error: {e}")

    if is_correct:
        context.user_data["score"] = context.user_data.get("score", 0) + 1

    mcq = await get_mcq_by_id(mcq_id) if mcq_id else None
    explanation = ""
    correct_text = correct
    if mcq:
        opts = {"A": mcq["option_a"], "B": mcq["option_b"],
                "C": mcq["option_c"], "D": mcq["option_d"]}
        if mcq.get("option_e"):
            opts["E"] = mcq["option_e"]
        correct_text = opts.get(correct, correct)
        explanation = mcq.get("explanation") or ""

    icon = "✅" if is_correct else "❌"
    result_text = f"{icon} *{'Correct!' if is_correct else 'Wrong!'}*\n\n"
    result_text += f"Your answer: *{chosen}*\n"
    result_text += f"Correct: *{correct}* — {correct_text}\n"

    # FIX: Use full explanation (up to Telegram's 4096 char limit), not 300
    if explanation:
        # Reserve ~200 chars for the header and XP line
        max_exp_len = 3800
        if len(explanation) > max_exp_len:
            explanation = explanation[:max_exp_len] + "…"
        result_text += f"\n📖 {explanation}\n"

    result_text += f"\n⚡ XP: {'+' if xp_gain >= 0 else ''}{xp_gain}"

    next_index = q_index + 1
    total = context.user_data.get("total", len(question_ids))

    if next_index < len(question_ids):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Next ➡ ({next_index + 1}/{total})", callback_data="quiz_next"),
            InlineKeyboardButton("🔖 Bookmark", callback_data="quiz_bookmark")
        ]])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏁 See Results", callback_data="quiz_result"),
            InlineKeyboardButton("🔖 Bookmark", callback_data="quiz_bookmark")
        ]])

    try:
        await query.edit_message_text(result_text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error(f"Edit answer error: {e}")
        await query.edit_message_text(result_text, reply_markup=kb)


async def _send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    next_index = context.user_data.get("q_index", 0) + 1
    await _send_question_by_index(update, context, next_index)


async def _show_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    score = context.user_data.get("score", 0)
    total = context.user_data.get("total", 0)
    accuracy = round(score * 100 / total, 1) if total > 0 else 0

    if accuracy >= 80:
        grade = "🏆 Excellent!"
    elif accuracy >= 60:
        grade = "👍 Good"
    elif accuracy >= 40:
        grade = "📚 Keep practicing"
    else:
        grade = "💪 More revision needed"

    text = (
        f"🏁 *Quiz Complete!*\n\n"
        f"✅ Correct: *{score}/{total}*\n"
        f"🎯 Accuracy: *{accuracy}%*\n\n"
        f"{grade}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Quiz Again", callback_data="quiz_random"),
            InlineKeyboardButton("📊 My Stats", callback_data="my_stats"),
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="quiz_menu")],
    ])

    for key in ["session_qs", "q_index", "score", "total",
                "current_mcq_id", "current_correct", "answered", "quiz_subject"]:
        context.user_data.pop(key, None)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.effective_chat.send_message(
            text, parse_mode="Markdown", reply_markup=kb
        )


async def _launch_group_quiz(update, context, time_per_q: int):
    query = update.callback_query
    user = update.effective_user
    chat_id = context.user_data.get("setup_chat") or update.effective_chat.id
    subject = context.user_data.get("gs_subject")
    num_q = context.user_data.get("gs_num", DEFAULT_QUESTIONS_PER_QUIZ)

    mcqs = await get_mcqs_for_quiz(subject=subject, limit=num_q)
    if not mcqs:
        await query.edit_message_text("❌ Not enough approved MCQs for these settings.")
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
        f"🔑 Session: `{session_id}`\n\n"
        f"Others can join: /joingame {session_id}",
        parse_mode="Markdown",
        reply_markup=kb
    )


async def _begin_quiz(update, context, session_id: str):
    query = update.callback_query
    session = await get_quiz_session(session_id)
    if not session:
        await query.answer("Session not found.")
        return

    await update_quiz_session(session_id, status="active")
    question_ids = list(session["question_ids"])
    context.user_data["session_qs"] = question_ids
    context.user_data["q_index"] = 0
    context.user_data["score"] = 0
    context.user_data["total"] = len(question_ids)

    await query.edit_message_text(f"🚀 Quiz starting! {len(question_ids)} questions...")
    await asyncio.sleep(1)
    await _send_question_by_index(update, context, 0)
