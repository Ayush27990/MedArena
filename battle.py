"""
Battle Mode Handler — 1v1 real-time MCQ battles
"""

import uuid
import json
import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.database import (
    get_mcqs_for_quiz, create_battle, get_battle, update_battle,
    get_mcq_by_id, record_answer, add_xp
)
from config import (
    BATTLE_INVITE_TIMEOUT, DEFAULT_QUESTION_TIME, XP_CORRECT,
    XP_SPEED_BONUS, BATTLE_BONUS_SPEED_SECONDS, XP_WRONG
)

logger = logging.getLogger(__name__)

_pending_battles: dict = {}   # battle_id -> {expires_at}
_battle_q_starts: dict = {}   # battle_id_q -> timestamp
_battle_answered: dict = {}   # battle_id_q -> {user_id: answer}


async def battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Challenge another user: /battle @username or /battle in a group."""
    user = update.effective_user
    args = context.args

    if not args:
        await update.message.reply_text(
            "⚔️ *Battle Mode*\n\n"
            "Challenge someone:\n"
            "`/battle @username` — in a group\n"
            "`/battle` — in a group (anyone can accept)\n\n"
            "Both players get the same questions\\. Faster correct answers = bonus XP\\!",
            parse_mode="MarkdownV2"
        )
        return

    battle_id = f"btl_{uuid.uuid4().hex[:8]}"
    mcqs = await get_mcqs_for_quiz(limit=10)

    if not mcqs:
        await update.message.reply_text("❌ Not enough MCQs in the database yet.")
        return

    question_ids = [m["id"] for m in mcqs]

    await create_battle({
        "battle_id": battle_id,
        "challenger": user.id,
        "opponent": 0,  # will fill when accepted
        "chat_id": update.effective_chat.id,
        "question_ids": question_ids,
    })

    _pending_battles[battle_id] = {
        "expires_at": datetime.utcnow().timestamp() + BATTLE_INVITE_TIMEOUT,
        "challenger_name": user.full_name or user.username or "Unknown",
    }

    target = args[0].lstrip("@") if args else None
    mention = f"@{target}" if target else "anyone"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⚔️ Accept Battle!", callback_data=f"accept_battle_{battle_id}"
        )
    ]])

    await update.message.reply_text(
        f"⚔️ *{user.first_name}* challenges {mention} to a battle!\n\n"
        f"🔑 Battle ID: `{battle_id}`\n"
        f"❓ Questions: {len(question_ids)}\n"
        f"⏱ {DEFAULT_QUESTION_TIME}s per question\n"
        f"⚡ Speed bonus for fast correct answers!\n\n"
        f"Accept within {BATTLE_INVITE_TIMEOUT} seconds:",
        parse_mode="Markdown",
        reply_markup=kb
    )


async def accept_battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    battle_id = query.data.replace("accept_battle_", "")

    battle = await get_battle(battle_id)
    if not battle:
        await query.answer("❌ Battle not found.", show_alert=True)
        return

    if battle["challenger"] == user.id:
        await query.answer("⚠️ You can't accept your own battle!", show_alert=True)
        return

    pending = _pending_battles.get(battle_id)
    if not pending:
        await query.answer("⏰ Battle invitation expired.", show_alert=True)
        return

    if datetime.utcnow().timestamp() > pending["expires_at"]:
        await query.answer("⏰ Battle invitation expired.", show_alert=True)
        del _pending_battles[battle_id]
        return

    # Start the battle
    scores = json.dumps({
        str(battle["challenger"]): 0,
        str(user.id): 0,
    })
    await update_battle(battle_id, opponent=user.id, status="active", scores=scores)
    del _pending_battles[battle_id]

    challenger_name = pending.get("challenger_name", "Challenger")

    await query.edit_message_text(
        f"⚔️ *BATTLE STARTED!*\n\n"
        f"🔵 {challenger_name} vs 🔴 {user.first_name}\n\n"
        f"❓ {len(list(battle['question_ids']))} questions\n"
        f"Get ready...",
        parse_mode="Markdown"
    )

    await asyncio.sleep(2)

    # Notify both players
    question_ids = list(battle["question_ids"])
    _battle_q_starts[f"{battle_id}_0"] = datetime.utcnow().timestamp()
    _battle_answered[f"{battle_id}_0"] = {}

    context.user_data["battle_id"] = battle_id
    context.user_data["battle_q"] = 0
    context.user_data["battle_qs"] = question_ids

    await _send_battle_question(
        update, context, battle_id, 0, question_ids,
        battle["challenger"], user.id
    )


async def _send_battle_question(update, context, battle_id, q_index,
                                 question_ids, player1_id, player2_id):
    mcq_id = question_ids[q_index]
    mcq = await get_mcq_by_id(mcq_id)
    if not mcq:
        return

    _battle_q_starts[f"{battle_id}_{q_index}"] = datetime.utcnow().timestamp()
    _battle_answered[f"{battle_id}_{q_index}"] = {}

    letters = ["A", "B", "C", "D", "E"]
    opts = [mcq["option_a"], mcq["option_b"], mcq["option_c"], mcq["option_d"]]
    if mcq.get("option_e"):
        opts.append(mcq["option_e"])

    total = len(question_ids)
    text = (
        f"⚔️ *BATTLE — Q{q_index+1}/{total}*\n\n"
        f"{mcq['question']}\n\n"
    )
    for i, opt in enumerate(opts):
        text += f"*{letters[i]}.* {opt}\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(l, callback_data=f"battle_ans_{l}_{mcq['id']}_{battle_id}_{q_index}")]
        for l in letters[:len(opts)]
    ])

    chat_id = update.effective_chat.id
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Battle question send error: {e}")


async def battle_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = update.effective_user

    if data == "battle_menu":
        await battle_handler(update, context)
        return

    if data.startswith("battle_ans_"):
        await _handle_battle_answer(update, context)
        return


async def _handle_battle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    parts = query.data.split("_")
    # battle_ans_LETTER_MCQID_BATTLEID_QINDEX
    chosen = parts[2]
    mcq_id = int(parts[3])
    battle_id = parts[4]
    q_index = int(parts[5])

    battle = await get_battle(battle_id)
    if not battle:
        await query.answer("Battle not found.")
        return

    # Only participants can answer
    if user.id not in [battle["challenger"], battle["opponent"]]:
        await query.answer("⚠️ You're not in this battle!", show_alert=True)
        return

    answered_key = f"{battle_id}_{q_index}"
    if str(user.id) in _battle_answered.get(answered_key, {}):
        await query.answer("⚠️ You already answered this question!", show_alert=True)
        return

    mcq = await get_mcq_by_id(mcq_id)
    is_correct = chosen == mcq["correct"]
    start_time = _battle_q_starts.get(answered_key, datetime.utcnow().timestamp())
    time_taken = datetime.utcnow().timestamp() - start_time

    xp = XP_CORRECT if is_correct else XP_WRONG
    if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS:
        xp += XP_SPEED_BONUS

    await record_answer(user.id, mcq_id, chosen, is_correct, time_taken, battle_id)
    await add_xp(user.id, xp)

    # Track answer
    if answered_key not in _battle_answered:
        _battle_answered[answered_key] = {}
    _battle_answered[answered_key][str(user.id)] = {
        "chosen": chosen,
        "correct": is_correct,
        "time": time_taken,
        "xp": xp
    }

    # Update scores
    current_scores = json.loads(battle["scores"] or "{}")
    uid_str = str(user.id)
    current_scores[uid_str] = current_scores.get(uid_str, 0) + (xp if xp > 0 else 0)
    await update_battle(battle_id, scores=json.dumps(current_scores))

    speed_tag = " ⚡ Speed bonus!" if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS else ""
    icon = "✅" if is_correct else "❌"
    await query.answer(
        f"{icon} {'Correct' if is_correct else 'Wrong'}! ({time_taken:.1f}s){speed_tag}"
    )

    # Check if both answered
    answered = _battle_answered.get(answered_key, {})
    question_ids = list(battle["question_ids"])
    both_answered = len(answered) >= 2

    if both_answered:
        next_q = q_index + 1
        if next_q < len(question_ids):
            await asyncio.sleep(1)
            await _send_battle_question(
                update, context, battle_id, next_q, question_ids,
                battle["challenger"], battle["opponent"]
            )
        else:
            await _finish_battle(update, context, battle_id, battle)


async def _finish_battle(update, context, battle_id, battle):
    await update_battle(battle_id, status="finished")

    battle = await get_battle(battle_id)
    scores = json.loads(battle["scores"] or "{}")

    p1_id = str(battle["challenger"])
    p2_id = str(battle["opponent"])

    p1_score = scores.get(p1_id, 0)
    p2_score = scores.get(p2_id, 0)

    try:
        p1 = await context.bot.get_chat(int(p1_id))
        p1_name = p1.first_name or "Player 1"
    except:
        p1_name = "Player 1"

    try:
        p2 = await context.bot.get_chat(int(p2_id))
        p2_name = p2.first_name or "Player 2"
    except:
        p2_name = "Player 2"

    if p1_score > p2_score:
        winner = f"🏆 *{p1_name}* wins!"
    elif p2_score > p1_score:
        winner = f"🏆 *{p2_name}* wins!"
    else:
        winner = "🤝 It's a *draw*!"

    result = (
        f"⚔️ *BATTLE RESULTS*\n\n"
        f"🔵 {p1_name}: *{p1_score} pts*\n"
        f"🔴 {p2_name}: *{p2_score} pts*\n\n"
        f"{winner}"
    )

    await context.bot.send_message(
        chat_id=battle["chat_id"] or update.effective_chat.id,
        text=result,
        parse_mode="Markdown"
    )
