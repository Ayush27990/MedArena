"""
AI MCQ generator with admin approval.

/generate [count] topic     (admins only, in a private chat with the bot)
  e.g.  /generate 5 Pharmacology antibiotics

Gemini writes the MCQs, the bot shows each one to the admin as a preview,
and NOTHING is posted to a group until the admin taps "Send to group".
Approved MCQs are posted as Telegram quiz polls and also saved in the database.

Groups to choose from: TARGET_CHAT_IDS env var (comma-separated).
If TARGET_CHAT_IDS is empty, SOURCE_CHAT_IDS is used instead.
After generating, the admin taps which group(s) should receive the MCQs.
"""

import os
import uuid
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import ContextTypes
from config import ADMIN_IDS, SOURCE_CHAT_IDS
from services.gemini_service import generate_mcqs, GeminiError
from handlers.import_mcq import _process_mcq_dict

logger = logging.getLogger(__name__)

DEFAULT_COUNT = 5
MAX_COUNT = 10


def _target_chats() -> list[int]:
    raw = os.getenv("TARGET_CHAT_IDS", "")
    ids = [int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]
    return ids or list(SOURCE_CHAT_IDS)


def _drafts(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("gen_drafts", {})


def _preview_text(i: int, total: int, mcq: dict) -> str:
    lines = [f"📝 Draft {i + 1}/{total}", "", mcq["question"], ""]
    for letter, opt in zip("ABCD", mcq["options"]):
        mark = "✅ " if letter == mcq["correct"] else "▫️ "
        lines.append(f"{mark}{letter}. {opt}")
    if mcq.get("explanation"):
        lines += ["", f"💡 {mcq['explanation']}"]
    return "\n".join(lines)


def _title(data: dict, chat_id: int) -> str:
    return data["titles"].get(chat_id) or str(chat_id)


def _selector_text(data: dict) -> str:
    chosen = [_title(data, c) for c in data["targets"] if c in data["selected"]]
    shown = ", ".join(chosen) if chosen else "none yet"
    return f"📍 Which group should get these MCQs?\nTap to select (tap again to unselect).\n\nSelected: {shown}"


def _selector_markup(batch: str, data: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, cid in enumerate(data["targets"]):
        mark = "✅" if cid in data["selected"] else "▫️"
        rows.append([InlineKeyboardButton(f"{mark} {_title(data, cid)}"[:60], callback_data=f"gen_g|{batch}|{i}")])
    if len(data["targets"]) > 1:
        all_on = len(data["selected"]) == len(data["targets"])
        rows.append([InlineKeyboardButton(("✅" if all_on else "▫️") + " All groups", callback_data=f"gen_g|{batch}|all")])
    return InlineKeyboardMarkup(rows)


def _selected_chats(data: dict) -> list[int]:
    return [c for c in data["targets"] if c in data["selected"]]


def _buttons(batch: str, idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Send to group", callback_data=f"gen_ok|{batch}|{idx}"),
        InlineKeyboardButton("🗑 Skip", callback_data=f"gen_no|{batch}|{idx}"),
    ]])


async def generate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return

    if user.id not in ADMIN_IDS:
        await msg.reply_text("⛔ Only admins can generate MCQs.")
        return
    if msg.chat.type != "private":
        await msg.reply_text("Please use /generate in a private chat with me, so drafts stay private.")
        return

    args = context.args or []
    count = DEFAULT_COUNT
    if args and args[0].isdigit():
        count = max(1, min(MAX_COUNT, int(args[0])))
        args = args[1:]
    topic = " ".join(args).strip()
    if not topic:
        await msg.reply_text(
            "Tell me the topic. Examples:\n"
            "/generate Pharmacology antibiotics\n"
            "/generate 8 Cardiology heart failure\n\n"
            f"(1 to {MAX_COUNT} questions, default {DEFAULT_COUNT})"
        )
        return

    targets = _target_chats()
    if not targets:
        await msg.reply_text(
            "No group is set up to receive MCQs yet. Add TARGET_CHAT_IDS "
            "(your group IDs, comma-separated) in the host's environment variables and restart."
        )
        return

    working = await msg.reply_text(f"🤖 Asking Gemini for {count} MCQs on “{topic}”…")
    try:
        mcqs, dropped = await generate_mcqs(topic, count)
    except GeminiError as e:
        await working.edit_text(f"❌ {e}")
        return
    except Exception:
        logger.exception("generate_mcqs crashed")
        await working.edit_text("❌ Something went wrong while generating. Please try again.")
        return

    batch = uuid.uuid4().hex[:6]
    items = [{"mcq": m, "status": None, "msg_id": None} for m in mcqs]
    titles = {}
    for cid in targets:
        try:
            chat = await context.bot.get_chat(cid)
            titles[cid] = chat.title or str(cid)
        except Exception:
            titles[cid] = str(cid)
    data = {
        "admin": user.id, "chat_id": msg.chat_id, "topic": topic, "items": items,
        "targets": targets, "titles": titles,
        "selected": set(targets) if len(targets) == 1 else set(),
    }
    _drafts(context)[batch] = data

    note = f" ({dropped} unusable one(s) were left out)" if dropped else ""
    await working.edit_text(
        f"✅ {len(mcqs)} draft(s) ready{note}.\n"
        f"Nothing is posted until you tap “Send to group”."
    )
    if len(targets) > 1:
        await msg.reply_text(_selector_text(data), reply_markup=_selector_markup(batch, data))
    else:
        await msg.reply_text(f"📍 Posting will go to: {_title(data, targets[0])}")

    for i, item in enumerate(items):
        sent = await msg.reply_text(_preview_text(i, len(items), item["mcq"]), reply_markup=_buttons(batch, i))
        item["msg_id"] = sent.message_id

    if len(items) > 1:
        await msg.reply_text(
            "Or approve everything that is still waiting:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Send ALL to group", callback_data=f"gen_all|{batch}|0")
            ]])
        )


