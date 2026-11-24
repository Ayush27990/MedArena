"""
AI MCQ generator with admin approval.

/generate [count] topic     (admins only, in a private chat with the bot)
  e.g.  /generate 5 Pharmacology antibiotics

The AI writes the MCQs, the bot shows each one to the admin as a preview,
and NOTHING is posted to a group until the admin taps "Send to group".
Approved MCQs are posted as Telegram quiz polls and also saved in the database.

Every draft (here, in /auto, and for polls/text/images you send the bot) has:
  🔍 Reassess  - an AI re-solves the question and fixes a wrong answer key
  🔄 Regenerate - an AI writes a fresh question on the same topic / image
each with "Gemini" (same AI) or "Groq" (a different AI) so you can get a second opinion.
Image drafts also get a "Change layout" button.

This file is the shared core: auto_generate.py and source_draft.py reuse it.

Groups to choose from: TARGET_CHAT_IDS env var (comma-separated).
If TARGET_CHAT_IDS is empty, SOURCE_CHAT_IDS is used instead.
"""

import os
import time
import uuid
import random
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import ContextTypes
from config import ADMIN_IDS, SOURCE_CHAT_IDS
from services.gemini_service import (
    generate_mcqs, reassess_mcq, generate_image_mcq, GeminiError, PROVIDER_LABELS,
)
from handlers.import_mcq import _process_mcq_dict

logger = logging.getLogger(__name__)

DEFAULT_COUNT = 5
MAX_COUNT = 10

POLL_QUESTION_LIMIT = 300
CAPTION_LIMIT = 1024
CHOOSE_TEXT = "Choose the correct answer 👆"

# How an image question is posted in a group
LAYOUTS = ["before", "caption", "three"]
LAYOUT_NAMES = {
    "before": "Image → Poll (question inside the poll)",
    "caption": "Image + question as caption → Poll",
    "three": "Image → Question text → Poll",
}


class NoTargetGroups(Exception):
    """No TARGET_CHAT_IDS / SOURCE_CHAT_IDS configured."""


# ─────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────

def _target_chats() -> list[int]:
    raw = os.getenv("TARGET_CHAT_IDS", "")
    ids = [int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]
    return ids or list(SOURCE_CHAT_IDS)


