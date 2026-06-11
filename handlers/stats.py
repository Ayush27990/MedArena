"""Stats and Leaderboard handlers"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services.database import get_user, get_leaderboard, upsert_user


def _xp_to_rank(xp: int) -> str:
    if xp < 100:   return "🥉 Intern"
    if xp < 300:   return "🥈 Resident"
    if xp < 600:   return "🥇 Senior Resident"
    if xp < 1000:  return "🏅 Registrar"
    if xp < 2000:  return "🎖 Consultant"
    if xp < 5000:  return "🏆 Senior Consultant"
    return "👑 Professor"


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user.id, user.username or "", user.full_name or "")
    db_user = await get_user(user.id)

    if not db_user:
        msg = "❌ Could not retrieve your stats."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    attempted = db_user["total_attempted"] or 0
    correct   = db_user["total_correct"] or 0
    xp        = db_user["xp"] or 0
    accuracy  = round(correct * 100 / attempted, 1) if attempted > 0 else 0
    rank      = _xp_to_rank(xp)

    thresholds = [100, 300, 600, 1000, 2000, 5000, 999999]
    next_xp    = next((t for t in thresholds if t > xp), None)
    progress   = f"{xp}/{next_xp} XP to next rank" if next_xp else "Max rank!"

    text = (
        f"📊 *Your Stats — {user.first_name}*\n\n"
        f"{rank}\n\n"
        f"⭐ XP: *{xp}*\n"
        f"📈 {progress}\n\n"
        f"📝 Attempted: *{attempted}*\n"
        f"✅ Correct: *{correct}*\n"
        f"❌ Wrong: *{attempted - correct}*\n"
        f"🎯 Accuracy: *{accuracy}%*"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
        InlineKeyboardButton("🔄 Wrong Bank", callback_data="rev_wrong"),
    ]])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=kb
        )


async def leaderboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_leaderboard(limit=10)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    lines = ["🏆 *Leaderboard — Top 10*\n"]
    for i, row in enumerate(rows):
        name = (row["full_name"] or row["username"] or f"User{row['user_id']}")[:20]
        medal = medals[i] if i < len(medals) else f"{i+1}."
        acc = row["accuracy"] if row["accuracy"] is not None else 0
        lines.append(
            f"{medal} *{name}*\n"
            f"   ⭐ {row['xp']} XP | 🎯 {acc}% | 📝 {row['total_attempted']}"
        )

    if not rows:
        lines.append("No data yet. Start quizzing to appear here!")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 My Stats", callback_data="my_stats"),
    ]])

    text = "\n\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
