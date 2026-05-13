"""Learning loop — detect user feedback and store successful (input → skill_call) pairs.

Positive feedback: "perfecto", "exacto", "genial", "gracias", "bien hecho", etc.
Negative feedback: "no", "incorrecto", "eso no", "equivocado", "mal", etc.

Learned examples are injected into context as few-shots to improve future responses.
"""

import re
import structlog
from uuid import uuid4

from ..db.models import LearningExample
from ..db.session import async_session

logger = structlog.get_logger()

# Positive feedback patterns (Spanish + English)
_POSITIVE_PATTERNS = re.compile(
    r"\b(perfecto|exacto|exactamente|genial|excelente|correcto|bien hecho|"
    r"muy bien|eso es|así es|justo así|gracias|thank you|thanks|great|perfect|"
    r"exactly|correct|well done|nice|awesome|bravo|buen trabajo|lo lograste|"
    r"funcionó|funciona|se ve bien|está bien|eso es lo que|eso quería|"
    r"listo|lo tienes|lo hiciste|lo hiciste bien|muy bueno|de puta madre|"
    r"impressive|amazing|fantastic|brilliant|superb|outstanding|excellent|"
    r"justo lo que|era eso|eso mismo|eso era|sí así|sí exacto|sí perfecto|"
    r"you got it|that's it|that's right|that's correct|that's exactly|"
    r"good job|well done|nice work|good work|keep it up)\b",
    re.IGNORECASE,
)

# Negative feedback patterns
_NEGATIVE_PATTERNS = re.compile(
    r"\b(incorrecto|equivocado|está mal|eso no|no era eso|"
    r"no es correcto|fallaste|wrong|incorrect|that's not|"
    r"not what i|no lo que|no es lo que|eso no es|no funciona|no funcionó|"
    r"vuelve a intentar|intenta de nuevo|try again|inténtalo de nuevo|"
    r"te equivocaste|no es eso|eso está mal|está incorrecto|"
    r"that's wrong|not right|not correct|try again|retry)\b",
    re.IGNORECASE,
)

MAX_EXAMPLES_PER_CHAT = 50
MAX_EXAMPLES_IN_CONTEXT = 5


def detect_feedback(text: str) -> str | None:
    """Return 'positive', 'negative', or None if no feedback detected."""
    text = text.strip()
    word_count = len(text.split())
    # Messages longer than 20 words are usually new instructions, not feedback.
    # Raised from 15 → 20 to capture phrases like "eso es exactamente lo que quería"
    if word_count > 20:
        return None

    if _POSITIVE_PATTERNS.search(text):
        return "positive"
    if _NEGATIVE_PATTERNS.search(text):
        return "negative"
    return None


async def store_example(
    user_input: str,
    skill_calls: str,
    outcome: str,
    chat_id: str = "",
) -> None:
    """Store a (user_input → skill_calls) pair with the given outcome."""
    if not user_input or not skill_calls:
        return
    try:
        async with async_session() as session:
            entry = LearningExample(
                id=str(uuid4()),
                user_input=user_input[:500],
                skill_calls=skill_calls[:2000],
                outcome=outcome,
                chat_id=chat_id,
            )
            session.add(entry)
            await session.commit()
        logger.info("learning.stored", outcome=outcome, chat_id=chat_id)
    except Exception:
        logger.exception("learning.store_error")


async def get_positive_examples(chat_id: str = "", limit: int = MAX_EXAMPLES_IN_CONTEXT) -> list[dict]:
    """Retrieve the most-used positive examples for context injection."""
    from sqlalchemy import select
    results = []
    try:
        async with async_session() as session:
            q = select(LearningExample).where(LearningExample.outcome == "positive")
            if chat_id:
                q = q.where(LearningExample.chat_id == chat_id)
            q = q.order_by(LearningExample.use_count.desc()).limit(limit)
            rows = await session.execute(q)
            for row in rows.scalars():
                results.append({
                    "user_input": row.user_input,
                    "skill_calls": row.skill_calls,
                    "use_count": row.use_count,
                })
    except Exception:
        logger.exception("learning.get_error")
    return results


async def increment_use_count(example_id: str) -> None:
    """Increment use_count for a learning example (called when injected into context)."""
    from sqlalchemy import update
    try:
        async with async_session() as session:
            await session.execute(
                update(LearningExample)
                .where(LearningExample.id == example_id)
                .values(use_count=LearningExample.use_count + 1)
            )
            await session.commit()
    except Exception:
        pass


def format_learned_examples(examples: list[dict]) -> str:
    """Format learned examples as few-shots for system prompt injection."""
    if not examples:
        return ""
    lines = ["[LEARNED FROM YOUR FEEDBACK — these responses worked well:]"]
    for ex in examples:
        lines.append(f"\nUser: {ex['user_input']}")
        lines.append(f"Agent: {ex['skill_calls']}")
    return "\n".join(lines)