async def _post_to_groups(context: ContextTypes.DEFAULT_TYPE, mcq: dict, admin_id: int,
                          chat_ids: list[int]) -> tuple[int, list[int]]:
    """Post one MCQ as a quiz poll to the chosen groups. Returns (ok_count, failed_chat_ids)."""
    ok, failed = 0, []
    correct_idx = "ABCD".index(mcq["correct"])
    for chat_id in chat_ids:
        try:
            await context.bot.send_poll(
                chat_id=chat_id,
                question=mcq["question"],
                options=mcq["options"],
                type=Poll.QUIZ,
                correct_option_id=correct_idx,
                explanation=mcq.get("explanation") or None,
                is_anonymous=True,
            )
            ok += 1
        except Exception as e:
            logger.error(f"Could not post to {chat_id}: {e}")
            failed.append(chat_id)

    # Save to the database too (approved), so it can be used in quizzes. Never block posting on this.
    if ok:
        try:
            await _process_mcq_dict(
                {"question": mcq["question"], "options": list(mcq["options"]),
                 "correct": mcq["correct"], "explanation": mcq.get("explanation")},
                "gemini", 0, admin_id, True,
            )
        except Exception:
            logger.exception("Could not save generated MCQ to database")
    return ok, failed


async def _clear_buttons(context, chat_id: int, message_id: int | None, new_text: str | None = None):
    if not message_id:
        return
    try:
        if new_text is not None:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text)
        else:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception:
        pass


async def generate_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Admins only.", show_alert=True)
        return

    parts = q.data.split("|")
    if len(parts) != 3:
        await q.answer()
        return
    action, batch, idx_raw = parts
    data = _drafts(context).get(batch)
    if not data:
        await q.answer("These drafts expired (the bot restarted). Run /generate again.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    items = data["items"]
    chat_id = data["chat_id"]
    admin_id = q.from_user.id

    if action == "gen_g":
        if idx_raw == "all":
            if len(data["selected"]) == len(data["targets"]):
                data["selected"] = set()
            else:
                data["selected"] = set(data["targets"])
        else:
            try:
                cid = data["targets"][int(idx_raw)]
            except (ValueError, IndexError):
                await q.answer()
                return
            data["selected"] ^= {cid}
        await q.answer()
        try:
            await q.edit_message_text(_selector_text(data), reply_markup=_selector_markup(batch, data))
        except Exception:
            pass
        return

    if action in ("gen_ok", "gen_all") and not _selected_chats(data):
        await q.answer("Choose a group first (tap a group name above).", show_alert=True)
        return

    if action in ("gen_ok", "gen_no"):
        try:
            idx = int(idx_raw)
            item = items[idx]
        except (ValueError, IndexError):
            await q.answer()
            return
        if item["status"]:
            await q.answer("Already handled.")
            return

        if action == "gen_no":
            item["status"] = "skipped"
            await q.answer("Skipped")
            await _clear_buttons(context, chat_id, item["msg_id"],
                                 _preview_text(idx, len(items), item["mcq"]) + "\n\n🗑 Skipped")
            return

        await q.answer("Sending…")
        ok, failed = await _post_to_groups(context, item["mcq"], admin_id, _selected_chats(data))
        if ok:
            item["status"] = "sent"
            tail = f"\n\n✅ Sent to {ok} group(s)"
            if failed:
                tail += f"\n⚠️ Could not post to: {', '.join(map(str, failed))} (is the bot an admin there?)"
            await _clear_buttons(context, chat_id, item["msg_id"],
                                 _preview_text(idx, len(items), item["mcq"]) + tail)
        else:
            # Leave buttons so the admin can retry after fixing permissions
            await context.bot.send_message(
                chat_id, "⚠️ Could not post to any group. Make sure the bot is in the group and allowed to post polls, then tap again.")
        return

    if action == "gen_all":
        await q.answer("Sending all…")
        sent_n, skipped_fail = 0, 0
        for i, item in enumerate(items):
            if item["status"]:
                continue
            ok, failed = await _post_to_groups(context, item["mcq"], admin_id, _selected_chats(data))
            if ok:
                item["status"] = "sent"
                sent_n += 1
                await _clear_buttons(context, chat_id, item["msg_id"],
                                     _preview_text(i, len(items), item["mcq"]) + f"\n\n✅ Sent to {ok} group(s)")
            else:
                skipped_fail += 1
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        summary = f"✅ Sent {sent_n} MCQ(s) to the group(s)."
        if skipped_fail:
            summary += f"\n⚠️ {skipped_fail} could not be posted. Check the bot's permissions in the group."
        await context.bot.send_message(chat_id, summary)
        return

    await q.answer()
