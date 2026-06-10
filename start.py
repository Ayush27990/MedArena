"""Start and Help handlers"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.database import upsert_user, get_db_stats
from config import ADMIN_IDS


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user.id, user.username or "", user.full_name or "")

    is_admin = user.id in ADMIN_IDS

    keyboard = [
        [
            InlineKeyboardButton("🧠 Start Quiz", callback_data="quiz_menu"),
            InlineKeyboardButton("⚔️ Battle Mode", callback_data="battle_menu"),
        ],
        [
            InlineKeyboardButton("📊 My Stats", callback_data="my_stats"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
        ],
        [
            InlineKeyboardButton("🔄 Revision", callback_data="rev_menu"),
            InlineKeyboardButton("🔖 Bookmarks", callback_data="rev_bookmarks"),
        ],
        [
            InlineKeyboardButton("🔍 Search MCQs", callback_data="srch_start"),
        ],
    ]

    if is_admin:
        keyboard.append([
            InlineKeyboardButton("🛠 Admin Panel", callback_data="adm_panel"),
        ])

    await update.message.reply_text(
        f"👋 Welcome, *{user.first_name}*\\!\n\n"
        "🏥 *MedQuiz Master* — Your NEET PG \\| INICET \\| FMGE \\| USMLE prep ecosystem\n\n"
        "📚 I turn your Telegram MCQs into an intelligent quiz database\\.\n\n"
        "Choose an option below:",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏥 *MedQuiz Master — Command Reference*\n\n"
        "*Quiz & Battle*\n"
        "/quiz — Start a quiz session\n"
        "/startquiz — Start group quiz (in group)\n"
        "/joingame \\[session\\_id\\] — Join a quiz\n"
        "/battle @username — Challenge someone\n\n"
        "*Stats & Progress*\n"
        "/stats — Your performance stats\n"
        "/leaderboard — Top scorers\n\n"
        "*Revision*\n"
        "/revision — Topic\\-wise revision quiz\n"
        "/wrongbank — Redo questions you got wrong\n"
        "/bookmarks — Your saved questions\n\n"
        "*Search*\n"
        "/search keyword — Search MCQ database\n\n"
        "*Import MCQs* \\(send in this chat\\)\n"
        "• Forward a Telegram quiz poll\n"
        "• Send MCQ as text\n"
        "• Send a PDF file\n"
        "• Send an image/screenshot\n\n"
        "*Admin*\n"
        "/admin — Admin panel\n"
        "/pending — Review pending MCQs"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")
