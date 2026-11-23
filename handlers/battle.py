
"""
MedArena Battle Mode — 1v1 real-time MCQ battles
Fixed invitation delivery, opponent verification, topic setup,
timeouts, duplicate answers and question progression.
"""

import json
import uuid
import random
import asyncio
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop
from telegram.error import TelegramError

from services.database import (
    get_mcqs_for_quiz, create_battle, get_battle, update_battle,
    get_mcq_by_id, record_answer, add_xp, get_user_by_username,
    insert_mcq, get_mcq_id_by_hash, search_mcqs
)
from services.gemini_service import generate_mcqs, GeminiError
from handlers.import_mcq import _make_hash
from config import (
    BATTLE_INVITE_TIMEOUT, XP_CORRECT,
    XP_SPEED_BONUS, BATTLE_BONUS_SPEED_SECONDS,
    XP_WRONG, SUBJECTS
)

logger = logging.getLogger(__name__)

TIMER_OPTIONS = [15, 20, 30, 45, 60]
BATTLE_QUESTIONS = 10
BATTLE_EXPLANATION_CHARS = 400

# In-process locks prevent simultaneous updates from overwriting one another.
_battle_locks = {}
_battle_starts = {}
_battle_tasks = {}


def _lock(battle_id):
    if battle_id not in _battle_locks:
        _battle_locks[battle_id] = asyncio.Lock()
    return _battle_locks[battle_id]


def _json(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _md(text):
    out = str(text or "")
    for ch in ("\\", "_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def _subject_pool():
    return [s for s in SUBJECTS if s != "Other"]


def _subject_keyboard():
    pool = _subject_pool()
    rows = [
        [InlineKeyboardButton(
            "🎲 Mixed (all subjects)",
            callback_data="bsetup_subj_all"
        )],
        [InlineKeyboardButton(
            "✍️ Type my own topic",
            callback_data="bsetup_typeany"
        )]
    ]

    for i in range(0, len(pool), 2):
        rows.append([
            InlineKeyboardButton(
                pool[j][:24],
                callback_data=f"bsetup_subj_{j}"
            )
            for j in range(i, min(i + 2, len(pool)))
        ])

    return InlineKeyboardMarkup(rows)


def _subject_action_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⭐ Whole subject (high yield)",
            callback_data="bsetup_whole"
        )],
        [InlineKeyboardButton(
            "✍️ Type a topic in this subject",
            callback_data="bsetup_typesub"
        )]
    ])


def _ans_cb(letter, mcq_id, battle_id, q_index):
    return f"ba|{letter}|{mcq_id}|{battle_id}|{q_index}"


def _parse_ans_cb(data):
    _, letter, mcq_id, battle_id, q_index = data.split("|")
    return letter, int(mcq_id), battle_id, int(q_index)


async def _gemini_battle_question_ids(
    count=BATTLE_QUESTIONS,
    subject=None,
    topic=None
):
    pool = _subject_pool()

    if topic:
        try:
            avoid = [
                r["question"]
                for r in await search_mcqs(keyword=topic, limit=25)
            ]
        except Exception:
            avoid = []

        scope = f"{subject} - {topic}" if subject else topic

        mcqs, _ = await generate_mcqs(
            "Very high-yield medical exam questions on: " + scope +
            ". Cover different important facts and clinical points.",
            count,
            avoid=avoid,
            high_yield=True,
            expl_chars=BATTLE_EXPLANATION_CHARS
        )

        if subject:
            for m in mcqs:
                m["subject"] = subject

    elif subject:
        try:
            avoid = [
                r["question"]
                for r in await search_mcqs(subject=subject, limit=25)
            ]
        except Exception:
            avoid = []

        mcqs, _ = await generate_mcqs(
            f"{subject}: very high-yield questions on different topics",
            count,
            avoid=avoid,
            high_yield=True,
            expl_chars=BATTLE_EXPLANATION_CHARS
        )

        for m in mcqs:
            m["subject"] = subject

    else:
        subjects = random.sample(
            pool, min(count, len(pool))
        )

        mcqs, _ = await generate_mcqs(
            "Very high-yield medical exam questions across subjects",
            len(subjects),
            subjects=subjects,
            high_yield=True,
            expl_chars=BATTLE_EXPLANATION_CHARS
        )

    ids = []

    for m in mcqs:
        opts = m["options"]

        if len(opts) < 4:
            continue

        correct = str(m["correct"]).upper()
        if correct not in ("A", "B", "C", "D"):
            continue

        h = _make_hash(m["question"], opts[0])

        new_id = await insert_mcq({
            "question": m["question"],
            "option_a": opts[0],
            "option_b": opts[1],
            "option_c": opts[2],
            "option_d": opts[3],
            "correct": correct,
            "explanation": m.get("explanation"),
            "subject": (
                m.get("subject")
                if m.get("subject") in pool else None
            ),
            "topic": topic or "",
            "difficulty": "NEET PG",
            "source_type": "gemini_battle",
            "source_chat": 0,
            "imported_by": 0,
            "approved": True,
            "hash": h
        })

        mcq_id = new_id or await get_mcq_id_by_hash(h)

        if mcq_id:
            ids.append(mcq_id)

    return ids[:count]