def _drafts(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("gen_drafts", {})


async def download_file(bot, file_id: str) -> bytes:
    """Download a Telegram file (photo) by file_id."""
    f = await bot.get_file(file_id)
    return bytes(await f.download_as_bytearray())


def allowed_layouts(question: str) -> list[str]:
    n = len(question)
    out = []
    if n <= POLL_QUESTION_LIMIT:
        out.append("before")
    if n <= CAPTION_LIMIT:
        out.append("caption")
    out.append("three")
    return out


def pick_layout(question: str) -> str:
    """Short question: image then poll (or caption). Very long question: 3 parts."""
    return random.choice(allowed_layouts(question)[:2] if len(question) <= CAPTION_LIMIT else ["three"])


def _effective_layout(mcq: dict) -> str:
    layout = mcq.get("layout") or mcq.get("image_layout") or pick_layout(mcq["question"])
    if layout not in allowed_layouts(mcq["question"]):
        layout = "three"
    return layout


def _preview_text(i: int, total: int, mcq: dict, note: str = "") -> str:
    lines = [f"📝 Draft {i + 1}/{total}", ""]
    if mcq.get("image_file_id"):
        lines += [f"🖼 Image question · {LAYOUT_NAMES[_effective_layout(mcq)]}", ""]
    lines += [mcq["question"], ""]
    for letter, opt in zip("ABCD", mcq["options"]):
        mark = "✅ " if letter == mcq["correct"] else "▫️ "
        lines.append(f"{mark}{letter}. {opt}")
    if mcq.get("explanation"):
        lines += ["", f"💡 {mcq['explanation']}"]
    if note:
        lines += ["", note]
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


# ─────────────────────────────────────────────────────────────
# Buttons
# ─────────────────────────────────────────────────────────────

def _review_rows(batch: str, idx: int, is_image: bool) -> list[list[InlineKeyboardButton]]:
    """Reassess / Regenerate with the same AI (Gemini) or a different one (Groq)."""
    rows = [
        [InlineKeyboardButton("🔍 Reassess · Gemini", callback_data=f"gen_vg|{batch}|{idx}"),
         InlineKeyboardButton("🔄 Regenerate · Gemini", callback_data=f"gen_rg|{batch}|{idx}")],
        [InlineKeyboardButton("🔍 Reassess · Groq", callback_data=f"gen_vo|{batch}|{idx}"),
         InlineKeyboardButton("🔄 Regenerate · Groq", callback_data=f"gen_ro|{batch}|{idx}")],
    ]
    if is_image:
        rows.append([InlineKeyboardButton("🖼 Change layout", callback_data=f"gen_lay|{batch}|{idx}")])
    return rows


def _buttons(batch: str, idx: int, is_image: bool = False) -> InlineKeyboardMarkup:
    """Buttons for one draft of a /generate batch."""
    rows = [[
        InlineKeyboardButton("✅ Send to group", callback_data=f"gen_ok|{batch}|{idx}"),
        InlineKeyboardButton("🗑 Skip", callback_data=f"gen_no|{batch}|{idx}"),
    ]]
    return InlineKeyboardMarkup(rows + _review_rows(batch, idx, is_image))


def _send_buttons(batch: str, data: dict) -> InlineKeyboardMarkup:
    """Buttons for a single draft: one 'Send to <group>' per group (auto drafts, polls/text/images you send)."""
    rows = []
    for i, cid in enumerate(data["targets"]):
        rows.append([InlineKeyboardButton(f"✅ Send to {data['titles'].get(cid, cid)}"[:60],
                                          callback_data=f"agen|{batch}|{i}")])
    if len(data["targets"]) > 1:
        rows.append([InlineKeyboardButton("✅ Send to ALL groups", callback_data=f"agen|{batch}|all")])
    mcq = data["items"][0]["mcq"]
    rows += _review_rows(batch, 0, bool(mcq.get("image_file_id")))
    rows.append([InlineKeyboardButton("🗑 Skip", callback_data=f"gen_no|{batch}|0")])
    return InlineKeyboardMarkup(rows)


def _markup(batch: str, data: dict, idx: int) -> InlineKeyboardMarkup:
    if data.get("flow") == "single":
        return _send_buttons(batch, data)
    return _buttons(batch, idx, bool(data["items"][idx]["mcq"].get("image_file_id")))


# ─────────────────────────────────────────────────────────────
# Single drafts (shared by /auto and by polls / text / images you send)
# ─────────────────────────────────────────────────────────────

async def _group_titles(context: ContextTypes.DEFAULT_TYPE, targets: list[int]) -> dict:
    cache = context.bot_data.setdefault("group_titles", {})
    for cid in targets:
        if cid not in cache:
            try:
                cache[cid] = (await context.bot.get_chat(cid)).title or str(cid)
            except Exception:
                cache[cid] = str(cid)
    return {cid: cache[cid] for cid in targets}


async def send_single_draft(context: ContextTypes.DEFAULT_TYPE, admin_id: int, mcq: dict, *,
                            topic: str, level: str | None, header: str,
                            source: str | None = None, hint: str = "",
                            avoid: list[str] | None = None, auto: bool = False) -> str:
    """
    DM one draft to the admin with a 'Send to <group>' button per group.
    Image drafts: the picture is sent first, then the draft text.
    Raises NoTargetGroups, or the Telegram error if the admin can't be messaged.
    """
    targets = _target_chats()
    if not targets:
        raise NoTargetGroups()
    titles = await _group_titles(context, targets)

    batch = uuid.uuid4().hex[:6]
    data = {
        "admin": admin_id, "chat_id": admin_id, "topic": topic, "level": level,
        "source": source, "hint": hint, "avoid": list(avoid or []),
        "items": [{"mcq": mcq, "status": None, "msg_id": None, "img_msg_id": None, "note": "", "busy": False}],
        "targets": targets, "titles": titles, "selected": set(),
        "flow": "single", "auto": auto, "created": time.time(), "header": header,
    }
    _drafts(context)[batch] = data
    item = data["items"][0]

    try:
        if mcq.get("image_file_id"):
            try:
                photo = await context.bot.send_photo(admin_id, mcq["image_file_id"])
                item["img_msg_id"] = photo.message_id
            except Exception:
                logger.exception("Could not show the draft image to the admin")
        sent = await context.bot.send_message(
            admin_id, header + _preview_text(0, 1, mcq), reply_markup=_send_buttons(batch, data))
    except Exception:
        _drafts(context).pop(batch, None)
        raise
    item["msg_id"] = sent.message_id
    return batch


# ─────────────────────────────────────────────────────────────
# /generate
# ─────────────────────────────────────────────────────────────

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
    items = [{"mcq": m, "status": None, "msg_id": None, "note": "", "busy": False} for m in mcqs]
    titles = await _group_titles(context, targets)
    data = {
        "admin": user.id, "chat_id": msg.chat_id, "topic": topic, "items": items,
        "targets": targets, "titles": titles,
        "selected": set(targets) if len(targets) == 1 else set(),
        "flow": "multi", "level": None, "source": None, "hint": "", "avoid": [],
        "header": "",
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


# ─────────────────────────────────────────────────────────────
# Posting to groups
# ─────────────────────────────────────────────────────────────

async def _post_one(context, chat_id: int, mcq: dict) -> None:
    """Post one MCQ to one chat: a plain quiz poll, or an image question in the chosen layout."""
    correct_idx = "ABCD".index(mcq["correct"])
    poll_kwargs = dict(
        options=mcq["options"], type=Poll.QUIZ, correct_option_id=correct_idx,
        explanation=mcq.get("explanation") or None, is_anonymous=True,
    )
    image = mcq.get("image_file_id")
    if not image:
        await context.bot.send_poll(chat_id=chat_id, question=mcq["question"], **poll_kwargs)
        return

    layout = _effective_layout(mcq)
    if layout == "caption":            # image with the question as its caption, then the options
        await context.bot.send_photo(chat_id=chat_id, photo=image, caption=mcq["question"])
        await context.bot.send_poll(chat_id=chat_id, question=CHOOSE_TEXT, **poll_kwargs)
    elif layout == "three":            # image, then the (long) question, then the options
        await context.bot.send_photo(chat_id=chat_id, photo=image)
        await context.bot.send_message(chat_id=chat_id, text=mcq["question"])
        await context.bot.send_poll(chat_id=chat_id, question=CHOOSE_TEXT, **poll_kwargs)
    else:                              # image first, then the poll with question + options
        await context.bot.send_photo(chat_id=chat_id, photo=image)
        await context.bot.send_poll(chat_id=chat_id, question=mcq["question"], **poll_kwargs)


async def _post_to_groups(context: ContextTypes.DEFAULT_TYPE, mcq: dict, admin_id: int,
                          chat_ids: list[int]) -> tuple[int, list[int]]:
    """Post one MCQ to the chosen groups. Returns (ok_count, failed_chat_ids)."""
    ok, failed = 0, []
    for chat_id in chat_ids:
        try:
            await _post_one(context, chat_id, mcq)
            ok += 1
        except Exception as e:
            logger.error(f"Could not post to {chat_id}: {e}")
            failed.append(chat_id)

    # Save to the database too (approved), so it can be used in quizzes. Never block posting on this.
    if ok:
        try:
            await _process_mcq_dict(
                {"question": mcq["question"], "options": list(mcq["options"]),
                 "correct": mcq["correct"], "explanation": mcq.get("explanation"),
                 "image_file_id": mcq.get("image_file_id"),
                 "image_layout": _effective_layout(mcq) if mcq.get("image_file_id") else None,
                 "subject": mcq.get("subject"), "topic": mcq.get("topic")},
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


# ─────────────────────────────────────────────────────────────
# Reassess / Regenerate / Change layout
# ─────────────────────────────────────────────────────────────

async def _render(context, data: dict, batch: str, idx: int, extra: str = "", keep_buttons: bool = True):
    """Re-draw the draft message (text + buttons)."""
    item = data["items"][idx]
    note = "\n\n".join(x for x in (item.get("note"), extra) if x)
    text = data.get("header", "") + _preview_text(idx, len(data["items"]), item["mcq"], note)
    try:
        await context.bot.edit_message_text(
            chat_id=data["chat_id"], message_id=item["msg_id"], text=text,
            reply_markup=_markup(batch, data, idx) if keep_buttons else None)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"Could not redraw draft: {e}")


async def _review_action(q, context, batch: str, data: dict, action: str, idx_raw: str):
    try:
        idx = int(idx_raw)
        item = data["items"][idx]
    except (ValueError, IndexError):
        await q.answer()
        return
    if item["status"]:
        await q.answer("Already handled.")
        return
    if item.get("busy"):
        await q.answer("Still working on it…")
        return

    mcq = item["mcq"]

    if action == "gen_lay":
        if not mcq.get("image_file_id"):
            await q.answer()
            return
        options = allowed_layouts(mcq["question"])
        cur = _effective_layout(mcq)
        mcq["layout"] = options[(options.index(cur) + 1) % len(options)] if cur in options else options[0]
        await q.answer("Layout changed")
        await _render(context, data, batch, idx)
        return

    is_reassess = action[4] == "v"                       # gen_v? = reassess, gen_r? = regenerate
    provider = "gemini" if action[5] == "g" else "other"
    label = PROVIDER_LABELS[provider]
    await q.answer(f"{'Reassessing' if is_reassess else 'Regenerating'} with {label}…")

    item["busy"] = True
    await _render(context, data, batch, idx,
                  extra=f"⏳ {'Reassessing' if is_reassess else 'Regenerating'} with {label}…")
    try:
        image = None
        if mcq.get("image_file_id"):
            image = await download_file(context.bot, mcq["image_file_id"])

        if is_reassess:
            fixed, ai_note = await reassess_mcq(mcq, provider=provider, image=image)
            new = {**mcq, **fixed}
            if fixed["correct"] != mcq["correct"]:
                head = f"🔍 {label} re-checked: ⚠️ answer changed {mcq['correct']} → {fixed['correct']}"
            elif fixed["question"] != mcq["question"] or fixed["options"] != mcq["options"]:
                head = f"🔍 {label} re-checked: question/options were corrected"
            else:
                head = f"🔍 {label} re-checked: ✅ answer looks correct"
            item["note"] = head + (f"\n{ai_note}" if ai_note else "")
        elif image:
            res = await generate_image_mcq(
                image, hint=data.get("hint", ""), level=data.get("level"),
                avoid=[mcq["question"]] + data.get("avoid", []), provider=provider)
            new = {k: res[k] for k in ("question", "options", "correct", "explanation", "subject", "topic")}
            if res["uses_image"]:
                new["image_file_id"] = mcq["image_file_id"]
                new["layout"] = pick_layout(new["question"])
            item["note"] = f"🔄 New question written by {label}"
        else:
            mcqs, _ = await generate_mcqs(
                data["topic"], 1, level=data.get("level"),
                avoid=[mcq["question"]] + data.get("avoid", []),
                source=data.get("source"), provider=provider)
            new = dict(mcqs[0])
            for k in ("subject", "topic"):
                if mcq.get(k):
                    new[k] = mcq[k]
            item["note"] = f"🔄 New question written by {label}"

        item["mcq"] = new
    except GeminiError as e:
        item["note"] = f"⚠️ {label} could not do that: {e}"
    except Exception:
        logger.exception("review action crashed")
        item["note"] = f"⚠️ {label} failed. Please try again."
    finally:
        item["busy"] = False
    await _render(context, data, batch, idx)


# ─────────────────────────────────────────────────────────────
# Callbacks
# ─────────────────────────────────────────────────────────────

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
        await q.answer("These drafts expired (the bot restarted). Make a new one.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    items = data["items"]
    chat_id = data["chat_id"]
    admin_id = q.from_user.id

    if action in ("gen_vg", "gen_vo", "gen_rg", "gen_ro", "gen_lay"):
        await _review_action(q, context, batch, data, action, idx_raw)
        return

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
