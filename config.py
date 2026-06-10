"""
Configuration - load from environment variables
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Database (PostgreSQL on Railway)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/medquiz")

# Groq AI
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL_FAST = "llama-3.1-8b-instant"
GROQ_MODEL_SMART = "llama-3.3-70b-versatile"

# Admin user IDs (comma-separated in env)
_admin_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()]

# Source channels/groups to monitor for MCQ import
# Comma-separated list of chat IDs
_sources_raw = os.getenv("SOURCE_CHAT_IDS", "")
SOURCE_CHAT_IDS = [int(x.strip()) for x in _sources_raw.split(",") if x.strip()]

# Quiz settings
DEFAULT_QUESTION_TIME = 30       # seconds per question
DEFAULT_QUESTIONS_PER_QUIZ = 10
MAX_QUESTIONS_PER_QUIZ = 50

# Battle settings
BATTLE_INVITE_TIMEOUT = 60       # seconds to accept a battle challenge
BATTLE_BONUS_SPEED_SECONDS = 10  # answer within this time for speed bonus

# XP system
XP_CORRECT = 10
XP_WRONG = -2
XP_SPEED_BONUS = 5

# Difficulty labels
DIFFICULTIES = [
    "Easy",
    "Medium",
    "Hard",
    "NEET PG",
    "INICET",
    "FMGE",
    "USMLE",
]

# Medical subjects
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
