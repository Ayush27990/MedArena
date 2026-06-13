"""
Database service using asyncpg + raw SQL (no ORM overhead)
PostgreSQL on Railway
"""

import asyncpg
import logging
from config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mcqs (
                id          SERIAL PRIMARY KEY,
                question    TEXT NOT NULL,
                option_a    TEXT NOT NULL,
                option_b    TEXT NOT NULL,
                option_c    TEXT NOT NULL,
                option_d    TEXT NOT NULL,
                option_e    TEXT,
                correct     TEXT NOT NULL,
                explanation TEXT,
                subject     TEXT,
                topic       TEXT,
                difficulty  TEXT,
                source_type TEXT,
                source_chat BIGINT,
                imported_by BIGINT,
                approved    BOOLEAN DEFAULT FALSE,
                hash        TEXT UNIQUE,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                username    TEXT,
                full_name   TEXT,
                xp          INT DEFAULT 0,
                total_attempted INT DEFAULT 0,
                total_correct   INT DEFAULT 0,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS user_answers (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                mcq_id      INT REFERENCES mcqs(id) ON DELETE CASCADE,
                chosen      TEXT,
                is_correct  BOOLEAN,
                time_taken  FLOAT,
                session_id  TEXT,
                answered_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS bookmarks (
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                mcq_id      INT REFERENCES mcqs(id) ON DELETE CASCADE,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (user_id, mcq_id)
            );

            CREATE TABLE IF NOT EXISTS quiz_sessions (
                session_id  TEXT PRIMARY KEY,
                chat_id     BIGINT NOT NULL,
                created_by  BIGINT,
                subject     TEXT,
                topic       TEXT,
                difficulty  TEXT,
                num_questions INT DEFAULT 10,
                time_per_q  INT DEFAULT 30,
                status      TEXT DEFAULT 'waiting',
                current_q   INT DEFAULT 0,
                question_ids INT[],
                participants BIGINT[],
                scores      JSONB DEFAULT '{}',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS battle_sessions (
                battle_id   TEXT PRIMARY KEY,
                challenger  BIGINT NOT NULL,
                opponent    BIGINT NOT NULL,
                chat_id     BIGINT,
                status      TEXT DEFAULT 'pending',
                question_ids INT[],
                current_q   INT DEFAULT 0,
                scores      JSONB DEFAULT '{}',
                answer_times JSONB DEFAULT '{}',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_mcqs_subject    ON mcqs(subject);
            CREATE INDEX IF NOT EXISTS idx_mcqs_difficulty ON mcqs(difficulty);
            CREATE INDEX IF NOT EXISTS idx_mcqs_approved   ON mcqs(approved);
            CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_answers_mcq  ON user_answers(mcq_id);
        """)
    logger.info("All tables ensured.")


# ─── MCQ helpers ────────────────────────────────────────────────────

async def insert_mcq(data: dict) -> int | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO mcqs
                (question, option_a, option_b, option_c, option_d, option_e,
                 correct, explanation, subject, topic, difficulty,
                 source_type, source_chat, imported_by, approved, hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (hash) DO NOTHING
            RETURNING id
        """,
            data["question"], data["option_a"], data["option_b"],
            data["option_c"], data["option_d"], data.get("option_e"),
            data["correct"], data.get("explanation"),
            data.get("subject"), data.get("topic"), data.get("difficulty"),
            data.get("source_type"), data.get("source_chat"),
            data.get("imported_by"), data.get("approved", False),
            data["hash"]
        )
        return row["id"] if row else None


async def get_mcqs_for_quiz(subject=None, topic=None, difficulty=None, limit=10):
    pool = await get_pool()
    filters = ["approved = TRUE"]
    params = []
    i = 1
    if subject:
        filters.append(f"subject = ${i}"); params.append(subject); i += 1
    if topic:
        filters.append(f"topic ILIKE ${i}"); params.append(f"%{topic}%"); i += 1
    if difficulty:
        filters.append(f"difficulty = ${i}"); params.append(difficulty); i += 1
    where = " AND ".join(filters)
    params.append(limit)
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"SELECT * FROM mcqs WHERE {where} ORDER BY RANDOM() LIMIT ${i}",
            *params
        )


async def search_mcqs(keyword=None, subject=None, topic=None, difficulty=None, limit=20):
    pool = await get_pool()
    filters = ["approved = TRUE"]
    params = []
    i = 1
    if keyword:
        filters.append(f"(question ILIKE ${i} OR explanation ILIKE ${i})")
        params.append(f"%{keyword}%"); i += 1
    if subject:
        filters.append(f"subject = ${i}"); params.append(subject); i += 1
    if topic:
        filters.append(f"topic ILIKE ${i}"); params.append(f"%{topic}%"); i += 1
    if difficulty:
        filters.append(f"difficulty = ${i}"); params.append(difficulty); i += 1
    where = " AND ".join(filters)
    params.append(limit)
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"SELECT * FROM mcqs WHERE {where} ORDER BY created_at DESC LIMIT ${i}",
            *params
        )


async def get_pending_mcqs(limit=10):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM mcqs WHERE approved = FALSE ORDER BY created_at DESC LIMIT $1",
            limit
        )


