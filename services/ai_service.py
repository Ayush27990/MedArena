

async def find_correct_answer_from_explanation(question: str, options: list[str], explanation: str) -> str:
    """
    Given the question, options, and explanation text,
    use AI to determine which option letter (A/B/C/D) is correct.
    """
    options_str = "\n".join(f"{chr(65+i)}. {o}" for i, o in enumerate(options))

    prompt = f"""You are a medical education expert. Based on the explanation below, determine which answer option (A, B, C, D, or E) is correct for this MCQ.

Question: {question}

Options:
{options_str}

Explanation:
{explanation}

Read the explanation carefully. It will mention the correct answer either directly or by describing it.
Respond with ONLY a single letter: A, B, C, D, or E — nothing else."""

    try:
        result = await _call_groq(prompt, model=GROQ_MODEL_FAST, max_tokens=5)
        result = result.strip().upper()
        if result and result[0] in "ABCDE":
            return result[0]
        return None
    except Exception as e:
        logger.error(f"find_correct_answer error: {e}")
        return None
