"""
MedArena — 1v1 Battle Handler
Fixed:
1. One shared timeout per question.
2. Automatic progression when either player does not answer.
3. Strict subject/topic-specific Gemini generation.
4. No unrelated-subject fallback.
5. Race protection using per-battle asyncio locks.
6. time_per_q is kept in scores so every question uses the chosen timer.
"""

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop

from services.database import (
    get_mcqs_for_quiz,
    create_battle,
    get_battle,
    update_battle,
    get_mcq_by_id,
    record_answer,
    add_xp,
    get_user_by_username,
    insert_mcq,
    get_mcq_id_by_hash,
    search_mcqs,
)

from services.gemini_service import generate_mcqs, GeminiError
from handlers.import_mcq import _make_hash

from config import (
    BATTLE_INVITE_TIMEOUT,
    XP_CORRECT,
    XP_SPEED_BONUS,
    BATTLE_BONUS_SPEED_SECONDS,
    XP_WRONG,
    SUBJECTS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# BATTLE CONFIGURATION
# ---------------------------------------------------------

TIMER_OPTIONS = [15, 20, 30, 45, 60]
BATTLE_QUESTIONS = 10
BATTLE_EXPLANATION_CHARS = 400

_battle_q_starts = {}
_countdown_tasks = {}
_battle_locks = {}


def _get_battle_lock(battle_id):
    if battle_id not in _battle_locks:
        _battle_locks[battle_id] = asyncio.Lock()
    return _battle_locks[battle_id]


# ---------------------------------------------------------
# GENERAL HELPERS
# ---------------------------------------------------------

def _md(text):
    out = str(text or "")
    for ch in ("\\", "_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def _json_load(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _subject_pool():
    return [s for s in SUBJECTS if s != "Other"]


def _subject_keyboard():
    pool = _subject_pool()
    rows = [
        [InlineKeyboardButton("🎲 Mixed (all subjects)", callback_data="bsetup_subj_all")],
        [InlineKeyboardButton("✍️ Type my own topic", callback_data="bsetup_typeany")],
    ]
    for i in range(0, len(pool), 2):
        row = []
        for j in range(i, min(i + 2, len(pool))):
            row.append(
                InlineKeyboardButton(pool[j][:24], callback_data=f"bsetup_subj_{j}")
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _subject_action_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Whole subject (high yield)", callback_data="bsetup_whole")],
        [InlineKeyboardButton("✍️ Type a topic in this subject", callback_data="bsetup_typesub")],
    ])


def _ans_cb(letter, mcq_id, battle_id, q_index):
    return f"ba|{letter}|{mcq_id}|{battle_id}|{q_index}"


def _parse_ans_cb(data):
    _, letter, mcq_id, battle_id, q_index = data.split("|")
    return letter, int(mcq_id), battle_id, int(q_index)


def _answer_key(q_index, user_id):
    return f"{q_index}_{user_id}"


# ---------------------------------------------------------
# GEMINI QUESTION GENERATION
# ---------------------------------------------------------

async def _gemini_battle_question_ids(count=BATTLE_QUESTIONS, subject=None, topic=None):
    pool = _subject_pool()
    avoid = []

    try:
        if subject and topic:
            existing = await search_mcqs(subject=subject, topic=topic, limit=25)
        elif subject:
            existing = await search_mcqs(subject=subject, limit=25)
        elif topic:
            existing = await search_mcqs(topic=topic, limit=25)
        else:
            existing = await get_mcqs_for_quiz(limit=25)

        avoid = [str(row["question"]) for row in existing if row["question"]]
    except Exception:
        logger.exception("Could not retrieve existing questions for Gemini.")

    if subject and topic:
        prompt = (
            f"ONLY {subject}. "
            f"Generate medical entrance examination MCQs strictly about {topic}. "
            f"Every question must belong to {subject} and must directly test {topic}. "
            f"Do not include questions from other medical subjects."
        )
    elif subject:
        prompt = (
            f"ONLY {subject}. "
            f"Generate high-yield medical entrance examination MCQs "
            f"from different important topics within {subject}. "
            f"Every question must belong to {subject}. "
            f"Do not include questions from other subjects."
        )
    elif topic:
        prompt = (
            f"Generate high-yield medical entrance examination MCQs "
            f"strictly about {topic}. Do not include unrelated medical topics."
        )
    else:
        selected_subjects = random.sample(pool, min(count, len(pool)))
        prompt = (
            "Generate high-yield medical entrance examination MCQs "
            "from a balanced mixture of these subjects: "
            + ", ".join(selected_subjects)
            + ". Avoid concentrating all questions in one subject."
        )

    logger.info("Generating battle questions | subject=%s | topic=%s", subject, topic)

    generated, dropped = await generate_mcqs(
        prompt,
        count=count,
        level="NEET PG / INICET / FMGE",
        avoid=avoid,
    )

    if not generated:
        raise GeminiError("Gemini did not generate valid questions. Please try again.")

    logger.info("Gemini generated %s questions; dropped %s.", len(generated), dropped)

    ids = []

    for item in generated:
        try:
            options = item["options"]
            if len(options) != 4:
                continue

            question = item["question"].strip()
            if not question:
                continue

            correct = str(item["correct"]).strip().upper()
            if correct not in ("A", "B", "C", "D"):
                continue

            assigned_subject = subject if subject else None
            assigned_topic = topic or ""
            question_hash = _make_hash(question, options[0])

            new_id = await insert_mcq({
                "question": question,
                "option_a": options[0],
                "option_b": options[1],
                "option_c": options[2],
                "option_d": options[3],
                "correct": correct,
                "explanation": item.get("explanation"),
                "subject": assigned_subject,
                "topic": assigned_topic,
                "difficulty": "NEET PG",
                "source_type": "gemini_battle",
                "source_chat": 0,
                "imported_by": 0,
                "approved": True,
                "hash": question_hash,
            })

            if not new_id:
                new_id = await get_mcq_id_by_hash(question_hash)

            if new_id and new_id not in ids:
                ids.append(new_id)

        except Exception:
            logger.exception("Could not save a generated battle question.")

    return ids


async def _get_matching_saved_questions(subject=None, topic=None, limit=BATTLE_QUESTIONS):
    questions = await get_mcqs_for_quiz(subject=subject, topic=topic, limit=limit)
    return [row["id"] for row in questions]


async def _prepare_battle_questions(subject=None, topic=None):
    generated_ids = []

    try:
        generated_ids = await _gemini_battle_question_ids(
            count=BATTLE_QUESTIONS, subject=subject, topic=topic
        )
    except GeminiError:
        logger.exception("Gemini generation failed.")
    except Exception:
        logger.exception("Unexpected Gemini generation failure.")

    if len(generated_ids) >= BATTLE_QUESTIONS:
        return generated_ids[:BATTLE_QUESTIONS], True

    try:
        saved_ids = await _get_matching_saved_questions(
            subject=subject, topic=topic, limit=BATTLE_QUESTIONS
        )
    except Exception:
        logger.exception("Could not retrieve matching saved questions.")
        saved_ids = []

    combined = []
    for question_id in generated_ids + saved_ids:
        if question_id not in combined:
            combined.append(question_id)

    if len(combined) < 3:
        raise GeminiError(
            "Not enough questions are available for this subject/topic. "
            "Gemini may be temporarily unavailable. Please try again."
        )

    return combined[:BATTLE_QUESTIONS], bool(generated_ids)


# ---------------------------------------------------------
# START BATTLE
# ---------------------------------------------------------

async def battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.callback_query:
            await update.callback_query.answer()
        await _start_battle_setup(update, context, mode="dm")
    except Exception as e:
        logger.exception("battle_handler error")
        msg = update.effective_message
        if msg:
            await msg.reply_text(f"❌ Error starting battle: {e}")


async def group_battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await _start_battle_setup(update, context, mode="group")
    except Exception as e:
        logger.exception("group_battle_handler error")
        await update.effective_message.reply_text(f"❌ Error: {e}")


async def _start_battle_setup(update, context, mode):
    user = update.effective_user
    args = context.args or []

    mode_label = "DM Battle 🔒" if mode == "dm" else "Group Battle 👥"

    if not args:
        await update.effective_message.reply_text(
            f"⚔️ {mode_label}\n\n"
            f"Usage: /{'battle' if mode == 'dm' else 'groupbattle'} @username\n\n"
            "Example: /battle @username"
        )
        return

    target_username = args[0].lstrip("@")
    opponent_user = await get_user_by_username(target_username)

    if not opponent_user:
        await update.effective_message.reply_text(
            f"❌ @{target_username} has not started the bot yet.\n\n"
            "Ask them to open the bot and send /start first."
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

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{seconds}s", callback_data=f"bsetup_timer_{seconds}")
        for seconds in TIMER_OPTIONS
    ]])

    await update.effective_message.reply_text(
        f"⚔️ {mode_label}\n\n"
        f"Challenging: @{target_username}\n\n"
        "⏱ Choose seconds per question:",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------
# BATTLE SETUP CALLBACKS
# ---------------------------------------------------------

async def battle_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    setup = context.user_data.get("battle_setup")

    if not setup or setup["challenger_id"] != user.id:
        await query.answer("Setup expired. Please start again.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("bsetup_timer_"):
        setup["time_per_q"] = int(data.replace("bsetup_timer_", ""))
        setup["await_topic"] = False
        setup["subject"] = None

        await query.edit_message_text(
            f"⚔️ Challenging @{setup['target']}\n"
            f"⏱ {setup['time_per_q']} seconds per question\n\n"
            "📚 Choose a subject:",
            reply_markup=_subject_keyboard(),
        )
        return

    if not setup.get("time_per_q"):
        await query.edit_message_text("❌ Setup expired. Please start again.")
        return

    if data == "bsetup_typeany":
        setup["subject"] = None
        setup["await_topic"] = True
        await query.edit_message_text(
            "✍️ Send the topic you want to battle on.\n\n"
            "Example: Nephrotic syndrome, beta blockers, glycolysis, brachial plexus."
        )
        return

    if data == "bsetup_typesub":
        setup["await_topic"] = True
        await query.edit_message_text(f"✍️ Send the topic in {setup['subject']}.")
        return

    if data == "bsetup_whole":
        context.application.create_task(
            _battle_setup(
                context, user, query.message,
                setup["time_per_q"], setup.get("subject"), None,
            )
        )
        return

    if data.startswith("bsetup_subj_"):
        key = data.replace("bsetup_subj_", "")

        if key == "all":
            context.application.create_task(
                _battle_setup(
                    context, user, query.message,
                    setup["time_per_q"], None, None,
                )
            )
            return

        try:
            subject = _subject_pool()[int(key)]
        except (ValueError, IndexError):
            await query.answer("Invalid subject.", show_alert=True)
            return

        setup["subject"] = subject

        await query.edit_message_text(
            f"📚 Subject selected: {subject}\n\nChoose your battle type:",
            reply_markup=_subject_action_keyboard(),
        )


# ---------------------------------------------------------
# TOPIC TEXT HANDLER
# ---------------------------------------------------------

async def battle_topic_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    setup = context.user_data.get("battle_setup")

    if not setup or not setup.get("await_topic"):
        return

    if update.effective_user.id != setup["challenger_id"]:
        return

    topic = (update.effective_message.text or "").strip()[:100]

    if not topic:
        return

    setup["await_topic"] = False

    msg = await update.effective_message.reply_text(
        f"🤖 Gemini is generating questions on:\n"
        f"📚 {setup.get('subject') or 'All subjects'}\n"
        f"✍️ {topic}\n\n"
        "Please wait..."
    )

    context.application.create_task(
        _battle_setup(
            context, update.effective_user, msg,
            setup["time_per_q"], setup.get("subject"), topic,
        )
    )

    raise ApplicationHandlerStop


# ---------------------------------------------------------
# CREATE BATTLE INVITATION
# ---------------------------------------------------------

async def _battle_setup(context, user, msg, time_per_q, subject, topic):
    try:
        setup = context.user_data.get("battle_setup")

        if not setup or setup["challenger_id"] != user.id:
            await msg.edit_text("❌ Setup expired. Please start again.")
            return

        target = setup["target"]
        target_id = setup["target_id"]
        chat_id = setup["chat_id"]
        mode = setup["mode"]
        challenger_name = setup["challenger_name"]

        description = topic or subject or "Mixed subjects"

        await msg.edit_text(
            f"🤖 Gemini is preparing {BATTLE_QUESTIONS} questions on {description}.\n\n"
            "Please wait while the questions are generated..."
        )

        question_ids, used_gemini = await _prepare_battle_questions(
            subject=subject, topic=topic
        )

        if not question_ids:
            await msg.edit_text("❌ No questions could be generated.")
            return

        ai_note = (
            "🤖 Gemini AI generated questions"
            if used_gemini else "📚 Matching saved questions"
        )
        if subject:
            ai_note += f" · {subject}"
        if topic:
            ai_note += f" · {topic}"

        battle_id = f"btl{uuid.uuid4().hex[:8]}"

        await create_battle({
            "battle_id": battle_id,
            "challenger": user.id,
            "opponent": 0,
            "chat_id": chat_id,
            "question_ids": question_ids,
        })

        scores = {
            str(user.id): 0,
            "_challenger_name": challenger_name,
            "_mode": mode,
            "_time_per_q": time_per_q,
            "_target_id": target_id,
        }

        await update_battle(battle_id, scores=json.dumps(scores))

        context.user_data.pop("battle_setup", None)

        mode_tag = "🔒 Private battle" if mode == "dm" else "👥 Group battle"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "⚔️ Accept Battle!",
                callback_data=f"accept_battle_{battle_id}",
            )
        ]])

        invite_text = (
            f"⚔️ *{_md(challenger_name)}* challenges @{_md(target)}!\n\n"
            f"❓ Questions: *{len(question_ids)}*\n"
            f"⏱ Time per question: *{time_per_q}s*\n"
            f"📚 {_md(ai_note)}\n"
            f"🎮 {mode_tag}\n\n"
            f"Accept within {BATTLE_INVITE_TIMEOUT} seconds."
        )

        await msg.edit_text(
            invite_text, parse_mode="Markdown", reply_markup=keyboard
        )

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=invite_text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Could not send battle invitation to opponent.")

    except GeminiError as e:
        logger.exception("Battle question generation failed.")
        await msg.edit_text(f"❌ Could not prepare this battle.\n\n{e}")

    except Exception as e:
        logger.exception("Battle setup failed.")
        try:
            await msg.edit_text(f"❌ Battle setup failed: {e}")
        except Exception:
            pass