async def battle_handler(update, context):
    try:
        if update.callback_query:
            await update.callback_query.answer()

        await _start_battle_setup(update, context, "dm")

    except Exception as e:
        logger.exception("Battle handler error")
        await update.effective_message.reply_text(
            f"❌ Battle error: {e}"
        )


async def group_battle_handler(update, context):
    try:
        await _start_battle_setup(update, context, "group")
    except Exception:
        logger.exception("Group battle handler error")
        await update.effective_message.reply_text(
            "❌ Could not start group battle setup."
        )


async def _start_battle_setup(update, context, mode):
    user = update.effective_user
    args = context.args or []

    if not args:
        command = "battle" if mode == "dm" else "groupbattle"
        await update.effective_message.reply_text(
            f"⚔️ Battle Mode\n\n"
            f"Usage: /{command} @username\n\n"
            f"Example: /{command} @Ayush27990"
        )
        return

    target = args[0].lstrip("@").strip()

    if not target:
        await update.effective_message.reply_text(
            "❌ Please provide a valid username."
        )
        return

    opponent = await get_user_by_username(target)

    if not opponent:
        await update.effective_message.reply_text(
            f"❌ @{target} is not registered with MedArena.\n\n"
            "Ask them to open @MedArena121_bot and send /start."
        )
        return

    target_id = int(opponent["user_id"])

    if target_id == user.id:
        await update.effective_message.reply_text(
            "❌ You cannot challenge yourself."
        )
        return

    context.user_data["battle_setup"] = {
        "mode": mode,
        "target": target,
        "target_id": target_id,
        "chat_id": update.effective_chat.id,
        "challenger_id": user.id,
        "challenger_name": user.first_name or "Challenger"
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"{t}s",
            callback_data=f"bsetup_timer_{t}"
        )
        for t in TIMER_OPTIONS
    ]])

    await update.effective_message.reply_text(
        f"⚔️ {'DM Battle 🔒' if mode == 'dm' else 'Group Battle 👥'}\n\n"
        f"Opponent: @{target}\n\n"
        "⏱ Choose seconds per question:",
        reply_markup=kb
    )


