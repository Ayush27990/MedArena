"""
Battle Mode Handler — 1v1 real-time MCQ battles
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
    get_mcq_by_id, record_answer, add_xp, get_user_by_username
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


async def battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/battle — DM mode"""
    try:
        await _start_battle_setup(update, context, mode="dm")
    except Exception as e:
        logger.error(f"battle_handler error: {e}", exc_info=True)
        msg = update.callback_query or update.message
        if hasattr(msg, 'edit_message_text'):
            await msg.edit_message_text(f"❌ Error: {e}")
        else:
            await update.message.reply_text(f"❌ Error: {e}")


async def group_battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/groupbattle — Group mode"""
    try:
        await _start_battle_setup(update, context, mode="group")
    except Exception as e:
        logger.error(f"group_battle_handler error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


async def _start_battle_setup(update, context, mode):
    user = update.effective_user
    args = context.args or []

    mode_label = "DM Battle 🔒" if mode == "dm" else "Group Battle 👥"

    if not args:
        text = (
            f"⚔️ *{mode_label}*\n\n"
            f"Usage: `/{('battle' if mode == 'dm' else 'groupbattle')} @username`\n\n"
            f"Example: `/battle @Ayush27990`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    target_username = args[0].lstrip("@")

    # Look up opponent in database by username
    opponent_user = await get_user_by_username(target_username)
    if not opponent_user:
        await update.message.reply_text(
            f"❌ @{target_username} hasn't started the bot yet.\n\n"
            f"Ask them to open @MedArena121_bot and send /start first!",
            parse_mode="Markdown"
        )
        return

    context.user_data["battle_setup"] = {
        "mode": mode,
        "target": target_username,
        "target_id": opponent_user["user_id"],
        "chat_id": update.effective_chat.id,
        "challenger_id": user.id,
        "challenger_name": user.first_name or "Challenger",
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{t}s", callback_data=f"bsetup_timer_{t}")
        for t in TIMER_OPTIONS
    ]])

    await update.message.reply_text(
        f"⚔️ *{mode_label}*\n\n"
        f"Challenging: @{target_username}\n\n"
        f"⏱ Choose seconds per question:",
        parse_mode="Markdown",
        reply_markup=kb
    )


async def battle_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    try:
        time_per_q = int(query.data.replace("bsetup_timer_", ""))
        setup = context.user_data.get("battle_setup")

        if not setup or setup["challenger_id"] != user.id:
            await query.edit_message_text("❌ Setup expired. Please run the command again.")
            return

        mode = setup["mode"]
        target = setup["target"]
        target_id = setup["target_id"]
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

        mode_tag = "🔒 DM mode — questions sent privately" if mode == "dm" \
                   else "👥 Group mode"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚔️ Accept Battle!", callback_data=f"accept_battle_{battle_id}")
        ]])

        invite_text = (
            f"⚔️ *{challenger_name}* challenges @{target} to a battle!\n\n"
            f"❓ Questions: {len(question_ids)}\n"
            f"⏱ Time per question: *{time_per_q}s*\n"
            f"🎮 {mode_tag}\n\n"
            f"Accept within {BATTLE_INVITE_TIMEOUT} seconds:"
        )

        # Send to challenger's chat
        await query.edit_message_text(
            invite_text, parse_mode="Markdown", reply_markup=kb
        )

        # Also send directly to opponent's private chat
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=invite_text,
                parse_mode="Markdown",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Could not send battle invite to opponent {target_id}: {e}")

    except Exception as e:
        logger.error(f"battle_setup_callback error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Error setting up battle: {e}")


async def accept_battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    try:
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

        started_text = (
            f"⚔️ *BATTLE STARTED!*\n\n"
            f"🔵 {challenger_name} vs 🔴 {user.first_name}\n"
            f"❓ {len(question_ids)} questions | ⏱ {time_per_q}s each\n\n"
            f"Check your private chat with the bot for questions!"
        )

        await query.edit_message_text(started_text, parse_mode="Markdown")

        # Notify challenger too
        try:
            await context.bot.send_message(
                chat_id=battle["challenger"],
                text=started_text,
                parse_mode="Markdown"
            )
        except Exception:
            pass

        await asyncio.sleep(2)

        await _send_dm_question(context, battle["challenger"], battle_id, 0, question_ids, time_per_q)
        await _send_dm_question(context, user.id, battle_id, 0, question_ids, time_per_q)

    except Exception as e:
        logger.error(f"accept_battle_handler error: {e}", exc_info=True)
        try:
            await query.edit_message_text(f"❌ Error starting battle: {e}")
        except Exception:
            pass


async def _send_dm_question(context, user_id, battle_id, q_index, question_ids, time_per_q):
    try:
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
        text = (
            f"⚔️ *BATTLE — Q{q_index+1}/{total}*\n"
            f"⏱ You have *{time_per_q} seconds*\n\n"
            f"{mcq['question']}\n\n"
        )
        for i, opt in enumerate(opts):
            text += f"*{letters[i]}.* {opt}\n"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                letters[i],
                callback_data=_ans_cb(letters[i], mcq["id"], battle_id, q_index)
            )
            for i in range(len(opts))
        ]])

        await context.bot.send_message(
            chat_id=user_id, text=text,
            parse_mode="Markdown", reply_markup=kb
        )

        task = asyncio.create_task(
            _dm_timeout(context, user_id, battle_id, q_index, question_ids, time_per_q)
        )
        _countdown_tasks[f"{battle_id}|{q_index}|{user_id}"] = task
    except Exception as e:
        logger.error(f"_send_dm_question error for {user_id}: {e}", exc_info=True)


async def _dm_timeout(context, user_id, battle_id, q_index, question_ids, time_per_q):
    await asyncio.sleep(time_per_q)
    try:
        battle = await get_battle(battle_id)
        if not battle or battle["status"] != "active":
            return

        # Stale timeout: the battle has already moved past this question
        # (both players answered earlier and it was already advanced).
        if int(battle.get("current_q", q_index)) != q_index:
            return

        answer_times = json.loads(battle.get("answer_times") or "{}")
        player_key = f"{q_index}_{user_id}"

        if player_key not in answer_times:
            answer_times[player_key] = {
                "chosen": "-", "correct": False,
                "time": time_per_q, "xp": XP_WRONG
            }
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
            await asyncio.sleep(1)
            await _advance_battle(context, battle_id, q_index, question_ids, time_per_q)
    except Exception as e:
        logger.error(f"_dm_timeout error: {e}", exc_info=True)


async def _advance_battle(context, battle_id, q_index, question_ids, time_per_q):
    """Move the battle on to the next question (or finish it).

    Guarded by `current_q` so that if both _handle_battle_answer and a
    _dm_timeout race to advance the same question, only the first one
    actually sends the next question / finishes the battle.
    """
    battle = await get_battle(battle_id)
    if not battle or battle["status"] != "active":
        return
    if int(battle.get("current_q", q_index)) != q_index:
        return  # already advanced by the other path

    next_q = q_index + 1
    await update_battle(battle_id, current_q=next_q)

    if next_q < len(question_ids):
        await _send_dm_question(context, battle["challenger"], battle_id,
                                next_q, question_ids, time_per_q)
        await _send_dm_question(context, battle["opponent"], battle_id,
                                next_q, question_ids, time_per_q)
    else:
        await _finish_battle(context, battle_id)


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

    try:
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

        answer_times[player_key] = {
            "chosen": chosen, "correct": is_correct,
            "time": round(time_taken, 2), "xp": xp
        }
        await update_battle(battle_id, answer_times=json.dumps(answer_times))

        scores = json.loads(battle["scores"] or "{}")
        uid_str = str(user.id)
        scores[uid_str] = scores.get(uid_str, 0) + (xp if xp > 0 else 0)
        await update_battle(battle_id, scores=json.dumps(scores))

        icon = "✅" if is_correct else "❌"
        speed_tag = "⚡ Speed bonus! " if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS else ""
        correct_text = {
            "A": mcq["option_a"], "B": mcq["option_b"],
            "C": mcq["option_c"], "D": mcq["option_d"]
        }.get(mcq["correct"], mcq["correct"])

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

            for uid in (battle["challenger"], battle["opponent"]):
                task = _countdown_tasks.pop(f"{battle_id}|{q_index}|{uid}", None)
                if task:
                    task.cancel()

            fresh2 = await get_battle(battle_id)
            sc = json.loads(fresh2["scores"] or "{}")
            time_per_q = int(sc.get("_time_per_q", 30))

            await asyncio.sleep(2)
            await _advance_battle(context, battle_id, q_index, question_ids, time_per_q)

    except Exception as e:
        logger.error(f"_handle_battle_answer error: {e}", exc_info=True)
        try:
            await query.answer(f"❌ Error: {e}", show_alert=True)
        except Exception:
            pass


async def _finish_battle(context, battle_id):
    try:
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

        for uid in [int(p1_id), int(p2_id)]:
            try:
                await context.bot.send_message(
                    chat_id=uid, text=result, parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"DM result error for {uid}: {e}")

    except Exception as e:
        logger.error(f"_finish_battle error: {e}", exc_info=True)
