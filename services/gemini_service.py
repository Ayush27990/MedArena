"""
Gemini Service - writes new MCQs with the Gemini API (and, for a second opinion, Groq).

Needs the GEMINI_API_KEY environment variable.
Optional: GEMINI_MODEL (default below), GEMINI_FALLBACK_MODEL, GROQ_VISION_MODEL.

What is in here
  generate_mcqs        text MCQs on a topic (or based on a source text/poll)
  reassess_mcq         double-check an MCQ and fix a wrong answer key
  generate_image_mcq   ONE image-based MCQ written by looking at a real image
Every function takes provider="gemini" (default) or provider="other" (Groq),
so the admin can ask a different AI for a second opinion.
"""

import os
import re
import json
import base64
import asyncio
import logging
import httpx

from config import GROQ_API_KEY, GROQ_MODEL_SMART, SUBJECTS

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PROVIDER_LABELS = {"gemini": "Gemini", "other": "Groq"}

# Telegram quiz poll limits
MAX_QUESTION = 300
MAX_IMAGE_QUESTION = 900      # image questions may be longer: they can go in a normal text message
MAX_OPTION = 100
MAX_EXPLANATION = 200


class GeminiError(Exception):
    """Raised when the AI cannot produce MCQs. The message is safe to show the admin."""


# ─────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────

def _exam_and_style(level: str | None) -> tuple[str, str]:
    if level and "usmle" in level.lower():
        return ("USMLE Step 1 / Step 2 CK",
                "- USMLE style: a compact clinical vignette (age, sex, key findings) that asks for the "
                "mechanism, diagnosis or next best step. Keep it short: Telegram polls allow only 300 characters.")
    if level:
        return ("NEET PG / INICET",
                "- Prefer clinical-vignette or high-yield recall questions, in the style of recent NEET PG papers.")
    return ("NEET PG / INICET / FMGE",
            "- Prefer clinical-vignette or high-yield recall questions, in the style of recent NEET PG papers.")


def _avoid_block(avoid: list[str] | None) -> str:
    if not avoid:
        return ""
    lines = "\n".join(f"  * {q[:120]}" for q in avoid[:25])
    return f"\n- These questions already exist. Do NOT test the same concept again:\n{lines}"


def _build_prompt(topic: str, count: int, level: str | None = None,
                  avoid: list[str] | None = None, source: str | None = None) -> str:
    exam, style = _exam_and_style(level)

    source_block = ""
    if source:
        source_block = (
            "\n\nSOURCE MATERIAL (an MCQ, notes or a poll the admin sent). Write NEW question(s) that test the "
            "same concept or a closely related high-yield point. Do NOT copy the wording or the same stem. "
            "Use the source only as a guide; if it contains a factual error, ignore that part.\n"
            f"<<<\n{source[:4000]}\n>>>"
        )

    return f"""You are an expert medical examiner writing MCQs for {exam} preparation.

Write exactly {count} high-quality, exam-style MCQs on: {topic}{source_block}

Rules:
{style}
- Exactly 4 options per question. Exactly ONE is correct. Wrong options must be plausible.
- Be factually accurate according to standard textbooks. Do not invent facts. If unsure, skip that point.
- Do not repeat the same concept twice.{_avoid_block(avoid)}
- Question: at most 280 characters. Each option: at most 90 characters. No "A." / "B." prefixes inside options.
- Explanation: one or two short sentences, at most 180 characters, saying why the answer is correct.
- "correct" is a single letter: A, B, C or D.

Return ONLY a JSON array, no other text, no markdown. Each item:
{{"question": "...", "options": ["...", "...", "...", "..."], "correct": "A", "explanation": "..."}}"""


