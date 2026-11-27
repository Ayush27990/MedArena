"""
AI MCQ drafts with admin approval - the shared core.

/generate [count] topic     (admins only, in a private chat with the bot)
  e.g.  /generate 5 Pharmacology antibiotics

EVERY draft, whatever made it (/generate, auto drafts, polls, text, images, PDFs),
works the same way:
  - tap group names to tick one or several, then "Send to selected"
  - when there are 2+ drafts, a last message sends ALL waiting drafts to the ticked groups
Nothing is posted until you tap a Send button.

Each draft also has:
  🔍 Reassess  - an AI re-solves the question and fixes a wrong answer key
  🔄 Regenerate - an AI writes a fresh question from the same source
each with "Gemini" or "Groq". Image drafts also get a "Change layout" button.

Groups to choose from: TARGET_CHAT_IDS env var (comma-separated).
If TARGET_CHAT_IDS is empty, SOURCE_CHAT_IDS is used instead.
"""

import io
import os
import time
import uuid
import random
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import ContextTypes
from config import ADMIN_IDS, SOURCE_CHAT_IDS
from services.gemini_service import (
    generate_mcqs, reassess_mcq, generate_image_mcqs, GeminiError, PROVIDER_LABELS,
)
from handlers.import_mcq import _process_mcq_dict

logger = logging.getLogger(__name__)

DEFAULT_COUNT = 5
MAX_COUNT = 10

POLL_QUESTION_LIMIT = 300
CAPTION_LIMIT = 1024
DRAFT_TTL_SECONDS = 24 * 3600
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


def pdf_text(pdf: bytes) -> str:
    """Typed text inside a PDF ('' for scanned/handwritten pages or if it can't be read)."""
    try:
        import pypdf
        return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf)).pages).strip()
    except Exception:
        logger.exception("Could not extract text from the PDF")
        return ""


# ─────────────────────────────────────────────────────────────
# Buttons
# ─────────────────────────────────────────────────────────────

def _group_rows(data: dict, selected: set, make_data) -> list[list[InlineKeyboardButton]]:
    """One tick box per group, plus 'Select all'."""
    rows = []
    for i, cid in enumerate(data["targets"]):
        mark = "✅" if cid in selected else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {_title(data, cid)}"[:60], callback_data=make_data(i))])
    if len(data["targets"]) > 1:
        mark = "✅" if len(selected) == len(data["targets"]) else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} Select ALL groups", callback_data=make_data("all"))])
    return rows


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


def _markup(batch: str, data: dict, idx: int) -> InlineKeyboardMarkup:
    item = data["items"][idx]
    rows = _group_rows(data, item["selected"], lambda g: f"gen_s|{batch}|{idx}.{g}")
    rows.append([
        InlineKeyboardButton(f"📤 Send to selected ({len(item['selected'])})", callback_data=f"gen_ok|{batch}|{idx}"),
        InlineKeyboardButton("🗑 Skip", callback_data=f"gen_no|{batch}|{idx}"),
    ])
    rows += _review_rows(batch, idx, bool(item["mcq"].get("image_file_id")))
    return InlineKeyboardMarkup(rows)


def _waiting(data: dict) -> int:
    return sum(1 for i in data["items"] if i["status"] is None)


def _panel_text(data: dict) -> str:
    return (f"📦 Send several at once: {_waiting(data)} draft(s) waiting.\n"
            "Tick the groups, then send. Skip any draft you don't want first.")


