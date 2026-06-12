"""
Battle Mode Handler — 1v1 real-time MCQ battles
Supports:
  - /battle @user  → DM mode (private questions)
  - /groupbattle   → Group mode (questions in group chat with countdown)
Both support manual timer selection before starting.
"""

import json
import uuid
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
    BATTLE_INVITE_TIMEOUT, XP_CORRECT,
    XP_SPEED_BONUS, BATTLE_BONUS_SPEED_SECONDS, XP_WRONG
)

logger = logging.getLogger(__name__)

_battle_q_starts: dict = {}
_countdown_tasks: dict = {}

TIMER_OPTIONS = [15, 20, 30, 45, 60]


def _ans_cb(letter, mcq_id, battle_id, q_index):
    return f"ba|{letter}|{mcq_id}|{battle_id}|{q_index}"


def _parse_ans_cb(data):
    _, letter, mcq_id, battle_id, q_index = data.split("|")
    return letter, int(mcq_id), battle_id, int(q_index)


# ─── Entry points ────────────────────────────────────────────────────

async def battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/battle — DM mode"""
    await _start_battle_setup(update, context, mode="dm")


async def group_battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/groupbattle — Group mode"""
    await _start_battle_setup(update, context, mode="group")


async def _start_battle_setup(update, context, mode):
    user = update.effective_user
    args = context.args
    mode_label = "DM Battle 🔒" if mode == "dm" else "Group Battle 👥"
    mode_desc = (
        "Questions sent privately to each player." if mode == "dm"
        else "Questions appear in this group chat."
    )

    if not args:
        await update.message.reply_text(
            f"⚔️ *{mode_label}*\n\n{mode_desc}\n\n"
            f"Usage: `/{('battle' if mode == 'dm' else 'groupbattle')} @username`",
            parse_mode="Markdown"
        )
        return

    target_username = args[0].lstrip("@")
    context.user_data["battle_setup"] = {
        "mode": mode,
        "target": target_username,
        "chat_id": update.effective_chat.id,
        "challenger_id": user.id,
        "challenger_name": user.first_name or "Challenger",
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{t}s", callback_data=f"bsetup_timer_{t}")
        for t in TIMER_OPTIONS
    ]])

    await update.message.reply_text(
        f"⚔️ *{mode_label}*\n\nChallenging: @{target_username}\n\n⏱ Choose seconds per question:",
        parse_mode="Markdown",
        reply_markup=kb
    )


# ─── Timer selection callback ─────────────────────────────────────────

async def battle_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    time_per_q = int(query.data.replace("bsetup_timer_", ""))
    setup = context.user_data.get("battle_setup")

    if not setup or setup["challenger_id"] != user.id:
        await query.edit_message_text("❌ Setup expired. Please run the command again.")
        return

    mode = setup["mode"]
    target = setup["target"]
    chat_id = setup["chat_id"]
    challenger_name = setup["challenger_name"]

    mcqs = await get_mcqs_for_quiz(limit=10)
    if not mcqs:
        await query.edit_message_text("❌ Not enough MCQs in the database yet.")
        return

    battle_id = f"btl{uuid.uuid4().hex[:6]}"
    question_ids = [m["id"] for m in mcqs]

    await create_battle({
        "battle_id": battle_id,
        "challenger": user.id,
        "opponent": 0,
        "chat_id": chat_id,
        "question_ids": question_ids,
    })

    await update_battle(battle_id, scores=json.dumps({
        str(user.id): 0,
        "_challenger_name": challenger_name,
        "_mode": mode,
        "_time_per_q": time_per_q,
    }))

    context.user_data.pop("battle_setup", None)

    mode_tag = "🔒 DM mode" if mode == "dm" else "👥 Group mode"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚔️ Accept Battle!", callback_data=f"accept_battle_{battle_id}")
    ]])

    await query.edit_message_text(
        f"⚔️ *{challenger_name}* challenges @{target} to a battle!\n\n"
        f"❓ Questions: {len(question_ids)}\n"
        f"⏱ Time per question: *{time_per_q}s*\n"
        f"🎮 {mode_tag}\n\n"
        f"Accept within {BATTLE_INVITE_TIMEOUT} seconds:",
        parse_mode="Markdown",
        reply_markup=kb
    )


# ─── Accept battle ───────────────────────────────────────────────────