async def battle_setup_callback(update, context):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    setup = context.user_data.get("battle_setup")

    if not setup or setup["challenger_id"] != user.id:
        await query.edit_message_text(
            "❌ Setup expired. Run /battle again."
        )
        return

    data = query.data

    if data.startswith("bsetup_timer_"):
        seconds = int(data.split("_")[-1])

        if seconds not in TIMER_OPTIONS:
            return

        setup["time_per_q"] = seconds
        setup["subject"] = None
        setup["await_topic"] = False

        await query.edit_message_text(
            f"⚔️ Challenging @{setup['target']}\n"
            f"⏱ {seconds}s per question\n\n"
            "📚 Select a subject or type your own topic:",
            reply_markup=_subject_keyboard()
        )
        return

    if not setup.get("time_per_q"):
        await query.edit_message_text(
            "❌ Setup expired. Start again."
        )
        return

    if data == "bsetup_typeany":
        setup["subject"] = None
        setup["await_topic"] = True

        await query.edit_message_text(
            "✍️ Send your topic.\n\n"
            "Example: Nephrotic syndrome"
        )
        return

    if data == "bsetup_typesub":
        setup["await_topic"] = True

        await query.edit_message_text(
            f"✍️ Send a topic in {setup['subject']}."
        )
        return

    if data == "bsetup_whole":
        context.application.create_task(
            _battle_setup(
                context, user, query.message,
                setup["time_per_q"],
                setup["subject"], None
            )
        )
        return

    if data.startswith("bsetup_subj_"):
        key = data.replace("bsetup_subj_", "")

        if key == "all":
            context.application.create_task(
                _battle_setup(
                    context, user, query.message,
                    setup["time_per_q"], None, None
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
            f"📚 Subject: {subject}\n\nChoose:",
            reply_markup=_subject_action_keyboard()
        )


async def battle_topic_text_handler(update, context):
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
        f"🤖 Generating high-yield questions on {topic}..."
    )

    context.application.create_task(
        _battle_setup(
            context,
            update.effective_user,
            msg,
            setup["time_per_q"],
            setup.get("subject"),
            topic
        )
    )

    raise ApplicationHandlerStop


async def _battle_setup(
    context, user, msg, time_per_q, subject, topic
):
    try:
        setup = context.user_data.get("battle_setup")

        if not setup or setup["challenger_id"] != user.id:
            await msg.edit_text("❌ Setup expired.")
            return

        target = setup["target"]
        target_id = int(setup["target_id"])
        mode = setup["mode"]
        chat_id = setup["chat_id"]
        challenger_name = setup["challenger_name"]

        await msg.edit_text(
            "🤖 Gemini is preparing your battle questions..."
        )

        if topic:
            ai_note = f"🤖 Gemini AI · {topic}"
        elif subject:
            ai_note = f"🤖 Gemini AI · {subject}"
        else:
            ai_note = "🤖 Gemini AI · Mixed subjects"

        try:
            question_ids = await _gemini_battle_question_ids(
                subject=subject,
                topic=topic
            )
        except GeminiError:
            logger.warning("Gemini unavailable.")
            question_ids = []
        except Exception:
            logger.exception("Question generation failed.")
            question_ids = []

        if len(question_ids) < 3:
            mcqs = await get_mcqs_for_quiz(
                subject=subject,
                topic=topic,
                limit=BATTLE_QUESTIONS
            )

            if len(mcqs) < 3:
                mcqs = await get_mcqs_for_quiz(
                    limit=BATTLE_QUESTIONS
                )

            if not mcqs:
                await msg.edit_text(
                    "❌ No questions available. Try again later."
                )
                return

            question_ids = [m["id"] for m in mcqs]
            ai_note = "📚 Saved question bank"

        question_ids = question_ids[:BATTLE_QUESTIONS]

        if len(question_ids) < 3:
            await msg.edit_text(
                "❌ At least 3 questions are needed."
            )
            return

        battle_id = "btl" + uuid.uuid4().hex[:10]

        await create_battle({
            "battle_id": battle_id,
            "challenger": user.id,
            "opponent": target_id,
            "chat_id": chat_id,
            "question_ids": question_ids
        })

        scores = {
            str(user.id): 0,
            str(target_id): 0,
            "_challenger_name": challenger_name,
            "_mode": mode,
            "_time_per_q": time_per_q
        }

        await update_battle(
            battle_id,
            scores=json.dumps(scores)
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "⚔️ Accept Battle!",
                callback_data=f"accept_battle_{battle_id}"
            )
        ]])

        invite_text = (
            f"⚔️ *{_md(challenger_name)}* challenges @{_md(target)}!\n\n"
            f"❓ Questions: {len(question_ids)}\n"
            f"⏱ Time: {time_per_q}s per question\n"
            f"{ai_note}\n\n"
            f"Accept within {BATTLE_INVITE_TIMEOUT} seconds."
        )

        # IMPORTANT: Deliver the private invitation first.
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=invite_text,
                parse_mode="Markdown",
                reply_markup=kb
            )

        except TelegramError:
            logger.exception(
                "Unable to send invitation to %s", target_id
            )

            await update_battle(
                battle_id,
                status="cancelled"
            )

            await msg.edit_text(
                "❌ Telegram could not deliver the invitation.\n\n"
                "Ask your opponent to open @MedArena121_bot "
                "and send /start, then try again."
            )
            return

        context.user_data.pop("battle_setup", None)

        await msg.edit_text(
            f"✅ Challenge successfully sent to @{target}!\n\n"
            f"❓ {len(question_ids)} questions\n"
            f"⏱ {time_per_q}s per question\n\n"
            "Waiting for acceptance..."
        )

        # Expire pending invitations.
        context.application.create_task(
            _expire_battle(context, battle_id)
        )

    except Exception as e:
        logger.exception("Battle setup failed")
        try:
            await msg.edit_text(f"❌ Battle setup error: {e}")
        except Exception:
            pass


