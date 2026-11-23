
"""
MedArena Configuration
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------
# TELEGRAM
# Accept all common hosting environment variable names
# --------------------------------------------------

BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or ""
).strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "Telegram bot token missing! "
        "Set BOT_TOKEN or TELEGRAM_BOT_TOKEN in FadeHost."
    )

# --------------------------------------------------
# DATABASE
# --------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://user:pass@localhost/medquiz"
)

# --------------------------------------------------
# GROQ AI
# --------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

GROQ_MODEL_FAST = "llama-3.1-8b-instant"
GROQ_MODEL_SMART = "llama-3.3-70b-versatile"

# --------------------------------------------------
# GEMINI AI
# --------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# --------------------------------------------------
# ADMIN
# --------------------------------------------------

_admin_raw = os.getenv("ADMIN_IDS", "")

ADMIN_IDS = [
    int(x.strip())
    for x in _admin_raw.split(",")
    if x.strip().isdigit()
]

# --------------------------------------------------
# SOURCE CHANNELS
# --------------------------------------------------

_sources_raw = os.getenv("SOURCE_CHAT_IDS", "")

SOURCE_CHAT_IDS = [
    int(x.strip())
    for x in _sources_raw.split(",")
    if x.strip()
]

# --------------------------------------------------
# QUIZ SETTINGS
# --------------------------------------------------

DEFAULT_QUESTION_TIME = 30

DEFAULT_QUESTIONS_PER_QUIZ = 10

MAX_QUESTIONS_PER_QUIZ = 50

# --------------------------------------------------
# BATTLE SETTINGS
# --------------------------------------------------

BATTLE_INVITE_TIMEOUT = 60

BATTLE_BONUS_SPEED_SECONDS = 10

# --------------------------------------------------
# XP SYSTEM
# --------------------------------------------------

XP_CORRECT = 10

XP_WRONG = -2

XP_SPEED_BONUS = 5

# --------------------------------------------------
# DIFFICULTY
# --------------------------------------------------

DIFFICULTIES = [
    "Easy",
    "Medium",
    "Hard",
    "NEET PG",
    "INICET",
    "FMGE",
    "USMLE",
]

# --------------------------------------------------
# MEDICAL SUBJECTS
# --------------------------------------------------

SUBJECTS = [
    "Anatomy",
    "Physiology",
    "Biochemistry",
    "Pathology",
    "Pharmacology",
    "Microbiology",
    "Forensic Medicine",
    "ENT",
    "Ophthalmology",
    "Community Medicine (PSM)",
    "Internal Medicine",
    "Surgery",
    "Obstetrics & Gynaecology",
    "Paediatrics",
    "Psychiatry",
    "Orthopaedics",
    "Radiology",
    "Anaesthesia",
    "Dermatology",
    "Other",
]
