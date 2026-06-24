"""Unified LLM client with provider fallback chain.

Primary: DeepSeek V4 (paid, fast, high quality on Russian).
Fallback: Groq Llama 3.3 70B (free, used when DeepSeek fails or key missing).

Tiers:
- "standard"  -> deepseek-v4-flash, non-thinking. Cheap, fast, good enough
                 for relevance scoring.
- "premium"   -> deepseek-v4-pro, thinking enabled. Higher quality for tasks
                 that benefit from deliberate reasoning (cover letters).
"""
import logging

from config import DEEPSEEK_API_KEY, GROQ_API_KEY
from ai.deepseek_client import deepseek_chat, DEFAULT_MODEL, PRO_MODEL
from ai.groq_client import groq_chat

logger = logging.getLogger(__name__)


async def llm_chat(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 2000,
    model_tier: str = "standard",
    response_format: dict | None = None,
) -> str:
    """Send chat completion with provider fallback.

    Tries DeepSeek first. On any failure, falls back to Groq.

    `response_format` is forwarded to DeepSeek only (Groq ignores it).
    Pass {"type": "json_object"} to force valid JSON output.
    """
    errors: list[str] = []

    if DEEPSEEK_API_KEY:
        # DeepSeek model chain. Premium prefers pro (best quality) but falls
        # back to flash ON THE SAME PROVIDER when pro is overloaded/slow — flash
        # is lighter, usually stays up, and (unlike Groq) takes the full-size
        # cover-letter prompt without a 413 Payload Too Large. Standard tier is
        # flash-only. thinking=False: thinking mode dumps reasoning into
        # reasoning_content and can run out of tokens before the real answer.
        ds_models = (
            [PRO_MODEL, DEFAULT_MODEL] if model_tier == "premium"
            else [DEFAULT_MODEL]
        )
        for ds_model in ds_models:
            try:
                return await deepseek_chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=ds_model,
                    thinking=False,
                    response_format=response_format,
                )
            except Exception as e:
                logger.warning("DeepSeek %s failed: %s", ds_model, e)
                errors.append(f"deepseek/{ds_model}={e}")

    if GROQ_API_KEY:
        try:
            return await groq_chat(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as e:
            logger.error("Groq fallback also failed: %s", e)
            errors.append(f"groq={e}")

    raise RuntimeError(
        f"All LLM providers failed: {'; '.join(errors) or 'no keys configured'}"
    )
