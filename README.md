# 🏥 MedQuiz Master Bot

A complete Telegram quiz ecosystem for **NEET PG | INICET | FMGE | USMLE** preparation.

Automatically collects MCQs from your Telegram groups/channels, organizes them with AI, and delivers interactive quizzes with battles, leaderboards, and smart revision.

---

## ✨ Features

| Feature | Description |
|---|---|
| **MCQ Import** | Polls, text, PDF, images (OCR) |
| **AI Categorization** | Subject, topic, difficulty via Groq |
| **Deduplication** | Auto-detects duplicate questions |
| **Group Quiz** | Multi-user timed quizzes |
| **1v1 Battle** | Real-time battles with speed bonus |
| **Leaderboard** | XP-based ranking system |
| **Wrong Bank** | Redo questions you got wrong |
| **Bookmarks** | Save questions for later |
| **Search** | Filter by keyword / subject / difficulty |
| **Admin Panel** | Approve, edit, delete MCQs |

---

## 🚀 Setup

### 1. Create the bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. `/newbot` → get your `BOT_TOKEN`

### 2. Get your Groq API key
1. Go to [console.groq.com](https://console.groq.com)
2. Create an API key

### 3. Get your Telegram User ID
1. Message [@userinfobot](https://t.me/userinfobot)
2. Copy the numeric ID — this is your `ADMIN_IDS`

### 4. Deploy to Railway

#### a) Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/medquiz-bot
git push -u origin main
```

#### b) Create Railway project
1. Go to [railway.app](https://railway.app)
2. **New Project → Deploy from GitHub repo**
3. Select your repo

#### c) Add PostgreSQL
1. In your Railway project → **+ Add Service → PostgreSQL**
2. Copy the `DATABASE_URL` from the PostgreSQL service variables

#### d) Set environment variables
In Railway → your bot service → **Variables**, add:

```
BOT_TOKEN=your_bot_token
GROQ_API_KEY=your_groq_key
DATABASE_URL=postgresql://... (from Railway PostgreSQL)
ADMIN_IDS=your_telegram_user_id
SOURCE_CHAT_IDS=-1001234567890  (optional)
```

#### e) Deploy
Railway auto-deploys on push. Check **Logs** to verify it's running.

---

## 📱 Bot Commands

### User Commands
| Command | Action |
|---|---|
| `/start` | Welcome screen with all options |
| `/quiz` | Start a quiz |
| `/startquiz` | Start group quiz (in a group) |
| `/joingame SESSION_ID` | Join a group quiz |
| `/battle @username` | Challenge someone |
| `/stats` | Your performance stats |
| `/leaderboard` | Top 10 scorers |
| `/revision` | Smart revision menu |
| `/wrongbank` | Redo wrong questions |
| `/bookmarks` | Your saved questions |
| `/search keyword` | Search MCQ database |

### Admin Commands
| Command | Action |
|---|---|
| `/admin` | Admin panel |
| `/pending` | Review pending MCQs |

---

## 📥 Importing MCQs

### Method 1: Forward Telegram Quiz Polls
Forward any quiz poll (from your channels/groups) to the bot in private. It auto-extracts the question, options, and correct answer.

### Method 2: Text MCQ
Send text directly to the bot:
```
Q. Which drug is used in Wilson's disease?
A. Penicillamine
B. Deferoxamine
C. Dimercaprol
D. Succimer
Ans: A
```

### Method 3: PDF
Send a PDF file containing MCQs. The bot extracts text and parses all questions automatically.

### Method 4: Image/Screenshot
Send a photo of MCQs. The bot uses Groq's vision model (OCR) to extract questions.

---

## 🎮 Quiz Modes

### Solo Quiz
- Choose subject + difficulty
- Timed questions (30s default)
- Instant feedback with explanation
- XP earned per correct answer

### Group Quiz (in group chat)
1. Admin runs `/startquiz`
2. Configures: subject, num questions, time per Q
3. Members join with `/joingame SESSION_ID` or tap Join button
4. Quiz starts, everyone answers the same questions
5. Final leaderboard shown

### 1v1 Battle
1. `/battle @opponent_username` in group
2. Opponent taps Accept button (60s window)
3. Both get same questions simultaneously
4. Speed bonus: correct in <10s = +5 XP
5. Winner announced with score comparison

---

## ⭐ XP & Ranks

| XP | Rank |
|---|---|
| 0–99 | 🥉 Intern |
| 100–299 | 🥈 Resident |
| 300–599 | 🥇 Senior Resident |
| 600–999 | 🏅 Registrar |
| 1000–1999 | 🎖 Consultant |
| 2000–4999 | 🏆 Senior Consultant |
| 5000+ | 👑 Professor |

XP per question:
- ✅ Correct: +10
- ⚡ Speed bonus (≤10s): +5
- ❌ Wrong: -2

---

## 🛠 Admin Panel

- **Approve MCQs** — imported MCQs go to pending first (unless imported by admin)
- **Edit subject/difficulty/correct/explanation** — fix AI categorization errors
- **Delete** — remove bad/duplicate MCQs
- **Search** — find MCQs by keyword
- **Statistics** — total MCQs, users, pending count

---

## 🔧 Project Structure

```
medquiz_bot/
├── bot.py              # Entry point, handler registration
├── config.py           # Environment variables, constants
├── requirements.txt    # Python dependencies
├── railway.toml        # Railway deployment config
├── handlers/
│   ├── start.py        # /start, /help
│   ├── import_mcq.py   # Poll, text, PDF, image import
│   ├── quiz.py         # Solo + group quiz logic
│   ├── battle.py       # 1v1 battle mode
│   ├── stats.py        # Stats, leaderboard
│   ├── revision.py     # Wrong bank, bookmarks, subject revision
│   ├── admin.py        # Admin panel
│   └── search.py       # MCQ search
└── services/
    ├── database.py     # PostgreSQL (asyncpg) — all DB operations
    └── ai_service.py   # Groq API — categorization, OCR, explanation
```

---

## 📊 Database Schema

- **mcqs** — all questions with subject/topic/difficulty/approval status
- **users** — registered users with XP and stats
- **user_answers** — complete answer history
- **bookmarks** — saved questions per user
- **quiz_sessions** — group quiz state
- **battle_sessions** — 1v1 battle state

---

## 🔒 Security Notes

- Never commit your `.env` file
- Rotate your `GROQ_API_KEY` if accidentally exposed
- `ADMIN_IDS` controls who can approve/delete MCQs
- MCQs imported by non-admins go to `approved=FALSE` by default