def _build_reassess_prompt(mcq: dict, has_image: bool) -> str:
    opts = "\n".join(f"{l}. {o}" for l, o in zip("ABCD", mcq["options"]))
    image_line = ("\nThe question refers to the attached image. Look at it carefully and use it.\n"
                  if has_image else "")
    max_q = MAX_IMAGE_QUESTION if has_image else MAX_QUESTION
    return f"""You are a senior medical examiner double-checking an MCQ before it is published to students.
{image_line}
Question: {mcq['question']}
{opts}
Answer key given: {mcq['correct']}
Explanation given: {mcq.get('explanation') or '(none)'}

Task:
1. Solve the question yourself from standard textbooks FIRST. Only then compare with the key.
2. If the key is right and the question is sound, return it unchanged with "verdict": "ok".
3. If the key is wrong, or two options could be correct, or a fact in the question/explanation is wrong,
   FIX it with as few changes as possible (usually just "correct" and "explanation") and use "verdict": "fixed".
4. "note": one short sentence (at most 150 characters) saying what you checked or changed.

Limits: question at most {max_q} characters, each option at most 90 characters, explanation at most 180 characters,
exactly 4 options, no "A." prefixes inside options, "correct" is one letter A-D.

Return ONLY one JSON object, no markdown:
{{"verdict": "ok", "question": "...", "options": ["...", "...", "...", "..."], "correct": "A", "explanation": "...", "note": "..."}}"""


def _build_image_prompt(hint: str, level: str | None, avoid: list[str] | None) -> str:
    exam, _ = _exam_and_style(level)
    hint_line = f"\nThe admin added this note about the image: \"{hint[:500]}\"\n" if hint else "\n"
    subjects = ", ".join(s for s in SUBJECTS if s != "Other")
    return f"""You are an expert medical examiner writing an IMAGE-BASED MCQ for {exam} preparation.
Look carefully at the attached image.{hint_line}
Decide what kind of image it is:
- A medical picture (clinical photo, X-ray, CT, MRI, ultrasound, ECG, fundus, histology/pathology slide, specimen,
  instrument, structure, anatomy diagram, graph, flowchart, table): write ONE MCQ that CANNOT be answered without
  looking at the image, and set "uses_image": true.
- Mostly text (notes, a screenshot of a question, a textbook page): set "uses_image": false and write ONE new MCQ
  on the same concept. That question must make sense WITHOUT the image.

Rules:
- Use ONLY what is clearly visible/readable in the image (plus the admin's note). Never invent findings.
  If the image is too blurry, cropped or unclear for a reliable question, return {{"error": "short reason"}} instead.
- If the image has arrows or labels, refer to them naturally ("the structure marked by the arrows").
- Never reveal the answer inside the question text.
- Exactly 4 options, ONE correct, plausible wrong options. No "A." prefixes inside options.
- Question: ideally at most 280 characters (a short clinical vignette up to {MAX_IMAGE_QUESTION} characters is allowed
  only when really needed). Each option: at most 90 characters.
- Explanation: at most 180 characters, saying why the answer is correct.
- "subject" must be one of: {subjects}
- "topic": a short specific topic, at most 60 characters.
- Be factually accurate according to standard textbooks.{_avoid_block(avoid)}

Return ONLY one JSON object, no markdown:
{{"uses_image": true, "subject": "...", "topic": "...", "question": "...", "options": ["...", "...", "...", "..."], "correct": "A", "explanation": "..."}}"""


# ─────────────────────────────────────────────────────────────
# Parsing / cleaning
# ─────────────────────────────────────────────────────────────