# ---------------------------------------------------------
# ACCEPT BATTLE
# ---------------------------------------------------------

async def accept_battle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    battle_id = query.data.replace("accept_battle_", "")

    try:
        battle = await get_battle(battle_id)

        if not battle:
            await query.answer("Battle not found.", show_alert=True)
            return

        if battle["status"] != "pending":
            await query.answer("Battle already started or expired.", show_alert=True)
            return

        if battle["challenger"] == user.id:
            await query.answer("You cannot accept your own battle.", show_alert=True)
            return

        scores = _json_load(battle["scores"])
        target_id = scores.get("_target_id")

        if target_id and int(target_id) != user.id:
            await query.answer("This invitation is not for you.", show_alert=True)
            return

        await query.answer("Battle accepted!")

        challenger_name = scores.pop("_challenger_name", "Challenger")
        scores.pop("_mode", None)
        scores.pop("_target_id", None)

        # FIX: keep _time_per_q in scores (was .pop) so later questions
        # still use the timer the players picked.
        time_per_q = int(scores.get("_time_per_q", 30))

        scores[str(user.id)] = 0
        scores[str(battle["challenger"])] = scores.get(str(battle["challenger"]), 0)

        await update_battle(
            battle_id,
            opponent=user.id,
            status="active",
            current_q=0,
            scores=json.dumps(scores),
            answer_times=json.dumps({}),
        )

        question_ids = list(battle["question_ids"] or [])

        if not question_ids:
            await update_battle(battle_id, status="finished")
            await query.edit_message_text("❌ This battle has no questions.")
            return

        started_text = (
            "⚔️ *BATTLE STARTED!*\n\n"
            f"🔵 {_md(challenger_name)} vs 🔴 {_md(user.first_name or 'Opponent')}\n\n"
            f"❓ {len(question_ids)} questions\n"
            f"⏱ {time_per_q} seconds per question\n\n"
            "Questions will arrive in your private chat."
        )

        await query.edit_message_text(started_text, parse_mode="Markdown")

        try:
            await context.bot.send_message(
                chat_id=battle["challenger"],
                text=started_text,
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Could not notify challenger.")

        await asyncio.sleep(1)

        await _start_battle_question(context, battle_id, 0, question_ids, time_per_q)

    except Exception as e:
        logger.exception("Accept battle failed.")
        try:
            await query.edit_message_text(f"❌ Could not start battle: {e}")
        except Exception:
            pass


# ---------------------------------------------------------
# SEND QUESTION TO ONE PLAYER
# ---------------------------------------------------------

async def _send_dm_question(context, user_id, battle_id, q_index, question_ids, time_per_q):
    mcq_id = question_ids[q_index]
    mcq = await get_mcq_by_id(mcq_id)

    if not mcq:
        raise ValueError(f"Question {mcq_id} not found.")

    letters = ["A", "B", "C", "D"]
    options = [mcq["option_a"], mcq["option_b"], mcq["option_c"], mcq["option_d"]]
    total = len(question_ids)

    text = (
        f"⚔️ *BATTLE — Q{q_index + 1}/{total}*\n"
        f"⏱ You have *{time_per_q} seconds*\n"
    )

    if mcq["subject"]:
        text += f"📚 {_md(mcq['subject'])}\n"
    if mcq["topic"]:
        text += f"📝 {_md(mcq['topic'])}\n"

    text += "\n" + _md(mcq["question"]) + "\n\n"

    for i, option in enumerate(options):
        text += f"*{letters[i]}.* {_md(option)}\n"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            letters[i],
            callback_data=_ans_cb(letters[i], mcq["id"], battle_id, q_index),
        )
        for i in range(4)
    ]])

    await context.bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------
