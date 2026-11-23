"""
Gemini Service - writes new MCQs with the Gemini API.
Needs the GEMINI_API_KEY environment variable.
Optional: GEMINI_MODEL (default below) so you can switch models without editing code.
"""

import os
import re
import json
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Telegram quiz poll limits
MAX_QUESTION = 300
MAX_OPTION = 100
MAX_EXPLANATION = 200


class GeminiError(Exception):
    """Raised when Gemini cannot produce MCQs. The message is safe to show the admin."""


def _build_prompt(topic: str, count: int, level: str | None = None,
                  avoid: list[str] | None = None) -> str:
    if level and "usmle" in level.lower():
        exam = "USMLE Step 1 / Step 2 CK"
        style = ("- USMLE style: a compact clinical vignette (age, sex, key findings) that asks for the "
                 "mechanism, diagnosis or next best step. Keep it short: Telegram polls allow only 300 characters.")
    elif level:
        exam = "NEET PG / INICET"
        style = "- Prefer clinical-vignette or high-yield recall questions, in the style of recent NEET PG papers."
    else:
        exam = "NEET PG / INICET / FMGE"
        style = "- Prefer clinical-vignette or high-yield recall questions, in the style of recent NEET PG papers."

    avoid_block = ""
    if avoid:
        lines = "\n".join(f"  * {q[:120]}" for q in avoid[:25])
        avoid_block = f"\n- These questions already exist. Do NOT test the same concept again:\n{lines}"

    return f"""You are an expert medical examiner writing MCQs for {exam} preparation.

Write exactly {count} high-quality, exam-style MCQs on: {topic}

Rules:
{style}
- Exactly 4 options per question. Exactly ONE is correct. Wrong options must be plausible.
- Be factually accurate according to standard textbooks. Do not invent facts. If unsure, skip that point.
- Do not repeat the same concept twice.{avoid_block}
- Question: at most 280 characters. Each option: at most 90 characters. No "A." / "B." prefixes inside options.
- Explanation: one or two short sentences, at most 180 characters, saying why the answer is correct.
- "correct" is a single letter: A, B, C or D.

Return ONLY a JSON array, no other text, no markdown. Each item:
{{"question": "...", "options": ["...", "...", "...", "..."], "correct": "A", "explanation": "..."}}"""


def _extract_json_array(text: str) -> list:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.replace("```json", "").replace("```", "").strip()
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b == -1 or b < a:
        raise GeminiError("Gemini did not return a list of MCQs. Please try again.")
    try:
        data = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        raise GeminiError("Gemini's reply was cut off or broken. Please try again.")
    if not isinstance(data, list):
        raise GeminiError("Gemini's reply was not a list. Please try again.")
    return data


def _clean_mcqs(raw_items: list) -> tuple[list[dict], int]:
    """Keep only MCQs that Telegram can post as a quiz. Returns (good, dropped_count)."""
    good, dropped = [], 0
    seen = set()
    for item in raw_items:
        try:
            q = str(item.get("question", "")).strip()
            opts = [re.sub(r"^\s*[A-Da-d][.)]\s+", "", str(o)).strip() for o in item.get("options", [])]
            correct = str(item.get("correct", "")).strip().upper()[:1]
            expl = str(item.get("explanation", "") or "").strip()
        except AttributeError:
            dropped += 1
            continue

        valid = (
            q
            and len(q) <= MAX_QUESTION
            and len(opts) == 4
            and all(o and len(o) <= MAX_OPTION for o in opts)
            and len({o.lower() for o in opts}) == 4
            and correct in ("A", "B", "C", "D")
            and q.lower() not in seen
        )
        if not valid:
            dropped += 1
            continue

        seen.add(q.lower())
        if len(expl) > MAX_EXPLANATION:
            expl = expl[:MAX_EXPLANATION - 1].rstrip() + "…"
        good.append({"question": q, "options": opts, "correct": correct, "explanation": expl})
    return good, dropped


async def generate_mcqs(topic: str, count: int = 5, level: str | None = None,
                        avoid: list[str] | None = None) -> tuple[list[dict], int]:
    """
    Ask Gemini for `count` MCQs on `topic`.
    Returns (mcqs, dropped) where dropped = how many Gemini items were unusable.
    Retries when Gemini is busy, then tries GEMINI_FALLBACK_MODEL if you set one.
    Raises GeminiError with a readable message on failure.
    """
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY is not set on the host. Add it in the environment variables and restart.")

    models = [GEMINI_MODEL]
    fallback = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
    if fallback and fallback != GEMINI_MODEL:
        models.append(fallback)

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": _build_prompt(topic, count, level, avoid)}]}],
        "generationConfig": {"temperature": 0.6},
    }
    waits = [3, 6, 12]          # pause before retry 2, 3, 4 (per model)
    last_error = "Gemini is busy right now. Please try again in a few minutes."

    for model in models:
        url = GEMINI_URL.format(model=model)
        for attempt in range(len(waits) + 1):
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    r = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as e:
                logger.error(f"Gemini network error ({model}, try {attempt + 1}): {type(e).__name__}: {e}")
                last_error = "Could not reach Gemini. Please try again."
                if attempt < len(waits):
                    await asyncio.sleep(waits[attempt])
                    continue
                break  # next model

            if r.status_code in (429, 500, 502, 503, 504):
                logger.warning(f"Gemini busy ({model}, try {attempt + 1}): HTTP {r.status_code}")
                last_error = ("Gemini rate limit reached. Try again in a minute." if r.status_code == 429
                              else "Gemini is busy right now. Please try again in a few minutes.")
                if attempt < len(waits):
                    await asyncio.sleep(waits[attempt])
                    continue
                break  # next model

            if r.status_code != 200:
                logger.error(f"Gemini HTTP {r.status_code} ({model}): {r.text[:500]}")
                if r.status_code == 404:
                    last_error = "Gemini model not found. Set GEMINI_MODEL to a model that exists in your account."
                elif r.status_code in (400, 401, 403):
                    last_error = "Gemini rejected the request (check GEMINI_API_KEY and GEMINI_MODEL)."
                else:
                    last_error = f"Gemini error {r.status_code}."
                break  # next model (a fallback may still work)

            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
                items = _extract_json_array(text)
            except (KeyError, IndexError, ValueError, TypeError) as e:
                logger.error(f"Gemini bad response ({model}): {type(e).__name__}: {e}")
                last_error = "Gemini returned an empty or blocked answer. Try a different topic."
                break
            except GeminiError as e:
                last_error = str(e)
                if attempt < len(waits):
                    await asyncio.sleep(2)
                    continue
                break
            good, dropped = _clean_mcqs(items)
            if good:
                return good, dropped
            last_error = "Gemini's questions were not usable. Please try again."
            break

    raise GeminiError(last_error)