def _strip_noise(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.replace("```json", "").replace("```", "").strip()


def _extract_json_array(text: str) -> list:
    text = _strip_noise(text)
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b == -1 or b < a:
        raise GeminiError("The AI did not return a list of MCQs. Please try again.")
    try:
        data = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        raise GeminiError("The AI's reply was cut off or broken. Please try again.")
    if not isinstance(data, list):
        raise GeminiError("The AI's reply was not a list. Please try again.")
    return data


def _extract_json_object(text: str) -> dict:
    text = _strip_noise(text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise GeminiError("The AI did not return a usable answer. Please try again.")
    try:
        data = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        raise GeminiError("The AI's reply was cut off or broken. Please try again.")
    if not isinstance(data, dict):
        raise GeminiError("The AI's reply was not in the expected format. Please try again.")
    return data


def _clean_one(item, max_question: int = MAX_QUESTION) -> dict | None:
    """Return a clean MCQ dict that Telegram can post, or None if it is unusable."""
    try:
        q = str(item.get("question", "")).strip()
        opts = [re.sub(r"^\s*[A-Da-d][.)]\s+", "", str(o)).strip() for o in item.get("options", [])]
        correct = str(item.get("correct", "")).strip().upper()[:1]
        expl = str(item.get("explanation", "") or "").strip()
    except (AttributeError, TypeError):
        return None

    valid = (
        q
        and len(q) <= max_question
        and len(opts) == 4
        and all(o and len(o) <= MAX_OPTION for o in opts)
        and len({o.lower() for o in opts}) == 4
        and correct in ("A", "B", "C", "D")
    )
    if not valid:
        return None
    if len(expl) > MAX_EXPLANATION:
        expl = expl[:MAX_EXPLANATION - 1].rstrip() + "…"
    return {"question": q, "options": opts, "correct": correct, "explanation": expl}


def _clean_mcqs(raw_items: list) -> tuple[list[dict], int]:
    """Keep only MCQs that Telegram can post as a quiz. Returns (good, dropped_count)."""
    good, dropped, seen = [], 0, set()
    for item in raw_items:
        mcq = _clean_one(item)
        if not mcq or mcq["question"].lower() in seen:
            dropped += 1
            continue
        seen.add(mcq["question"].lower())
        good.append(mcq)
    return good, dropped


def _match_subject(value) -> str | None:
    v = str(value or "").strip().lower()
    for s in SUBJECTS:
        if s.lower() == v:
            return s
    return None


# ─────────────────────────────────────────────────────────────
# Talking to the AI (Gemini or Groq)
# ─────────────────────────────────────────────────────────────

async def _ask_gemini(prompt: str, image: bytes | None, mime: str, temperature: float) -> str:
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY is not set on the host. Add it in the environment variables and restart.")

    models = [GEMINI_MODEL]
    fallback = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
    if fallback and fallback != GEMINI_MODEL:
        models.append(fallback)

    parts = []
    if image:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}})
    parts.append({"text": prompt})

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {"contents": [{"parts": parts}], "generationConfig": {"temperature": temperature}}
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
                parts_out = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts_out)
            except (KeyError, IndexError, ValueError, TypeError) as e:
                logger.error(f"Gemini bad response ({model}): {type(e).__name__}: {e}")
                last_error = "Gemini returned an empty or blocked answer. Try a different topic or image."
                break
            if not text.strip():
                last_error = "Gemini returned an empty answer. Please try again."
                break
            return text

    raise GeminiError(last_error)


async def _ask_groq(prompt: str, image: bytes | None, mime: str, temperature: float) -> str:
    if not GROQ_API_KEY:
        raise GeminiError("GROQ_API_KEY is not set on the host, so the second AI is not available.")

    model = GROQ_VISION_MODEL if image else GROQ_MODEL_SMART
    if image:
        content = [
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": content}],
               "max_tokens": 2000, "temperature": min(temperature, 0.5)}
    waits = [2, 4, 8]
    last_error = "Groq is busy right now. Please try again in a minute."

    for attempt in range(len(waits) + 1):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(GROQ_URL, headers=headers, json=payload)
        except httpx.HTTPError as e:
            logger.error(f"Groq network error (try {attempt + 1}): {type(e).__name__}: {e}")
            last_error = "Could not reach Groq. Please try again."
            if attempt < len(waits):
                await asyncio.sleep(waits[attempt])
                continue
            break

        if r.status_code in (429, 500, 502, 503, 504):
            logger.warning(f"Groq busy (try {attempt + 1}): HTTP {r.status_code}")
            if attempt < len(waits):
                await asyncio.sleep(waits[attempt])
                continue
            break

        if r.status_code != 200:
            logger.error(f"Groq HTTP {r.status_code} ({model}): {r.text[:500]}")
            raise GeminiError(f"Groq error {r.status_code} (model {model}).")

        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, TypeError):
            raise GeminiError("Groq returned an empty answer. Please try again.")

    raise GeminiError(last_error)