# START A SHARED QUESTION TIMER
# ---------------------------------------------------------

async def _start_battle_question(context, battle_id, q_index, question_ids, time_per_q):
    battle = await get_battle(battle_id)

    if not battle or battle["status"] != "active":
        return

    if int(battle["current_q"]) != q_index:
        return

    _battle_q_starts[f"{battle_id}|{q_index}"] = datetime.utcnow().timestamp()

    try:
        await _send_dm_question(
            context, battle["challenger"], battle_id, q_index, question_ids, time_per_q
        )
        await _send_dm_question(
            context, battle["opponent"], battle_id, q_index, question_ids, time_per_q
        )
    except Exception:
        logger.exception("Could not send question to both players.")
        return

    task_key = f"{battle_id}|{q_index}"
    old_task = _countdown_tasks.get(task_key)

    if old_task and not old_task.done():
        old_task.cancel()

    _countdown_tasks[task_key] = asyncio.create_task(
        _question_timeout(context, battle_id, q_index, question_ids, time_per_q)
    )


# ---------------------------------------------------------
# SHARED TIMEOUT
# ---------------------------------------------------------

async def _question_timeout(context, battle_id, q_index, question_ids, time_per_q):
    try:
        await asyncio.sleep(time_per_q)

        lock = _get_battle_lock(battle_id)

        async with lock:
            battle = await get_battle(battle_id)

            if not battle:
                return
            if battle["status"] != "active":
                return
            if int(battle["current_q"]) != q_index:
                return

            answer_times = _json_load(battle["answer_times"])

            for uid in (battle["challenger"], battle["opponent"]):
                key = _answer_key(q_index, uid)

                if key not in answer_times:
                    answer_times[key] = {
                        "chosen": "-",
                        "correct": False,
                        "time": time_per_q,
                        "xp": 0,
                        "timed_out": True,
                    }

                    try:
                        await context.bot.send_message(
                            chat_id=uid,
                            text=(
                                f"⏰ Time's up for question {q_index + 1}!\n"
                                "No answer recorded. 0 battle points."
                            ),
                        )
                    except Exception:
                        logger.exception("Could not send timeout notification.")

            await update_battle(battle_id, answer_times=json.dumps(answer_times))

        await _advance_battle(
            context, battle_id, q_index, question_ids, time_per_q, force=True
        )

    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Shared question timeout failed.")


