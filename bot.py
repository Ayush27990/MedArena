"""
MedArena Bot — Main Entry Point
"""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

from config import BOT_TOKEN

from handlers.start import start_handler, help_handler

from handlers.import_mcq import (
    handle_poll,
    handle_text_mcq,
    handle_document,
    handle_photo,
    handle_explanation_text
)

from handlers.quiz import (
    quiz_menu_handler,
    start_group_quiz,
    join_quiz_handler,
    quiz_callback_handler
)

from handlers.battle import (
    battle_handler,
    group_battle_handler,
    battle_setup_callback,
    battle_topic_text_handler,
    accept_battle_handler,
    battle_callback_handler
)

from handlers.stats import (
    stats_handler,
    leaderboard_handler
)

from handlers.revision import (
    revision_handler,
    bookmarks_handler,
    wrong_bank_handler,
    revision_callback_handler
)

from handlers.admin import (
    admin_handler,
    admin_callback_handler,
    handle_admin_search,
    pending_approval_handler
)

from handlers.search import (
    search_handler,
    search_callback_handler,
    handle_search_text
)

from handlers.generate import (
    generate_handler,
    generate_callback_handler
)

from handlers.auto_generate import (
    auto_handler,
    auto_send_callback,
    schedule_auto
)

from services.database import init_db


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


async def post_init(application: Application):
    await init_db()
    logger.info("Database initialized successfully")


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    handled = await handle_explanation_text(update, context)

    if handled:
        return

    handled = await handle_search_text(update, context)

    if handled:
        return

    await handle_admin_search(update, context)


def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # START AND HELP
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))

    # QUIZ
    app.add_handler(CommandHandler("quiz", quiz_menu_handler))
    app.add_handler(CommandHandler("startquiz", start_group_quiz))
    app.add_handler(CommandHandler("joingame", join_quiz_handler))

    # BATTLE
    app.add_handler(CommandHandler("battle", battle_handler))
    app.add_handler(CommandHandler("groupbattle", group_battle_handler))

    # STATS
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("leaderboard", leaderboard_handler))

    # REVISION
    app.add_handler(CommandHandler("revision", revision_handler))
    app.add_handler(CommandHandler("bookmarks", bookmarks_handler))
    app.add_handler(CommandHandler("wrongbank", wrong_bank_handler))

    # SEARCH
    app.add_handler(CommandHandler("search", search_handler))

    # AI GENERATOR
    app.add_handler(CommandHandler("generate", generate_handler))

    # AUTO GENERATION
    app.add_handler(CommandHandler("auto", auto_handler))
    schedule_auto(app)

    # ADMIN
    app.add_handler(CommandHandler("admin", admin_handler))
    app.add_handler(CommandHandler("pending", pending_approval_handler))

    # BATTLE TOPIC INPUT
    # FIX: group=-1 so it runs BEFORE every other text handler.
    # In python-telegram-bot only the first matching handler in a group
    # runs, so putting this in the default group made it swallow all text
    # and block search / explanation / admin text handling.
    # It raises ApplicationHandlerStop itself when it handles a topic.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            battle_topic_text_handler
        ),
        group=-1
    )

    # MCQ IMPORT
    app.add_handler(
        MessageHandler(filters.POLL, handle_poll)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(
                r'(?i)(^\d+[\.\)]|^Q[\.\)]|^Question)'
            ),
            handle_text_mcq
        )
    )

    app.add_handler(
        MessageHandler(filters.Document.PDF, handle_document)
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )

    # GENERAL TEXT
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    # QUIZ CALLBACKS
    app.add_handler(
        CallbackQueryHandler(
            quiz_callback_handler,
            pattern=r'^quiz_'
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            quiz_callback_handler,
            pattern=r'^(my_stats|leaderboard)$'
        )
    )

    # BATTLE SETUP CALLBACKS
    app.add_handler(
        CallbackQueryHandler(
            battle_setup_callback,
            pattern=r'^bsetup_'
        )
    )

    # BATTLE ACCEPTANCE
    app.add_handler(
        CallbackQueryHandler(
            accept_battle_handler,
            pattern=r'^accept_battle_'
        )
    )

    # BATTLE MENU
    app.add_handler(
        CallbackQueryHandler(
            battle_callback_handler,
            pattern=r'^battle_'
        )
    )

    # BATTLE ANSWERS
    app.add_handler(
        CallbackQueryHandler(
            battle_callback_handler,
            pattern=r'^ba\|'
        )
    )

    # REVISION CALLBACKS
    app.add_handler(
        CallbackQueryHandler(
            revision_callback_handler,
            pattern=r'^rev_'
        )
    )

    # ADMIN CALLBACKS
    app.add_handler(
        CallbackQueryHandler(
            admin_callback_handler,
            pattern=r'^adm_'
        )
    )

    # SEARCH CALLBACKS
    app.add_handler(
        CallbackQueryHandler(
            search_callback_handler,
            pattern=r'^srch_'
        )
    )

    # GENERATE CALLBACKS
    app.add_handler(
        CallbackQueryHandler(
            generate_callback_handler,
            pattern=r'^gen_'
        )
    )

    # AUTO GENERATION CALLBACKS
    app.add_handler(
        CallbackQueryHandler(
            auto_send_callback,
            pattern=r'^agen\|'
        )
    )

    logger.info("MedArena bot starting...")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