async def accept_battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    battle_id = query.data.replace("accept_battle_", "")

    battle = await get_battle(battle_id)
    if not battle:
        await query.answer("❌ Battle not found.", show_alert=True)
        return
    if battle["status"] != "pending":
        await query.answer("❌ Battle already started or expired.", show_alert=True)
        return
    if battle["challenger"] == user.id:
        await query.answer("⚠️ You can't accept your own battle!", show_alert=True)
        return

    existing_scores = json.loads(battle["scores"] or "{}")
    challenger_name = existing_scores.pop("_challenger_name", "Challenger")
    mode = existing_scores.pop("_mode", "dm")
    time_per_q = int(existing_scores.pop("_time_per_q", 30))
    existing_scores[str(user.id)] = 0
    existing_scores[str(battle["challenger"])] = existing_scores.get(str(battle["challenger"]), 0)

    await update_battle(battle_id,
        opponent=user.id, status="active", current_q=0,
        scores=json.dumps(existing_scores)
    )

    question_ids = list(battle["question_ids"])
    mode_note = (
        "Questions are in your *private chat* with the bot." if mode == "dm"
        else "Questions will appear here in the group."
    )

    await query.edit_message_text(
        f"⚔️ *BATTLE STARTED!*\n\n"
        f"🔵 {challenger_name} vs 🔴 {user.first_name}\n"
        f"❓ {len(question_ids)} questions | ⏱ {time_per_q}s each\n\n"
        f"{mode_note}",
        parse_mode="Markdown"
    )

    await asyncio.sleep(2)

    if mode == "dm":
        await _send_dm_question(context, battle["challenger"], battle_id, 0, question_ids, time_per_q)
        await _send_dm_question(context, user.id, battle_id, 0, question_ids, time_per_q)
    else:
        await _send_group_question(context, battle["chat_id"], battle_id, 0,
                                   question_ids, time_per_q,
                                   battle["challenger"], user.id)


# ─── DM mode ─────────────────────────────────────────────────────────

async def _send_dm_question(context, user_id, battle_id, q_index, question_ids, time_per_q):
    mcq_id = question_ids[q_index]
    mcq = await get_mcq_by_id(mcq_id)
    if not mcq:
        return

    _battle_q_starts[f"{battle_id}|{q_index}"] = datetime.utcnow().timestamp()

    letters = ["A", "B", "C", "D", "E"]
    opts = [mcq["option_a"], mcq["option_b"], mcq["option_c"], mcq["option_d"]]
    if mcq.get("option_e"):
        opts.append(mcq["option_e"])

    total = len(question_ids)
    text = f"⚔️ *BATTLE — Q{q_index+1}/{total}*\n⏱ You have *{time_per_q} seconds*\n\n{mcq['question']}\n\n"
    for i, opt in enumerate(opts):
        text += f"*{letters[i]}.* {opt}\n"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(letters[i], callback_data=_ans_cb(letters[i], mcq["id"], battle_id, q_index))
        for i in range(len(opts))
    ]])

    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error(f"DM question error for {user_id}: {e}")

    asyncio.create_task(_dm_timeout(context, user_id, battle_id, q_index, question_ids, time_per_q))


async def _dm_timeout(context, user_id, battle_id, q_index, question_ids, time_per_q):
    await asyncio.sleep(time_per_q)
    battle = await get_battle(battle_id)
    if not battle or battle["status"] != "active":
        return

    answer_times = json.loads(battle.get("answer_times") or "{}")
    player_key = f"{q_index}_{user_id}"

    if player_key not in answer_times:
        answer_times[player_key] = {"chosen": "-", "correct": False, "time": time_per_q, "xp": XP_WRONG}
        await update_battle(battle_id, answer_times=json.dumps(answer_times))
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⏰ *Time's up!* Q{q_index+1} — No answer recorded.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    other_id = battle["opponent"] if user_id == battle["challenger"] else battle["challenger"]
    other_key = f"{q_index}_{other_id}"
    fresh = await get_battle(battle_id)
    fresh_times = json.loads(fresh.get("answer_times") or "{}")

    if other_key in fresh_times:
        next_q = q_index + 1
        if next_q < len(question_ids):
            await asyncio.sleep(1)
            await _send_dm_question(context, battle["challenger"], battle_id, next_q, question_ids, time_per_q)
            await _send_dm_question(context, battle["opponent"], battle_id, next_q, question_ids, time_per_q)
        else:
            await _finish_battle(context, battle_id)