# ---------------------------------------------------------
# ADVANCE TO NEXT QUESTION
# ---------------------------------------------------------

async def _advance_battle(context, battle_id, q_index, question_ids, time_per_q, force=False):
    lock = _get_battle_lock(battle_id)
    next_q = None

    async with lock:
        battle = await get_battle(battle_id)

        if not battle:
            return
        if battle["status"] != "active":
            return
        if int(battle["current_q"]) != q_index:
            return

        answer_times = _json_load(battle["answer_times"])

        challenger_key = _answer_key(q_index, battle["challenger"])
        opponent_key = _answer_key(q_index, battle["opponent"])

        both_answered = (
            challenger_key in answer_times and opponent_key in answer_times
        )

        if not both_answered and not force:
            return

        next_q = q_index + 1

        await update_battle(battle_id, current_q=next_q)

        task = _countdown_tasks.pop(f"{battle_id}|{q_index}", None)

        if task and task is not asyncio.current_task():
            task.cancel()

    if next_q < len(question_ids):
        await _start_battle_question(context, battle_id, next_q, question_ids, time_per_q)
    else:
        await _finish_battle(context, battle_id)


# ---------------------------------------------------------
# BATTLE CALLBACK ROUTER
# ---------------------------------------------------------

async def battle_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data == "battle_menu":
        await battle_handler(update, context)
        return

    if data.startswith("ba|"):
        await _handle_battle_answer(update, context)
        return