async def _expire_battle(context, battle_id):
    await asyncio.sleep(BATTLE_INVITE_TIMEOUT)

    async with _lock(battle_id):
        battle = await get_battle(battle_id)

        if battle and battle["status"] == "pending":
            await update_battle(
                battle_id,
                status="expired"
            )

            for uid in (
                int(battle["challenger"]),
                int(battle["opponent"])
            ):
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text="⌛ Battle invitation expired."
                    )
                except TelegramError:
                    pass


async def accept_battle_handler(update, context):
    query = update.callback_query
    user = update.effective_user

    await query.answer()

    battle_id = query.data.replace("accept_battle_", "")

    try:
        async with _lock(battle_id):
            battle = await get_battle(battle_id)

            if not battle:
                await query.answer(
                    "Battle not found.",
                    show_alert=True
                )
                return

            if battle["status"] != "pending":
                await query.answer(
                    "Battle is no longer available.",
                    show_alert=True
                )
                return

            # Only the invited opponent may accept.
            if int(battle["opponent"]) != user.id:
                await query.answer(
                    "This invitation is not for you!",
                    show_alert=True
                )
                return

            scores = _json(battle["scores"])

            challenger_name = scores.get(
                "_challenger_name", "Challenger"
            )

            time_per_q = int(
                scores.get("_time_per_q", 30)
            )

            question_ids = list(battle["question_ids"])

            scores[str(user.id)] = 0
            scores[str(battle["challenger"])] = 0

            await update_battle(
                battle_id,
                status="active",
                current_q=0,
                scores=json.dumps(scores)
            )

        # Remove the invitation button.
        try:
            await query.edit_message_text(
                f"⚔️ BATTLE STARTED!\n\n"
                f"{challenger_name} vs {user.first_name}\n\n"
                f"❓ {len(question_ids)} questions\n"
                f"⏱ {time_per_q}s each\n\n"
                "Check your private chat for questions."
            )
        except TelegramError:
            pass

        challenger_id = int(battle["challenger"])

        for uid in (challenger_id, user.id):
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text="⚔️ Battle accepted! Get ready..."
                )
            except TelegramError:
                logger.exception(
                    "Could not notify battle participant %s", uid
                )

        await asyncio.sleep(2)

        # Send the first question to both players.
        await _send_dm_question(
            context, challenger_id, battle_id,
            0, question_ids, time_per_q
        )

        await _send_dm_question(
            context, user.id, battle_id,
            0, question_ids, time_per_q
        )

    except Exception:
        logger.exception("Battle acceptance failed")


async def _send_dm_question(
    context, user_id, battle_id, q_index,
    question_ids, time_per_q
):
    try:
        mcq_id = question_ids[q_index]
        mcq = await get_mcq_by_id(mcq_id)

        if not mcq:
            logger.error("Missing MCQ %s", mcq_id)
            return

        key = f"{battle_id}|{q_index}"

        _battle_starts.setdefault(key, time.monotonic())

        letters = ["A", "B", "C", "D", "E"]

        options = [
            mcq["option_a"],
            mcq["option_b"],
            mcq["option_c"],
            mcq["option_d"]
        ]

        if mcq["option_e"]:
            options.append(mcq["option_e"])

        text = (
            f"⚔️ *BATTLE — Q{q_index + 1}/{len(question_ids)}*\n"
            f"⏱ You have *{time_per_q} seconds*\n\n"
            f"{_md(mcq['question'])}\n\n"
        )

        for i, option in enumerate(options):
            text += f"*{letters[i]}.* {_md(option)}\n"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                letters[i],
                callback_data=_ans_cb(
                    letters[i], mcq_id, battle_id, q_index
                )
            )
            for i in range(len(options))
        ]])

        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb
        )

        task_key = f"{battle_id}|{q_index}|{user_id}"

        old_task = _battle_tasks.pop(task_key, None)

        if old_task:
            old_task.cancel()

        _battle_tasks[task_key] = asyncio.create_task(
            _dm_timeout(
                context, user_id, battle_id,
                q_index, question_ids, time_per_q
            )
        )

    except TelegramError:
        logger.exception(
            "Could not send question to player %s", user_id
        )


