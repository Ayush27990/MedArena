"""
AI Service - Groq API
Handles MCQ categorization, explanation generation, and text parsing
"""

import json
import logging
import asyncio
import httpx
from config import GROQ_API_KEY, GROQ_MODEL_FAST, GROQ_MODEL_SMART, SUBJECTS, DIFFICULTIES

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json",
}


async def _call_groq(prompt: str, model: str = GROQ_MODEL_FAST,
                     max_tokens: int = 800, retries: int = 3) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(GROQ_URL, headers=HEADERS, json=payload)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(f"Rate limited, waiting {wait}s")
                await asyncio.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                raise
    return ""


async def categorize_mcq(question: str, options: list[str]) -> dict:
    """
    Returns: {subject, topic, difficulty, explanation (optional fix)}
    """
    subjects_str = ", ".join(SUBJECTS)
    difficulties_str = ", ".join(DIFFICULTIES)
    options_str = "\n".join(f"{chr(65+i)}. {o}" for i, o in enumerate(options))

    prompt = f"""You are a medical education expert. Analyze this MCQ and respond ONLY with valid JSON.

Question: {question}
{options_str}

Return exactly this JSON structure (no markdown, no explanation outside JSON):
{{
  "subject": "<one of: {subjects_str}>",
  "topic": "<specific topic within subject, e.g. 'Cardiac Cycle', 'Gram Positive Cocci'>",
  "difficulty": "<one of: {difficulties_str}>",
  "question_fixed": "<corrected question if there were typos/formatting issues, else null>",
  "explanation": "<brief 2-3 sentence clinical explanation of why the correct answer is correct, null if you cannot determine correct answer>"
}}"""

    try:
        raw = await _call_groq(prompt, model=GROQ_MODEL_FAST, max_tokens=500)
        # Strip markdown fences if present
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        return {
            "subject": data.get("subject", "Other"),
            "topic": data.get("topic", ""),
            "difficulty": data.get("difficulty", "Medium"),
            "question_fixed": data.get("question_fixed"),
            "explanation": data.get("explanation"),
        }
    except Exception as e:
        logger.error(f"Categorize error: {e}")
        return {"subject": "Other", "topic": "", "difficulty": "Medium",
                "question_fixed": None, "explanation": None}


async def generate_explanation(question: str, options: list[str], correct: str) -> str:
    """Generate explanation for an MCQ given the correct answer."""
    options_str = "\n".join(f"{chr(65+i)}. {o}" for i, o in enumerate(options))
    prompt = f"""You are a medical educator. Write a concise clinical explanation (3-4 sentences) for this MCQ.

Question: {question}
{options_str}
Correct Answer: {correct}

Explain WHY {correct} is correct and briefly why other options are wrong. Be precise and exam-focused. Respond with only the explanation text."""

    try:
        return await _call_groq(prompt, model=GROQ_MODEL_SMART, max_tokens=300)
    except Exception as e:
        logger.error(f"Explanation error: {e}")
        return ""


async def parse_text_mcq(text: str) -> list[dict]:
    """
    Parse free-form text containing one or more MCQs.
    Returns list of MCQ dicts.
    """
    prompt = f"""Extract all MCQs from this text and return ONLY a JSON array. Each MCQ object must have:
- "question": string
- "options": array of exactly 4 strings (the answer choices, WITHOUT the A/B/C/D prefix)
- "correct": "A", "B", "C", "D", or "E" (letter only)
- "explanation": string or null

Text to parse:
{text}

Rules:
- If correct answer is not explicitly marked, set "correct" to null
- Remove option letters (A. B. 1. etc.) from the option text
- If there are 5 options, include all 5
- Return [] if no valid MCQs found
- Respond ONLY with the JSON array, no other text"""

    try:
        raw = await _call_groq(prompt, model=GROQ_MODEL_SMART, max_tokens=1500)
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Parse text MCQ error: {e}")
        return []


async def parse_pdf_text(text: str) -> list[dict]:
    """Parse extracted PDF text for MCQs."""
    # Split into chunks if too large
    chunk_size = 3000
    all_mcqs = []
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        mcqs = await parse_text_mcq(chunk)
        all_mcqs.extend(mcqs)
        await asyncio.sleep(0.5)  # rate limiting
    return all_mcqs


async def ocr_image_to_mcqs(image_base64: str) -> list[dict]:
    """
    Use Groq vision-capable model to extract MCQs from image.
    Falls back to text extraction prompt if vision not available.
    """
    # Note: Use llama-3.2-90b-vision-preview for image support
    payload = {
        "model": "llama-3.2-11b-vision-preview",
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    }
                },
                {
                    "type": "text",
                    "text": """Extract all MCQs from this image and return ONLY a JSON array. Each object:
{"question": "...", "options": ["opt1","opt2","opt3","opt4"], "correct": "A"/"B"/"C"/"D" or null, "explanation": null}
Respond with ONLY the JSON array."""
                }
            ]
        }],
        "max_tokens": 1500,
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(GROQ_URL, headers=HEADERS, json=payload)
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return []