# ---------------------------------------------------------
# HANDLE PLAYER ANSWER
# ---------------------------------------------------------

async def _handle_battle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    try:
        chosen, mcq_id, battle_id, q_index = _parse_ans_cb(query.data)
    except Exception:
        await query.answer("Invalid answer.", show_alert=True)
        return

    lock = _get_battle_lock(battle_id)

    is_correct = False
    time_taken = 0
    xp = 0
    both_answered = False
    mcq = None

    try:
        async with lock:
            battle = await get_battle(battle_id)

            if not battle:
                await query.answer("Battle not found.", show_alert=True)
                return

            if battle["status"] != "active":
                await query.answer("This battle is no longer active.", show_alert=True)
                return

            if user.id not in (battle["challenger"], battle["opponent"]):
                await query.answer("You are not part of this battle.", show_alert=True)
                return

            if int(battle["current_q"]) != q_index:
                await query.answer("This question has expired.", show_alert=True)
                return

            if list(battle["question_ids"])[q_index] != mcq_id:
                await query.answer("Invalid question.", show_alert=True)
                return

            answer_times = _json_load(battle["answer_times"])
            player_key = _answer_key(q_index, user.id)

            if player_key in answer_times:
                await query.answer("You have already answered.", show_alert=True)
                return

            mcq = await get_mcq_by_id(mcq_id)

            if not mcq:
                await query.answer("Question not found.", show_alert=True)
                return

            is_correct = chosen == mcq["correct"]

            start_time = _battle_q_starts.get(
                f"{battle_id}|{q_index}", datetime.utcnow().timestamp()
            )

            time_taken = max(0, datetime.utcnow().timestamp() - start_time)

            xp = XP_CORRECT if is_correct else XP_WRONG

            if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS:
                xp += XP_SPEED_BONUS

            answer_times[player_key] = {
                "chosen": chosen,
                "correct": is_correct,
                "time": round(time_taken, 2),
                "xp": xp,
                "timed_out": False,
            }

            scores = _json_load(battle["scores"])
            uid_string = str(user.id)
            scores[uid_string] = scores.get(uid_string, 0) + max(0, xp)

            await update_battle(
                battle_id,
                answer_times=json.dumps(answer_times),
                scores=json.dumps(scores),
            )

            other_id = (
                battle["opponent"]
                if user.id == battle["challenger"]
                else battle["challenger"]
            )

            other_key = _answer_key(q_index, other_id)
            both_answered = player_key in answer_times and other_key in answer_times

        await query.answer("Answer recorded!")

        try:
            await record_answer(
                user.id, mcq_id, chosen, is_correct, time_taken, battle_id
            )
            await add_xp(user.id, xp)
        except Exception:
            logger.exception("Could not save answer history or XP.")

        icon = "✅" if is_correct else "❌"

        speed_tag = (
            "⚡ Speed bonus!\n"
            if is_correct and time_taken <= BATTLE_BONUS_SPEED_SECONDS
            else ""
        )

        correct_text = {
            "A": mcq["option_a"],
            "B": mcq["option_b"],
            "C": mcq["option_c"],
            "D": mcq["option_d"],
        }.get(mcq["correct"], mcq["correct"])

        result_message = (
            f"{icon} *{'Correct!' if is_correct else 'Wrong!'}*\n"
            f"{speed_tag}"
            f"Your answer: *{chosen}*\n"
            f"Correct answer: *{mcq['correct']}* — {_md(correct_text)}\n"
            f"⏱ {time_taken:.1f}s\n"
            f"⚡ XP: {'+' if xp >= 0 else ''}{xp}\n"
            + (f"\n💡 {_md(mcq['explanation'])}\n" if mcq["explanation"] else "")
            + (
                "\n🏁 Both players answered!"
                if both_answered
                else "\n⏳ Waiting for opponent..."
            )
        )

        try:
            await query.edit_message_text(result_message, parse_mode="Markdown")
        except Exception:
            logger.exception("Could not edit answer message.")

        if both_answered:
            battle = await get_battle(battle_id)

            if battle:
                scores = _json_load(battle["scores"])
                time_per_q = int(scores.get("_time_per_q", 30))

                await _advance_battle(
                    context, battle_id, q_index,
                    list(battle["question_ids"]), time_per_q,
                )

    except Exception as e:
        logger.exception("Battle answer handler failed.")
        try:
            await query.answer(f"Error: {e}", show_alert=True)
        except Exception:
            pass