async def _dm_timeout(
    context, user_id, battle_id,
    q_index, question_ids, time_per_q
):
    await asyncio.sleep(time_per_q)

    try:
        await _submit_answer(
            context,
            user_id,
            battle_id,
            q_index,
            None,
            question_ids[q_index],
            timed_out=True
        )

    except Exception:
        logger.exception("Battle timeout failed")


async def _submit_answer(
    context, user_id, battle_id, q_index,
    chosen, mcq_id, timed_out=False
):
    advance = False
    time_per_q = 30
    question_ids = []

    async with _lock(battle_id):
        battle = await get_battle(battle_id)

        if not battle or battle["status"] != "active":
            return False

        if int(battle["current_q"]) != q_index:
            return False

        if user_id not in (
            int(battle["challenger"]),
            int(battle["opponent"])
        ):
            return False

        answer_times = _json(battle["answer_times"])
        player_key = f"{q_index}_{user_id}"

        if player_key in answer_times:
            return False

        if int(battle["question_ids"][q_index]) != int(mcq_id):
            return False

        mcq = await get_mcq_by_id(mcq_id)

        if not mcq:
            return False

        elapsed = time.monotonic() - _battle_starts.get(
            f"{battle_id}|{q_index}", time.monotonic()
        )

        elapsed = min(elapsed, 999)

        correct = (
            not timed_out and chosen == mcq["correct"]
        )

        xp = XP_CORRECT if correct else XP_WRONG

        if correct and elapsed <= BATTLE_BONUS_SPEED_SECONDS:
            xp += XP_SPEED_BONUS

        answer_times[player_key] = {
            "chosen": chosen or "-",
            "correct": correct,
            "time": round(elapsed, 2),
            "xp": xp
        }

        scores = _json(battle["scores"])

        scores[str(user_id)] = (
            scores.get(str(user_id), 0) + max(xp, 0)
        )

        await update_battle(
            battle_id,
            answer_times=json.dumps(answer_times),
            scores=json.dumps(scores)
        )

        other_id = (
            int(battle["opponent"])
            if user_id == int(battle["challenger"])
            else int(battle["challenger"])
        )

        other_key = f"{q_index}_{other_id}"

        advance = other_key in answer_times

        question_ids = list(battle["question_ids"])
        time_per_q = int(scores.get("_time_per_q", 30))

        # Record XP and answer outside the lock.
        try:
            await record_answer(
                user_id,
                mcq_id,
                chosen or "-",
                correct,
                elapsed,
                battle_id
            )
            await add_xp(user_id, xp)
        except Exception:
            logger.exception("Could not record battle XP")

    # Tell the player their result.
    if timed_out:
        result = (
            f"⏰ *Time's up!* Q{q_index + 1}\n"
            "No answer recorded."
        )
    else:
        icon = "✅" if correct else "❌"

        correct_text = {
            "A": mcq["option_a"],
            "B": mcq["option_b"],
            "C": mcq["option_c"],
            "D": mcq["option_d"],
            "E": mcq["option_e"]
        }.get(mcq["correct"], mcq["correct"])

        result = (
            f"{icon} *{'Correct!' if correct else 'Wrong!'}*\n"
            f"Your answer: *{chosen}*\n"
            f"Correct: *{mcq['correct']}* — {_md(correct_text)}\n"
            f"⏱ {elapsed:.1f}s | ⚡ XP: {xp}\n"
        )

        if mcq.get("explanation"):
            result += "\n💡 " + _md(mcq["explanation"])

        result += "\n\n⏳ Waiting for opponent..."

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=result,
            parse_mode="Markdown"
        )
    except TelegramError:
        logger.exception("Could not send answer result")

    if advance:
        await _advance_battle(
            context, battle_id, q_index,
            question_ids, time_per_q
        )

    return True