# ─── Group mode ──────────────────────────────────────────────────────

async def _send_group_question(context, chat_id, battle_id, q_index,
                                question_ids, time_per_q, p1_id, p2_id):
    mcq_id = question_ids[q_index]
    mcq = await get_mcq_by_id(mcq_id)
    if not mcq:
        return

    _battle_q_starts[f"{battle_id}|{q_index}"] = datetime.utcnow().timestamp()

    letters = ["A", "B", "C", "D", "E"]
    opts = [mcq["option_a"], mcq["option_b"], mcq["option_c"], mcq["option_d"]]
    if mcq.get("option_e"):
        opts.append(mcq["option_e"])

    total = len(question_ids)
    text = f"⚔️ *BATTLE — Q{q_index+1}/{total}*\n⏱ *{time_per_q} seconds* to answer!\n\n{mcq['question']}\n\n"
    for i, opt in enumerate(opts):
        text += f"*{letters[i]}.* {opt}\n"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(letters[i], callback_data=_ans_cb(letters[i], mcq["id"], battle_id, q_index))
        for i in range(len(opts))
    ]])

    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error(f"Group question error: {e}")
        return

    task = asyncio.create_task(
        _group_countdown(context, chat_id, battle_id, q_index, question_ids, time_per_q, p1_id, p2_id)
    )
    _countdown_tasks[f"{battle_id}|{q_index}"] = task


async def _group_countdown(context, chat_id, battle_id, q_index,
                            question_ids, time_per_q, p1_id, p2_id):
    await asyncio.sleep(time_per_q)
    battle = await get_battle(battle_id)
    if not battle or battle["status"] != "active":
        return

    answer_times = json.loads(battle.get("answer_times") or "{}")
    for uid in [p1_id, p2_id]:
        player_key = f"{q_index}_{uid}"
        if player_key not in answer_times:
            answer_times[player_key] = {"chosen": "-", "correct": False, "time": time_per_q, "xp": XP_WRONG}
    await update_battle(battle_id, answer_times=json.dumps(answer_times))

    mcq = await get_mcq_by_id(question_ids[q_index])
    if mcq:
        correct_text = {"A": mcq["option_a"], "B": mcq["option_b"],
                        "C": mcq["option_c"], "D": mcq["option_d"]}.get(mcq["correct"], "")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ *Time's up!*\n✅ Correct: *{mcq['correct']}* — {correct_text}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    next_q = q_index + 1
    if next_q < len(question_ids):
        await asyncio.sleep(2)
        await _send_group_question(context, chat_id, battle_id, next_q,
                                   question_ids, time_per_q, p1_id, p2_id)
    else:
        await asyncio.sleep(1)
        await _finish_battle(context, battle_id)


# ─── Answer handler ──────────────────────────────────────────────────

async def battle_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    if data == "battle_menu":
        await battle_handler(update, context)
        return
    if data.startswith("ba|"):
        await _handle_battle_answer(update, context)
        return