# ---------------------------------------------------------
# FINISH BATTLE
# ---------------------------------------------------------

async def _finish_battle(context, battle_id):
    try:
        await update_battle(battle_id, status="finished")

        battle = await get_battle(battle_id)

        if not battle:
            return

        scores = _json_load(battle["scores"])

        p1_id = int(battle["challenger"])
        p2_id = int(battle["opponent"])

        p1_score = scores.get(str(p1_id), 0)
        p2_score = scores.get(str(p2_id), 0)

        try:
            p1_name = (await context.bot.get_chat(p1_id)).first_name or "Player 1"
        except Exception:
            p1_name = "Player 1"

        try:
            p2_name = (await context.bot.get_chat(p2_id)).first_name or "Player 2"
        except Exception:
            p2_name = "Player 2"

        if p1_score > p2_score:
            winner = f"🏆 *{_md(p1_name)} wins!*"
        elif p2_score > p1_score:
            winner = f"🏆 *{_md(p2_name)} wins!*"
        else:
            winner = "🤝 It's a *draw!*"

        result = (
            "⚔️ *BATTLE RESULTS*\n\n"
            f"🔵 {_md(p1_name)}: *{p1_score} points*\n"
            f"🔴 {_md(p2_name)}: *{p2_score} points*\n\n"
            f"{winner}"
        )

        for uid in (p1_id, p2_id):
            try:
                await context.bot.send_message(
                    chat_id=uid, text=result, parse_mode="Markdown"
                )
            except Exception:
                logger.exception("Could not send battle results to %s.", uid)

        for key in list(_countdown_tasks):
            if key.startswith(f"{battle_id}|"):
                task = _countdown_tasks.pop(key, None)
                if task and not task.done():
                    task.cancel()

        for key in list(_battle_q_starts):
            if key.startswith(f"{battle_id}|"):
                _battle_q_starts.pop(key, None)

        _battle_locks.pop(battle_id, None)

    except Exception:
        logger.exception("Could not finish battle.")