async def approve_mcq(mcq_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE mcqs SET approved=TRUE WHERE id=$1", mcq_id)


async def delete_mcq(mcq_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM mcqs WHERE id=$1", mcq_id)


async def update_mcq(mcq_id: int, field: str, value: str):
    pool = await get_pool()
    allowed = {"question","option_a","option_b","option_c","option_d",
               "correct","explanation","subject","topic","difficulty"}
    if field not in allowed:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE mcqs SET {field}=$1, updated_at=NOW() WHERE id=$2",
            value, mcq_id
        )


async def get_mcq_by_id(mcq_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM mcqs WHERE id=$1", mcq_id)


async def get_db_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        total    = await conn.fetchval("SELECT COUNT(*) FROM mcqs")
        approved = await conn.fetchval("SELECT COUNT(*) FROM mcqs WHERE approved=TRUE")
        pending  = await conn.fetchval("SELECT COUNT(*) FROM mcqs WHERE approved=FALSE")
        users    = await conn.fetchval("SELECT COUNT(*) FROM users")
        return {"total": total, "approved": approved,
                "pending": pending, "users": users}


async def update_mcq_explanation_by_hash(hash: str, explanation: str):
    """Update explanation for an MCQ identified by its hash."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE mcqs SET explanation=$1, updated_at=NOW() WHERE hash=$2",
            explanation, hash
        )


# ─── User helpers ────────────────────────────────────────────────────

async def upsert_user(user_id: int, username: str, full_name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
              SET username=$2, full_name=$3, updated_at=NOW()
        """, user_id, username, full_name)


async def get_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def add_xp(user_id: int, xp_delta: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET xp=xp+$1, updated_at=NOW() WHERE user_id=$2",
            xp_delta, user_id
        )


async def record_answer(user_id: int, mcq_id: int, chosen: str,
                        is_correct: bool, time_taken: float, session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_answers
              (user_id, mcq_id, chosen, is_correct, time_taken, session_id)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, user_id, mcq_id, chosen, is_correct, time_taken, session_id)
        await conn.execute("""
            UPDATE users SET
              total_attempted = total_attempted + 1,
              total_correct   = total_correct + $1,
              updated_at      = NOW()
            WHERE user_id = $2
        """, 1 if is_correct else 0, user_id)


async def get_leaderboard(limit=10):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT user_id, username, full_name, xp,
                   total_attempted, total_correct,
                   CASE WHEN total_attempted > 0
                        THEN ROUND(total_correct * 100.0 / total_attempted, 1)
                        ELSE 0 END AS accuracy
            FROM users ORDER BY xp DESC LIMIT $1
        """, limit)


async def get_wrong_questions(user_id: int, limit=20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT DISTINCT m.*
            FROM user_answers ua
            JOIN mcqs m ON m.id = ua.mcq_id
            WHERE ua.user_id=$1 AND ua.is_correct=FALSE
            ORDER BY m.id DESC LIMIT $2
        """, user_id, limit)


async def toggle_bookmark(user_id: int, mcq_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM bookmarks WHERE user_id=$1 AND mcq_id=$2",
            user_id, mcq_id
        )
        if exists:
            await conn.execute(
                "DELETE FROM bookmarks WHERE user_id=$1 AND mcq_id=$2",
                user_id, mcq_id
            )
            return False
        else:
            await conn.execute(
                "INSERT INTO bookmarks(user_id, mcq_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                user_id, mcq_id
            )
            return True


async def get_bookmarks(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT m.* FROM bookmarks b
            JOIN mcqs m ON m.id = b.mcq_id
            WHERE b.user_id=$1 ORDER BY b.created_at DESC
        """, user_id)


# ─── Session helpers ────────────────────────────────────────────────

async def create_quiz_session(data: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO quiz_sessions
              (session_id, chat_id, created_by, subject, topic, difficulty,
               num_questions, time_per_q, question_ids)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """,
            data["session_id"], data["chat_id"], data["created_by"],
            data.get("subject"), data.get("topic"), data.get("difficulty"),
            data.get("num_questions", 10), data.get("time_per_q", 30),
            data.get("question_ids", [])
        )


async def get_quiz_session(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM quiz_sessions WHERE session_id=$1", session_id
        )


async def update_quiz_session(session_id: str, **kwargs):
    pool = await get_pool()
    sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kwargs))
    vals = list(kwargs.values())
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE quiz_sessions SET {sets} WHERE session_id=$1",
            session_id, *vals
        )


async def create_battle(data: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO battle_sessions
              (battle_id, challenger, opponent, chat_id, question_ids)
            VALUES ($1,$2,$3,$4,$5)
        """,
            data["battle_id"], data["challenger"], data["opponent"],
            data.get("chat_id"), data.get("question_ids", [])
        )


async def get_battle(battle_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM battle_sessions WHERE battle_id=$1", battle_id
        )


async def update_battle(battle_id: str, **kwargs):
    pool = await get_pool()
    sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kwargs))
    vals = list(kwargs.values())
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE battle_sessions SET {sets} WHERE battle_id=$1",
            battle_id, *vals
        )


async def get_user_by_username(username: str):
    """Find a user by their Telegram username."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE LOWER(username) = LOWER($1)",
            username.lstrip("@")
        )


async def update_mcq_by_hash(hash: str, field: str, value: str):
    """Update a specific field for an MCQ identified by its hash."""
    allowed = {"correct", "explanation", "subject", "topic", "difficulty"}
    if field not in allowed:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE mcqs SET {field}=$1, updated_at=NOW() WHERE hash=$2",
            value, hash
        )