async def _ask(prompt: str, provider: str = "gemini", image: bytes | None = None,
               mime: str = "image/jpeg", temperature: float = 0.6) -> str:
    if provider == "other":
        return await _ask_groq(prompt, image, mime, temperature)
    return await _ask_gemini(prompt, image, mime, temperature)


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

async def generate_mcqs(topic: str, count: int = 5, level: str | None = None,
                        avoid: list[str] | None = None, source: str | None = None,
                        provider: str = "gemini") -> tuple[list[dict], int]:
    """
    Ask the AI for `count` MCQs on `topic` (optionally guided by `source` text).
    Returns (mcqs, dropped) where dropped = how many items were unusable.
    Raises GeminiError with a readable message on failure.
    """
    prompt = _build_prompt(topic, count, level, avoid, source)
    last = GeminiError("The questions were not usable. Please try again.")
    for attempt in range(2):
        text = await _ask(prompt, provider)
        try:
            items = _extract_json_array(text)
        except GeminiError as e:
            last = e
            await asyncio.sleep(1)
            continue
        good, dropped = _clean_mcqs(items)
        if good:
            return good, dropped
        last = GeminiError("The AI's questions were not usable. Please try again.")
    raise last


async def reassess_mcq(mcq: dict, provider: str = "gemini",
                       image: bytes | None = None) -> tuple[dict, str]:
    """
    Ask the AI to double-check an MCQ (and fix a wrong answer key).
    Returns (fixed_mcq, note). fixed_mcq only has question/options/correct/explanation.
    """
    prompt = _build_reassess_prompt(mcq, bool(image))
    max_q = MAX_IMAGE_QUESTION if image else MAX_QUESTION
    last = GeminiError("The re-check was not usable. Please try again.")
    for attempt in range(2):
        text = await _ask(prompt, provider, image=image, temperature=0.2)
        try:
            obj = _extract_json_object(text)
        except GeminiError as e:
            last = e
            await asyncio.sleep(1)
            continue
        fixed = _clean_one(obj, max_q)
        if fixed:
            return fixed, str(obj.get("note", "") or "").strip()[:200]
        last = GeminiError("The AI's corrected question was not usable. Please try again.")
    raise last


async def generate_image_mcq(image: bytes, mime: str = "image/jpeg", hint: str = "",
                             level: str | None = None, avoid: list[str] | None = None,
                             provider: str = "gemini") -> dict:
    """
    Write ONE MCQ by looking at a real image.
    Returns {question, options, correct, explanation, uses_image, subject, topic}.
    uses_image=False means the image was just text/notes and the question works without it.
    Raises GeminiError (also when the image is too unclear).
    """
    prompt = _build_image_prompt(hint, level, avoid)
    last = GeminiError("The image question was not usable. Please try again.")
    for attempt in range(2):
        text = await _ask(prompt, provider, image=image, mime=mime, temperature=0.5)
        try:
            obj = _extract_json_object(text)
        except GeminiError as e:
            last = e
            await asyncio.sleep(1)
            continue

        if obj.get("error"):
            raise GeminiError(f"This image can't be used for a question: {str(obj['error'])[:150]}")

        uses_image = obj.get("uses_image") not in (False, "false", "False", 0)
        mcq = _clean_one(obj, MAX_IMAGE_QUESTION if uses_image else MAX_QUESTION)
        if mcq:
            mcq["uses_image"] = uses_image
            mcq["subject"] = _match_subject(obj.get("subject"))
            mcq["topic"] = str(obj.get("topic", "") or "").strip()[:60]
            return mcq
        last = GeminiError("The AI's image question was not usable. Please try again.")
    raise last
