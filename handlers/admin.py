"""
Admin Panel Handler
- Approve/edit/delete MCQs
- Database statistics
- Pending review queue
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
    q = mcq["question"][:80] + "..." if len(mcq["question"]) > 80 else mcq["question"]
    return (
        f"📌 *ID {mcq['id']}*\n"
        f"❓ {q}\n\n"
        f"A. {mcq['option_a']}\n"
        f"B. {mcq['option_b']}\n"
        f"C. {mcq['option_c']}\n"
        f"D. {mcq['option_d']}\n"
        f"{'E. ' + mcq['option_e'] + chr(10) if mcq.get('option_e') else ''}"
        f"\n✅ Correct: *{mcq['correct']}*\n"
        f"📚 {mcq.get('subject') or '?'} | 🎯 {mcq.get('difficulty') or '?'}\n"
        f"📝 {mcq.get('topic') or '?'}"
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
            InlineKeyboardButton("📊 DB Stats", callback_data="adm_stats"),
        ],
        [
            InlineKeyboardButton("🔍 Search MCQs", callback_data="adm_search"),
        ],
    ])

    await update.message.reply_text(
        f"🛠 *Admin Panel*\n\n"
        f"📚 Total MCQs: *{stats['total']}*\n"
        f"✅ Approved: *{stats['approved']}*\n"
        f"⏳ Pending: *{stats['pending']}*\n"
        f"👥 Users: *{stats['users']}*",
        parse_mode="Markdown",
        reply_markup=kb
    )


async def pending_approval_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ Admin access only.")
        return

    pending = await get_pending_mcqs(limit=1)
    if not pending:
        await update.message.reply_text("✅ No pending MCQs — queue is empty!")
        return

    mcq = pending[0]
    context.user_data["reviewing_mcq"] = mcq["id"]

    kb = _approval_keyboard(mcq["id"])
    await update.message.reply_text(
        _mcq_preview(mcq),
        parse_mode="Markdown",
        reply_markup=kb
    )


def _approval_keyboard(mcq_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{mcq_id}"),
            InlineKeyboardButton("❌ Delete", callback_data=f"adm_delete_{mcq_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Subject", callback_data=f"adm_edit_subject_{mcq_id}"),
            InlineKeyboardButton("✏️ Edit Diff", callback_data=f"adm_edit_diff_{mcq_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Correct", callback_data=f"adm_edit_correct_{mcq_id}"),
            InlineKeyboardButton("📖 Edit Explain", callback_data=f"adm_edit_explain_{mcq_id}"),
        ],
        [
            InlineKeyboardButton("⏭ Next Pending", callback_data="adm_next_pending"),
        ],
    ])


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
                InlineKeyboardButton("📊 DB Stats", callback_data="adm_stats"),
            ],
            [
                InlineKeyboardButton("🔍 Search MCQs", callback_data="adm_search"),
            ],
        ])
        await query.edit_message_text(
            f"🛠 *Admin Panel*\n\n"
            f"📚 Total: *{stats['total']}* | ✅ Approved: *{stats['approved']}* | ⏳ Pending: *{stats['pending']}*\n"
            f"👥 Users: *{stats['users']}*",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    if data == "adm_stats":
        stats = await get_db_stats()
        await query.edit_message_text(
            f"📊 *Database Statistics*\n\n"
            f"📚 Total MCQs: *{stats['total']}*\n"
            f"✅ Approved: *{stats['approved']}*\n"
            f"⏳ Pending review: *{stats['pending']}*\n"
            f"👥 Registered users: *{stats['users']}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀ Back", callback_data="adm_panel")
            ]])
        )
        return

    if data in ["adm_pending", "adm_next_pending"]:
        pending = await get_pending_mcqs(limit=1)
        if not pending:
            await query.edit_message_text(
                "✅ No more pending MCQs!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀ Admin Panel", callback_data="adm_panel")
                ]])
            )
            return
        mcq = pending[0]
        context.user_data["reviewing_mcq"] = mcq["id"]
        await query.edit_message_text(
            _mcq_preview(mcq),
            parse_mode="Markdown",
            reply_markup=_approval_keyboard(mcq["id"])
        )
        return

    if data.startswith("adm_approve_"):
        mcq_id = int(data.split("_")[-1])
        await approve_mcq(mcq_id)
        await query.edit_message_text(
            f"✅ MCQ #{mcq_id} approved!\n\nFetch next?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Next Pending", callback_data="adm_next_pending"),
                InlineKeyboardButton("🛠 Admin Panel", callback_data="adm_panel"),
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

    # Edit subject
    if data.startswith("adm_edit_subject_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "subject"
        kb = [[InlineKeyboardButton(s, callback_data=f"adm_setval_{s}_{mcq_id}")]
              for s in SUBJECTS[:12]]
        await query.edit_message_text(
            "Select new subject:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # Edit difficulty
    if data.startswith("adm_edit_diff_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "difficulty"
        kb = [[InlineKeyboardButton(d, callback_data=f"adm_setval_{d}_{mcq_id}")]
              for d in DIFFICULTIES]
        await query.edit_message_text(
            "Select difficulty:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # Edit correct answer
    if data.startswith("adm_edit_correct_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "correct"
        kb = [
            [InlineKeyboardButton(l, callback_data=f"adm_setval_{l}_{mcq_id}")]
            for l in ["A", "B", "C", "D", "E"]
        ]
        await query.edit_message_text(
            "Select correct answer:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # Edit explanation (requires text input)
    if data.startswith("adm_edit_explain_"):
        mcq_id = int(data.split("_")[-1])
        context.user_data["editing_mcq"] = mcq_id
        context.user_data["editing_field"] = "explanation"
        context.user_data["awaiting_edit_text"] = True
        await query.edit_message_text(
            f"✏️ Send the new explanation for MCQ #{mcq_id}:\n\n_(Type and send as next message)_",
            parse_mode="Markdown"
        )
        return

    # Set value from button
    if data.startswith("adm_setval_"):
        parts = data.split("_", 3)
        # adm_setval_VALUE_MCQID
        value = parts[2]
        mcq_id = int(parts[3])
        field = context.user_data.get("editing_field", "subject")
        await update_mcq(mcq_id, field, value)
        mcq = await get_mcq_by_id(mcq_id)
        await query.edit_message_text(
            f"✅ Updated {field} → *{value}*\n\n" + _mcq_preview(mcq),
            parse_mode="Markdown",
            reply_markup=_approval_keyboard(mcq_id)
        )
        return

    if data == "adm_search":
        context.user_data["admin_search_mode"] = True
        await query.edit_message_text(
            "🔍 Send a keyword to search the MCQ database:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀ Cancel", callback_data="adm_panel")
            ]])
        )
        return


async def handle_admin_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for admin search or explanation editing."""
    user = update.effective_user
    if not _is_admin(user.id):
        return

    text = update.message.text.strip()

    # Explanation edit
    if context.user_data.get("awaiting_edit_text"):
        mcq_id = context.user_data.get("editing_mcq")
        if mcq_id:
            await update_mcq(mcq_id, "explanation", text)
            await update.message.reply_text(
                f"✅ Explanation updated for MCQ #{mcq_id}."
            )
        context.user_data.pop("awaiting_edit_text", None)
        context.user_data.pop("editing_mcq", None)
        context.user_data.pop("editing_field", None)
        return

    # Admin search
    if context.user_data.get("admin_search_mode"):
        from services.database import search_mcqs
        results = await search_mcqs(keyword=text, limit=5)
        context.user_data.pop("admin_search_mode", None)

        if not results:
            await update.message.reply_text(f"❌ No MCQs found for: *{text}*", parse_mode="Markdown")
            return

        for mcq in results:
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{mcq['id']}"),
                    InlineKeyboardButton("❌ Delete", callback_data=f"adm_delete_{mcq['id']}"),
                ],
            ])
            await update.message.reply_text(
                _mcq_preview(mcq),
                parse_mode="Markdown",
                reply_markup=kb
            )