async def _handle_battle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    try:
        chosen, mcq_id, battle_id, q_index = _parse_ans_cb(query.data)
    except Exception as e:
        logger.error(f"Bad battle callback: {query.data} — {e}")
        await query.answer("❌ Error processing answer.", show_alert=True)
        return

    battle = await get_battle(battle_id)
    if not battle:
        await query.answer("Battle not found.")
        return
    if user.id not in [battle["challenger"], battle["opponent"]]:
        await query.answer("⚠️ You're not in this battle!", show_alert=True)
        return

    answer_times = json.loads(battle.get("answer_times") or "{}")
    player_key = f"{q_index}_{user.id}"
    if player_key in answer_times:
        await query.answer("⚠️ Already answered!", show_alert=True)
        return

    mcq = await get_mcq_by_id(mcq_id)
    if not mcq:
        await query.answer("Question not found.")
        return

    is_correct = (chosen == mcq["correct"])
    start_time = _battle_q_starts.get(f"{battle_id}|{q_index}", datetime.utcnow().timestamp())
    time_taken = datetime.utcnow().timestamp() - start_time

    xp = XP_CORRECT if is_correct else XP_WRONG
    if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS:
        xp += XP_SPEED_BONUS

    try:
        await record_answer(user.id, mcq_id, chosen, is_correct, time_taken, battle_id)
        await add_xp(user.id, xp)
    except Exception as e:
        logger.error(f"record_answer error: {e}")

    answer_times[player_key] = {"chosen": chosen, "correct": is_correct,
                                 "time": round(time_taken, 2), "xp": xp}
    await update_battle(battle_id, answer_times=json.dumps(answer_times))

    scores = json.loads(battle["scores"] or "{}")
    uid_str = str(user.id)
    scores[uid_str] = scores.get(uid_str, 0) + (xp if xp > 0 else 0)
    await update_battle(battle_id, scores=json.dumps(scores))

    icon = "✅" if is_correct else "❌"
    speed_tag = "⚡ Speed bonus! " if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS else ""
    correct_text = {"A": mcq["option_a"], "B": mcq["option_b"],
                    "C": mcq["option_c"], "D": mcq["option_d"]}.get(mcq["correct"], mcq["correct"])

    result_msg = (
        f"{icon} *{'Correct!' if is_correct else 'Wrong!'}* {speed_tag}\n"
        f"Your answer: *{chosen}* | Correct: *{mcq['correct']}* — {correct_text}\n"
        f"⏱ {time_taken:.1f}s | ⚡ XP: {'+' if xp >= 0 else ''}{xp}\n\n"
        f"⏳ Waiting for opponent..."
    )

    try:
        await query.edit_message_text(result_msg, parse_mode="Markdown")
    except Exception:
        await query.answer(f"{icon} {'Correct' if is_correct else 'Wrong'}! ({time_taken:.1f}s)")

    other_id = battle["opponent"] if user.id == battle["challenger"] else battle["challenger"]
    other_key = f"{q_index}_{other_id}"
    fresh_battle = await get_battle(battle_id)
    fresh_times = json.loads(fresh_battle.get("answer_times") or "{}")
    both_answered = (player_key in fresh_times) and (other_key in fresh_times)

    if both_answered:
        question_ids = list(battle["question_ids"])
        next_q = q_index + 1

        # Cancel group countdown if both answered early
        task = _countdown_tasks.pop(f"{battle_id}|{q_index}", None)
        if task:
            task.cancel()

        if next_q < len(question_ids):
            await asyncio.sleep(2)
            # Re-fetch to get mode and time_per_q
            fresh2 = await get_battle(battle_id)
            sc = json.loads(fresh2["scores"] or "{}")
            time_per_q = int(sc.get("_time_per_q", 30))

            # Determine mode by checking if chat_id matches a group
            is_group_mode = fresh2.get("chat_id") and fresh2["chat_id"] != fresh2["challenger"]

            if is_group_mode:
                try:
                    await context.bot.send_message(
                        chat_id=fresh2["chat_id"],
                        text=f"✅ Correct: *{mcq['correct']}* — {correct_text}\nNext question coming up...",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                await _send_group_question(context, fresh2["chat_id"], battle_id, next_q,
                                           question_ids, time_per_q,
                                           battle["challenger"], battle["opponent"])
            else:
                await _send_dm_question(context, battle["challenger"], battle_id,
                                        next_q, question_ids, time_per_q)
                await _send_dm_question(context, battle["opponent"], battle_id,
                                        next_q, question_ids, time_per_q)
        else:
            await asyncio.sleep(1)
            await _finish_battle(context, battle_id)


# ─── Finish battle ───────────────────────────────────────────────────

async def _finish_battle(context, battle_id):
    await update_battle(battle_id, status="finished")
    battle = await get_battle(battle_id)

    scores = json.loads(battle["scores"] or "{}")
    p1_id = str(battle["challenger"])
    p2_id = str(battle["opponent"])
    p1_score = scores.get(p1_id, 0)
    p2_score = scores.get(p2_id, 0)

    try:
        p1_name = (await context.bot.get_chat(int(p1_id))).first_name or "Player 1"
    except Exception:
        p1_name = "Player 1"
    try:
        p2_name = (await context.bot.get_chat(int(p2_id))).first_name or "Player 2"
    except Exception:
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

    if battle.get("chat_id"):
        try:
            await context.bot.send_message(chat_id=battle["chat_id"], text=result, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Group result error: {e}")

    for uid in [int(p1_id), int(p2_id)]:
        try:
            await context.bot.send_message(chat_id=uid, text=result, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"DM result error for {uid}: {e}")
