"""
Admin Panel Handler - FIXED
Fixes:
1. Next Pending button now sends a NEW message instead of editing (avoids "message not modified" error)
2. ADMIN_IDS variable name fixed (was ADMIN_USER_ID in Railway)
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.database import (
    get_pending_mcqs, approve_mcq, delete_mcq, update_mcq,
    get_db_stats, get_mcq_by_id
)
from config import ADMIN_IDS, SUBJECTS, DIFFICULTIES

logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _mcq_preview(mcq) -> str:
    q = mcq["question"][:120] + "..." if len(mcq["question"]) > 120 else mcq["question"]
    opts = (
        f"A. {mcq['option_a']}\n"
        f"B. {mcq['option_b']}\n"
        f"C. {mcq['option_c']}\n"
        f"D. {mcq['option_d']}\n"
    )
    if mcq.get("option_e"):
        opts += f"E. {mcq['option_e']}\n"
    return (
        f"📌 *ID {mcq['id']}*\n"
        f"❓ {q}\n\n"
        f"{opts}\n"
        f"✅ Correct: *{mcq['correct']}*\n"
        f"📚 {mcq.get('subject') or '?'} | 🎯 {mcq.get('difficulty') or '?'}\n"
        f"📝 {mcq.get('topic') or '?'}"
    )


def _approval_keyboard(mcq_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{mcq_id}"),
            InlineKeyboardButton("❌ Delete", callback_data=f"adm_delete_{mcq_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Subject", callback_data=f"adm_edit_subject_{mcq_id}"),
            InlineKeyboardButton("✏️ Difficulty", callback_data=f"adm_edit_diff_{mcq_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Correct Ans", callback_data=f"adm_edit_correct_{mcq_id}"),
            InlineKeyboardButton("📖 Explanation", callback_data=f"adm_edit_explain_{mcq_id}"),
        ],
        [
            InlineKeyboardButton("⏭ Next Pending", callback_data="adm_next_pending"),
        ],
    ])


async def _send_pending_mcq(chat_id, context, edit_message=None):
    """Send next pending MCQ. edit_message: message object to edit, or None to send new."""
    pending = await get_pending_mcqs(limit=1)
    if not pending:
        text = "✅ No more pending MCQs — queue is empty!"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Admin Panel", callback_data="adm_panel")]])
        if edit_message:
            try:
                await edit_message.edit_text(text, reply_markup=kb)
            except:
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        return

    mcq = pending[0]
    text = _mcq_preview(mcq)
    kb = _approval_keyboard(mcq["id"])

    if edit_message:
        try:
            await edit_message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except Exception as e:
            logger.warning(f"Edit failed, sending new: {e}")

    await context.bot.send_message(
        chat_id=chat_id, text=text,
        parse_mode="Markdown", reply_markup=kb
    )


async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return
    stats = await get_db_stats()
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📥 Pending ({stats['pending']})", callback_data="adm_pending"),
            InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
        ],
        [InlineKeyboardButton("🔍 Search MCQs", callback_data="adm_search")],
    ])
    await update.message.reply_text(
        f"🛠 *Admin Panel*\n\n"
        f"📚 Total: *{stats['total']}* | ✅ Approved: *{stats['approved']}* | ⏳ Pending: *{stats['pending']}*\n"
        f"👥 Users: *{stats['users']}*",
        parse_mode="Markdown", reply_markup=kb
    )


async def pending_approval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return
    await _send_pending_mcq(update.effective_chat.id, context)


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not _is_admin(user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return
    await query.answer()
    data = query.data

    if data == "adm_panel":
        stats = await get_db_stats()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"📥 Pending ({stats['pending']})", callback_data="adm_pending"),
                InlineKeyboardButton("📊 Stats", callback_data="adm_stats"),
            ],
            [InlineKeyboardButton("🔍 Search MCQs", callback_data="adm_search")],
        ])
        await query.edit_message_text(
            f"🛠 *Admin Panel*\n\nTotal: *{stats['total']}* | Approved: *{stats['approved']}* | Pending: *{stats['pending']}*\nUsers: *{stats['users']}*",
            parse_mode="Markdown", reply_markup=kb
        )
        return

    if data == "adm_stats":
        stats = await get_db_stats()
        await query.edit_message_text(
            f"📊 *Database Stats*\n\n"
            f"📚 Total MCQs: *{stats['total']}*\n"
            f"✅ Approved: *{stats['approved']}*\n"
            f"⏳ Pending: *{stats['pending']}*\n"
            f"👥 Users: *{stats['users']}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="adm_panel")]])
        )
        return

    if data in ["adm_pending", "adm_next_pending"]:
        await _send_pending_mcq(query.message.chat_id, context, edit_message=query.message)
        return

    if data.startswith("adm_approve_"):
        mcq_id = int(data.split("_")[-1])
        await approve_mcq(mcq_id)
        await query.edit_message_text(
            f"✅ MCQ #{mcq_id} approved!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Next Pending", callback_data="adm_next_pending"),
                InlineKeyboardButton("🛠 Panel", callback_data="adm_panel"),
            ]])
        )
        return

    if data.startswith("adm_delete_"):
        mcq_id = int(data.split("_")[-1])
        await delete_mcq(mcq_id)
        await query.edit_message_text(
            f"🗑 MCQ #{mcq_id} deleted.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Next Pending", callback_data="adm_next_pending"),
            ]])
        )
        return

    if data.startswith("adm_edit_subject_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "subject"
        kb = [[InlineKeyboardButton(s, callback_data=f"adm_setval_{s}|{mcq_id}")]
              for s in SUBJECTS[:12]]
        await query.edit_message_text("Select new subject:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("adm_edit_diff_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "difficulty"
        kb = [[InlineKeyboardButton(d, callback_data=f"adm_setval_{d}|{mcq_id}")]
              for d in DIFFICULTIES]
        await query.edit_message_text("Select difficulty:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("adm_edit_correct_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "correct"
        kb = [[InlineKeyboardButton(l, callback_data=f"adm_setval_{l}|{mcq_id}")]
              for l in ["A", "B", "C", "D", "E"]]
        await query.edit_message_text("Select correct answer:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("adm_edit_explain_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "explanation"
        context.user_data["awaiting_edit_text"] = True
        await query.edit_message_text(
            f"✏️ Send the new explanation for MCQ #{mcq_id} as your next message:"
        )
        return

    if data.startswith("adm_setval_"):
        # Format: adm_setval_VALUE|MCQID
        rest = data[11:]  # VALUE|MCQID
        parts = rest.rsplit("|", 1)
        if len(parts) != 2:
            return
        value, mcq_id_str = parts[0], parts[1]
        mcq_id = int(mcq_id_str)
        field = context.user_data.get("editing_field", "subject")
        await update_mcq(mcq_id, field, value)
        mcq = await get_mcq_by_id(mcq_id)
        if mcq:
            await query.edit_message_text(
                f"✅ Updated *{field}* → *{value}*\n\n" + _mcq_preview(mcq),
                parse_mode="Markdown",
                reply_markup=_approval_keyboard(mcq_id)
            )
        return

    if data == "adm_search":
        context.user_data["admin_search_mode"] = True
        await query.edit_message_text(
            "🔍 Send a keyword to search:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="adm_panel")]])
        )
        return


async def handle_admin_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        return

    text = update.message.text.strip()

    if context.user_data.get("awaiting_edit_text"):
        mcq_id = context.user_data.get("editing_mcq")
        if mcq_id:
            await update_mcq(mcq_id, "explanation", text)
            await update.message.reply_text(f"✅ Explanation updated for MCQ #{mcq_id}.")
        context.user_data.pop("awaiting_edit_text", None)
        context.user_data.pop("editing_mcq", None)
        context.user_data.pop("editing_field", None)
        return

    if context.user_data.get("admin_search_mode"):
        from services.database import search_mcqs
        results = await search_mcqs(keyword=text, limit=5)
        context.user_data.pop("admin_search_mode", None)
        if not results:
            await update.message.reply_text(f"❌ No MCQs found for: *{text}*", parse_mode="Markdown")
            return
        for mcq in results:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{mcq['id']}"),
                InlineKeyboardButton("❌ Delete", callback_data=f"adm_delete_{mcq['id']}"),
            ]])
            await update.message.reply_text(
                _mcq_preview(mcq), parse_mode="Markdown", reply_markup=kb
            )