def _panel_markup(batch: str, data: dict) -> InlineKeyboardMarkup:
    rows = _group_rows(data, data["selected"], lambda g: f"gen_g|{batch}|{g}")
    rows.append([InlineKeyboardButton(
        f"📤 Send ALL waiting ({_waiting(data)}) to selected", callback_data=f"gen_all|{batch}|0")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────
# Delivering drafts (shared by /generate, /auto and anything you send the bot)
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


def prune_drafts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget finished or day-old batches."""
    drafts = _drafts(context)
    now = time.time()
    for batch in [b for b, d in drafts.items()
                  if now - d["created"] > DRAFT_TTL_SECONDS or not _waiting(d)]:
        drafts.pop(batch, None)


async def send_drafts(context: ContextTypes.DEFAULT_TYPE, admin_id: int, mcqs: list[dict], *,
                      topic: str, level: str | None, header: str,
                      source: str | None = None, hint: str = "",
                      avoid: list[str] | None = None, auto: bool = False,
                      source_image_id: str | None = None, pdf_file_id: str | None = None) -> int:
    """
    DM a batch of drafts to the admin. Returns how many were delivered.
    Raises NoTargetGroups, or the Telegram error if the admin can't be messaged at all.
    """
    targets = _target_chats()
    if not targets:
        raise NoTargetGroups()
    titles = await _group_titles(context, targets)
    prune_drafts(context)

    only_one = set(targets) if len(targets) == 1 else set()
    items = [{"mcq": m, "status": None, "msg_id": None, "note": "", "busy": False,
              "selected": set(only_one), "sent_to": set()} for m in mcqs]
    batch = uuid.uuid4().hex[:6]
    data = {
        "admin": admin_id, "chat_id": admin_id, "topic": topic, "level": level,
        "source": source, "hint": hint, "avoid": list(avoid or []),
        "source_image_id": source_image_id, "pdf_file_id": pdf_file_id,
        "items": items, "targets": targets, "titles": titles,
        "selected": set(only_one), "panel_msg_id": None,
        "auto": auto, "created": time.time(), "header": header,
    }
    _drafts(context)[batch] = data

    delivered, shown_images = 0, set()
    for i, item in enumerate(items):
        image_id = item["mcq"].get("image_file_id")
        try:
            if image_id and image_id not in shown_images:
                try:
                    await context.bot.send_photo(admin_id, image_id)
                    shown_images.add(image_id)
                except Exception:
                    logger.exception("Could not show the draft image to the admin")
            sent = await context.bot.send_message(
                admin_id, header + _preview_text(i, len(items), item["mcq"]),
                reply_markup=_markup(batch, data, i))
        except Exception:
            if not delivered:
                _drafts(context).pop(batch, None)
                raise
            logger.exception("Could not deliver draft %s of %s", i + 1, len(items))
            item["status"] = "undelivered"
            continue
        item["msg_id"] = sent.message_id
        delivered += 1

    if delivered > 1:
        try:
            panel = await context.bot.send_message(
                admin_id, _panel_text(data), reply_markup=_panel_markup(batch, data))
            data["panel_msg_id"] = panel.message_id
        except Exception:
            logger.exception("Could not send the send-all panel")
    return delivered


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

    if not _target_chats():
        await msg.reply_text(
            "No group is set up to receive MCQs yet. Add TARGET_CHAT_IDS "
            "(your group IDs, comma-separated) in the host's environment variables and restart."
        )
        return

    working = await msg.reply_text(f"🤖 Asking Gemini for {count} MCQs on “{topic}”…")
    try:
        mcqs, _ = await generate_mcqs(topic, count)
    except GeminiError as e:
        await working.edit_text(f"❌ {e}")
        return
    except Exception:
        logger.exception("generate_mcqs crashed")
        await working.edit_text("❌ Something went wrong while generating. Please try again.")
        return

    await working.edit_text(
        f"✅ {len(mcqs)} draft(s) ready.\nNothing is posted until you tap a Send button.")
    await send_drafts(context, msg.chat_id, mcqs, topic=topic, level=None, header="")


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


async def _regenerate(context, data: dict, mcq: dict, provider: str) -> tuple[dict, str]:
    """A fresh question from the same source (image + caption, PDF, text or topic)."""
    label = PROVIDER_LABELS[provider]
    note = f"🔄 New question written by {label}"
    avoid = [i["mcq"]["question"] for i in data["items"]] + data.get("avoid", [])
    image_id = mcq.get("image_file_id") or data.get("source_image_id")

    if image_id:
        image = await download_file(context.bot, image_id)
        res = (await generate_image_mcqs(
            image, 1, hint=data.get("hint", ""), level=data.get("level"),
            avoid=avoid, provider=provider))[0]
        new = {k: res[k] for k in ("question", "options", "correct", "explanation", "subject", "topic")}
        if res["uses_image"]:
            new["image_file_id"] = image_id
            new["layout"] = pick_layout(new["question"])
        return new, note

    source, pdf = data.get("source"), None
    if data.get("pdf_file_id"):
        raw = await download_file(context.bot, data["pdf_file_id"])
        if provider == "gemini":
            pdf = raw
        else:
            text = pdf_text(raw)
            if len(text) >= 200:
                # a random 4000-character window, so repeats don't always use page 1
                start = random.randint(0, max(0, len(text) - 4000))
                source = text[start:start + 4000]
            else:
                source = f"{mcq['question']}\n{mcq.get('explanation') or ''}"
                note += " (it can't read these PDF pages, so it used the same concept)"

    mcqs, _ = await generate_mcqs(
        data["topic"], 1, level=data.get("level"), avoid=avoid, source=source,
        provider=provider, pdf=pdf, hint=data.get("hint", ""))
    new = dict(mcqs[0])
    for k in ("subject", "topic"):
        if mcq.get(k):
            new[k] = mcq[k]
    return new, note


async def _review_action(q, context, batch: str, data: dict, action: str, idx: int):
    item = data["items"][idx]
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
    doing = "Reassessing" if is_reassess else "Regenerating"

    item["busy"] = True
    try:
        await q.answer(f"{doing} with {label}…")
        await _render(context, data, batch, idx, extra=f"⏳ {doing} with {label}…")
        if is_reassess:
            image = None
            if mcq.get("image_file_id"):
                image = await download_file(context.bot, mcq["image_file_id"])
            fixed, ai_note = await reassess_mcq(mcq, provider=provider, image=image, hint=data.get("hint", ""))
            new = {**mcq, **fixed}
            if fixed["correct"] != mcq["correct"]:
                head = f"🔍 {label} re-checked: ⚠️ answer changed {mcq['correct']} → {fixed['correct']}"
            elif fixed["question"] != mcq["question"] or fixed["options"] != mcq["options"]:
                head = f"🔍 {label} re-checked: question/options were corrected"
            else:
                head = f"🔍 {label} re-checked: ✅ answer looks correct"
            item["note"] = head + (f"\n{ai_note}" if ai_note else "")
        else:
            new, item["note"] = await _regenerate(context, data, mcq, provider)
        item["mcq"] = new
    except GeminiError as e:
        item["note"] = f"⚠️ {label} could not do that: {e}"
    except Exception:
        logger.exception("review action crashed")
        item["note"] = f"⚠️ {label} failed. Please try again."
    finally:
        item["busy"] = False
    await _render(context, data, batch, idx)


async def _send_item(context, data: dict, batch: str, idx: int, chats: list[int], admin_id: int) -> bool:
    """Post one draft to the chosen groups. True = every chosen group got it."""
    item = data["items"][idx]
    todo = [c for c in chats if c not in item["sent_to"]]
    item["busy"] = True                      # set before any await: blocks double taps
    try:
        _, failed = await _post_to_groups(context, item["mcq"], admin_id, todo)
    finally:
        item["busy"] = False
    item["sent_to"] |= {c for c in todo if c not in failed}
    sent_names = ", ".join(_title(data, c) for c in data["targets"] if c in item["sent_to"])

    if not failed:
        item["status"] = "sent"
        await _clear_buttons(context, data["chat_id"], item["msg_id"],
                             _preview_text(idx, len(data["items"]), item["mcq"]) + f"\n\n✅ Sent to {sent_names}")
        return True

    # Keep the buttons, with only the failed groups ticked, so a retry never posts twice.
    item["selected"] = set(failed)
    extra = f"⚠️ Could not post to: {', '.join(_title(data, c) for c in failed)}. Is the bot allowed to post polls there?"
    if sent_names:
        extra = f"✅ Already sent to {sent_names}\n" + extra
    await _render(context, data, batch, idx, extra=extra)
    return False


async def _update_panel(context, data: dict, batch: str) -> None:
    if not data.get("panel_msg_id"):
        return
    try:
        if _waiting(data):
            await context.bot.edit_message_text(
                chat_id=data["chat_id"], message_id=data["panel_msg_id"],
                text=_panel_text(data), reply_markup=_panel_markup(batch, data))
        else:
            await context.bot.edit_message_text(
                chat_id=data["chat_id"], message_id=data["panel_msg_id"],
                text="📦 All drafts in this batch are done.")
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"Could not update the panel: {e}")


def _toggle(data: dict, selected: set, which: str) -> bool:
    if which == "all":
        if len(selected) == len(data["targets"]):
            selected.clear()
        else:
            selected.update(data["targets"])
        return True
    if which.isdigit() and int(which) < len(data["targets"]):
        selected.symmetric_difference_update({data["targets"][int(which)]})
        return True
    return False


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
    admin_id = q.from_user.id

    # ── Panel: tick groups / send every waiting draft ──
    if action == "gen_g":
        _toggle(data, data["selected"], idx_raw)
        await q.answer()
        await _update_panel(context, data, batch)
        return

    if action == "gen_all":
        chats = [c for c in data["targets"] if c in data["selected"]]
        if not chats:
            await q.answer("Tick at least one group first.", show_alert=True)
            return
        if data.get("sending_all"):
            await q.answer("Already sending…")
            return
        data["sending_all"] = True
        sent_n, problems = 0, 0
        try:
            await q.answer("Sending all…")
            for i, item in enumerate(items):
                if item["status"] or item["busy"]:
                    continue
                if await _send_item(context, data, batch, i, chats, admin_id):
                    sent_n += 1
                else:
                    problems += 1
        finally:
            data["sending_all"] = False
        await _update_panel(context, data, batch)
        summary = f"✅ Sent {sent_n} MCQ(s) to {len(chats)} group(s)."
        if problems:
            summary += f"\n⚠️ {problems} draft(s) had a problem. See the note under each one."
        await context.bot.send_message(data["chat_id"], summary)
        return

    # ── Buttons under one draft ──
    which = ""
    if action == "gen_s":
        idx_raw, _, which = idx_raw.partition(".")
    if not idx_raw.isdigit() or int(idx_raw) >= len(items):
        await q.answer()
        return
    idx = int(idx_raw)
    item = items[idx]
    if item["status"]:
        await q.answer("Already handled.")
        return
    if item["busy"]:
        await q.answer("Still working on it…")
        return

    if action == "gen_s":
        _toggle(data, item["selected"], which)
        await q.answer()
        await _render(context, data, batch, idx)
        return

    if action == "gen_ok":
        chats = [c for c in data["targets"] if c in item["selected"]]
        if not chats:
            await q.answer("Tick at least one group first.", show_alert=True)
            return
        await q.answer("Sending…")
        await _send_item(context, data, batch, idx, chats, admin_id)
        await _update_panel(context, data, batch)
        return

    if action == "gen_no":
        item["status"] = "skipped"
        await q.answer("Skipped")
        await _clear_buttons(context, data["chat_id"], item["msg_id"],
                             _preview_text(idx, len(items), item["mcq"]) + "\n\n🗑 Skipped")
        await _update_panel(context, data, batch)
        return

    if action in ("gen_vg", "gen_vo", "gen_rg", "gen_ro", "gen_lay"):
        await _review_action(q, context, batch, data, action, idx)
        return

    await q.answer()
