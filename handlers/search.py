"""
MedQuiz Master Bot - Main Entry Point
NEET PG / INICET / FMGE / USMLE Quiz Ecosystem
"""

import logging
import asyncio
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PollAnswerHandler, filters, ContextTypes
)
from config import BOT_TOKEN
from handlers.start import start_handler, help_handler
from handlers.import_mcq import (
    handle_poll, handle_text_mcq, handle_document, handle_photo
)
from handlers.quiz import (
    quiz_menu_handler, start_group_quiz, join_quiz_handler,
    quiz_callback_handler
)
from handlers.battle import (
    battle_handler, accept_battle_handler, battle_callback_handler
)
from handlers.stats import stats_handler, leaderboard_handler
from handlers.revision import (
    revision_handler, bookmarks_handler, wrong_bank_handler,
    revision_callback_handler
)
from handlers.admin import (
    admin_handler, admin_callback_handler, handle_admin_search,
    pending_approval_handler
)
from handlers.search import search_handler, search_callback_handler, handle_search_text
from services.database import init_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    await init_db()
    logger.info("Database initialized successfully")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Central text handler. Routes to search or admin depending on user state.
    FIX: Search keyword mode is checked FIRST before falling through to admin.
    """
    # If user is in keyword search mode, handle it
    handled = await handle_search_text(update, context)
    if handled:
        return

    # Otherwise pass to admin search handler
    await handle_admin_search(update, context)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Start & Help
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))

    # Quiz
    app.add_handler(CommandHandler("quiz", quiz_menu_handler))
    app.add_handler(CommandHandler("startquiz", start_group_quiz))
    app.add_handler(CommandHandler("joingame", join_quiz_handler))

    # Battle
    app.add_handler(CommandHandler("battle", battle_handler))

    # Stats & Leaderboard
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("leaderboard", leaderboard_handler))

    # Revision
    app.add_handler(CommandHandler("revision", revision_handler))
    app.add_handler(CommandHandler("bookmarks", bookmarks_handler))
    app.add_handler(CommandHandler("wrongbank", wrong_bank_handler))

    # Search
    app.add_handler(CommandHandler("search", search_handler))

    # Admin
    app.add_handler(CommandHandler("admin", admin_handler))
    app.add_handler(CommandHandler("pending", pending_approval_handler))

    # MCQ Import - polls (both from groups AND channels)
    app.add_handler(MessageHandler(filters.POLL, handle_poll))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS & filters.POLL, handle_poll))

    # MCQ Import - text (must come before generic text handler)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r'(?i)(^\d+[\.\)]|^Q[\.\)]|^Question)'),
        handle_text_mcq
    ))

    # MCQ Import - files & images
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # FIX: Single combined text handler that routes search keywords BEFORE admin
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text
    ))

    # Callback queries (inline buttons)
    app.add_handler(CallbackQueryHandler(quiz_callback_handler, pattern=r'^quiz_'))
    app.add_handler(CallbackQueryHandler(quiz_callback_handler, pattern=r'^my_stats$'))
    app.add_handler(CallbackQueryHandler(quiz_callback_handler, pattern=r'^leaderboard$'))
    app.add_handler(CallbackQueryHandler(battle_callback_handler, pattern=r'^battle_'))
    app.add_handler(CallbackQueryHandler(revision_callback_handler, pattern=r'^rev_'))
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern=r'^adm_'))
    app.add_handler(CallbackQueryHandler(search_callback_handler, pattern=r'^srch_'))
    app.add_handler(CallbackQueryHandler(accept_battle_handler, pattern=r'^accept_battle_'))

    logger.info("MedQuiz Master Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
