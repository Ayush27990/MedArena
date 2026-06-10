"""Search handler — search MCQs by keyword, subject, or topic"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.database import search_mcqs
from config import SUBJECTS, DIFFICULTIES


async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        # Direct keyword search: /search keyword
        keyword = " ".join(args)
        await _do_search(update, context, keyword=keyword)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔤 By Keyword", callback_data="srch_keyword")],
            [InlineKeyboardButton("📚 By Subject", callback_data="srch_subject")],
            [InlineKeyboardButton("🎯 By Difficulty", callback_data="srch_diff")],
        ])
        msg = "🔍 *Search MCQ Database*\n\nChoose a filter:"
        if update.message:
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)


async def _do_search(update, context, keyword=None, subject=None, difficulty=None):
    results = await search_mcqs(keyword=keyword, subject=subject,
                                difficulty=difficulty, limit=5)
    if not results:
        msg = "❌ No MCQs found. Try different keywords."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        elif update.message:
            await update.message.reply_text(msg)
        return

    header = f"🔍 *Found {len(results)} MCQ(s)*\n\n"
    if update.callback_query:
        await update.callback_query.edit_message_text(header, parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text(header, parse_mode="Markdown")

    for mcq in results:
        q = mcq["question"][:100] + "..." if len(mcq["question"]) > 100 else mcq["question"]
        opts = [
            f"A. {mcq['option_a']}",
            f"B. {mcq['option_b']}",
            f"C. {mcq['option_c']}",
            f"D. {mcq['option_d']}",
        ]
        if mcq.get("option_e"):
            opts.append(f"E. {mcq['option_e']}")

        text = (
            f"❓ {q}\n\n"
            + "\n".join(opts) +
            f"\n\n||✅ Answer: *{mcq['correct']}*||\n"
            f"📚 {mcq.get('subject') or '?'} | 🎯 {mcq.get('difficulty') or '?'}"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔖 Bookmark", callback_data=f"srch_bk_{mcq['id']}"),
        ]])

        chat = update.effective_chat
        await chat.send_message(text, parse_mode="Markdown", reply_markup=kb)


async def search_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "srch_start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔤 By Keyword", callback_data="srch_keyword")],
            [InlineKeyboardButton("📚 By Subject", callback_data="srch_subject")],
            [InlineKeyboardButton("🎯 By Difficulty", callback_data="srch_diff")],
        ])
        await query.edit_message_text(
            "🔍 *Search MCQ Database*\n\nChoose a filter:",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    if data == "srch_keyword":
        context.user_data["search_mode"] = "keyword"
        await query.edit_message_text(
            "🔤 Send a keyword to search:\n\n_(e.g. 'Wilson disease', 'mitral valve', 'APTT')_",
            parse_mode="Markdown"
        )
        return

    if data == "srch_subject":
        kb = [[InlineKeyboardButton(s, callback_data=f"srch_sub_{s}")] for s in SUBJECTS[:10]]
        kb.append([InlineKeyboardButton("More ▶", callback_data="srch_sub_more")])
        await query.edit_message_text(
            "📚 Select Subject:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data == "srch_sub_more":
        kb = [[InlineKeyboardButton(s, callback_data=f"srch_sub_{s}")] for s in SUBJECTS[10:]]
        kb.append([InlineKeyboardButton("◀ Back", callback_data="srch_subject")])
        await query.edit_message_text(
            "📚 Select Subject:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("srch_sub_"):
        subject = data[9:]
        await _do_search(update, context, subject=subject)
        return

    if data == "srch_diff":
        kb = [[InlineKeyboardButton(d, callback_data=f"srch_dv_{d}")] for d in DIFFICULTIES]
        await query.edit_message_text(
            "🎯 Select Difficulty:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("srch_dv_"):
        diff = data[8:]
        await _do_search(update, context, difficulty=diff)
        return

    if data.startswith("srch_bk_"):
        mcq_id = int(data[8:])
        user = update.effective_user
        from services.database import toggle_bookmark
        added = await toggle_bookmark(user.id, mcq_id)
        await query.answer("🔖 Bookmarked!" if added else "🗑 Removed from bookmarks")
        return