async def _advance_battle(
    context, battle_id, q_index,
    question_ids, time_per_q
):
    next_q = None
    finish = False

    async with _lock(battle_id):
        battle = await get_battle(battle_id)

        if not battle or battle["status"] != "active":
            return

        if int(battle["current_q"]) != q_index:
            return

        answers = _json(battle["answer_times"])

        p1 = f"{q_index}_{battle['challenger']}"
        p2 = f"{q_index}_{battle['opponent']}"

        if p1 not in answers or p2 not in answers:
            return

        next_q = q_index + 1

        if next_q >= len(question_ids):
            await update_battle(
                battle_id,
                current_q=next_q,
                status="finished"
            )
            finish = True
        else:
            await update_battle(
                battle_id,
                current_q=next_q
            )

    # Cancel old timers.
    for uid in (
        int(battle["challenger"]),
        int(battle["opponent"])
    ):
        task = _battle_tasks.pop(
            f"{battle_id}|{q_index}|{uid}", None
        )
        if task and not task.done():
            task.cancel()

    await asyncio.sleep(1)

    if finish:
        await _finish_battle(context, battle_id)
        return

    _battle_starts[f"{battle_id}|{next_q}"] = time.monotonic()

    await _send_dm_question(
        context, int(battle["challenger"]),
        battle_id, next_q, question_ids, time_per_q
    )

    await _send_dm_question(
        context, int(battle["opponent"]),
        battle_id, next_q, question_ids, time_per_q
    )


async def _handle_battle_answer(update, context):
    query = update.callback_query
    user = update.effective_user

    try:
        chosen, mcq_id, battle_id, q_index = _parse_ans_cb(
            query.data
        )

        await query.answer()

        success = await _submit_answer(
            context,
            user.id,
            battle_id,
            q_index,
            chosen,
            mcq_id
        )

        if not success:
            await query.answer(
                "Already answered, expired, or invalid question.",
                show_alert=True
            )

    except Exception:
        logger.exception("Battle answer handler failed")
        try:
            await query.answer(
                "Error processing answer.",
                show_alert=True
            )
        except Exception:
            pass


async def battle_callback_handler(update, context):
    query = update.callback_query

    if query.data == "battle_menu":
        await battle_handler(update, context)
        return

    if query.data.startswith("ba|"):
        await _handle_battle_answer(update, context)


async def _finish_battle(context, battle_id):
    try:
        battle = await get_battle(battle_id)

        if not battle:
            return

        scores = _json(battle["scores"])

        p1_id = int(battle["challenger"])
        p2_id = int(battle["opponent"])

        p1_score = scores.get(str(p1_id), 0)
        p2_score = scores.get(str(p2_id), 0)

        try:
            p1_name = (
                await context.bot.get_chat(p1_id)
            ).first_name or "Player 1"
        except Exception:
            p1_name = "Player 1"

        try:
            p2_name = (
                await context.bot.get_chat(p2_id)
            ).first_name or "Player 2"
        except Exception:
            p2_name = "Player 2"

        if p1_score > p2_score:
            winner = f"🏆 *{_md(p1_name)} wins!*"
        elif p2_score > p1_score:
            winner = f"🏆 *{_md(p2_name)} wins!*"
        else:
            winner = "🤝 *It's a draw!*"

        result = (
            "⚔️ *BATTLE RESULTS*\n\n"
            f"🔵 {_md(p1_name)}: *{p1_score} pts*\n"
            f"🔴 {_md(p2_name)}: *{p2_score} pts*\n\n"
            f"{winner}"
        )

        for uid in (p1_id, p2_id):
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=result,
                    parse_mode="Markdown"
                )
            except TelegramError:
                logger.exception(
                    "Could not send final result to %s", uid
                )

        # Clean up in-memory state.
        for key in list(_battle_starts):
            if key.startswith(battle_id + "|"):
                _battle_starts.pop(key, None)

        _battle_locks.pop(battle_id, None)

    except Exception:
        logger.exception("Battle finalization failed")
