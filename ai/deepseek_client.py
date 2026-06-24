"""DeepSeek API client. OpenAI-compatible format.

Uses deepseek-v4-flash (non-thinking) — latest stable, fast and cheap,
suitable for cover letter generation and vacancy relevance scoring.
"""
import asyncio
import logging
import time

import httpx

from config import DEEPSEEK_API_KEY, GLM_PROXY

logger = logging.getLogger(__name__)

API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
PRO_MODEL = "deepseek-v4-pro"

# Soft cooldown — paid tier is generous, but keep a small floor to
# avoid hammering during burst loops.
_last_request_time = 0.0
MIN_REQUEST_INTERVAL = 1.0  # seconds

# Per-MODEL circuit breaker — pro and flash fail INDEPENDENTLY. Under load the
# heavy `deepseek-v4-pro` overloads first (the "900-second timeout" error),
# while `deepseek-v4-flash` stays up. So the breaker is keyed by model name: a
# pro outage trips ONLY pro and must NOT block the flash fallback (llm_chat
# tries pro -> flash on the same provider before Groq). Without this a pro
# outage stalled / failed every cover letter. asyncio is single-threaded, so a
# plain module dict is race-safe enough here.
_cooldown_until: dict[str, float] = {}
COOLDOWN_S = 120.0


def _trip_breaker(model_name: str) -> None:
    _cooldown_until[model_name] = time.time() + COOLDOWN_S


async def deepseek_chat(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 2000,
    model: str | None = None,
    thinking: bool = False,
    response_format: dict | None = None,
) -> str:
    """Send chat completion request to DeepSeek API. Returns content string.

    Args:
        model: defaults to deepseek-v4-flash. Pass PRO_MODEL for higher quality
            (recommended together with thinking=True for cover letters).
        thinking: enable reasoning mode. Slower and pricier, but writes deeper.
    """
    global _last_request_time

    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is empty")

    model_name = model or DEFAULT_MODEL

    # Per-model breaker open — THIS model failed recently. Skip it (fast) so the
    # caller flips to the next model (flash) / provider instead of waiting.
    if time.time() < _cooldown_until.get(model_name, 0.0):
        raise RuntimeError(f"DeepSeek {model_name} in cooldown after recent failure")

    now = time.time()
    wait_needed = MIN_REQUEST_INTERVAL - (now - _last_request_time)
    if wait_needed > 0:
        await asyncio.sleep(wait_needed)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if response_format:
        payload["response_format"] = response_format

    transport = None
    if GLM_PROXY:
        transport = httpx.AsyncHTTPTransport(proxy=GLM_PROXY)

    async with httpx.AsyncClient(transport=transport, timeout=60) as client:
        for attempt in range(3):
            try:
                _last_request_time = time.time()
                resp = await client.post(API_URL, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.HTTPError) as e:
                # Unresponsive endpoint — retrying the same hung server just
                # stacks more 60s waits. Trip the breaker and fail over to Groq
                # now (a cover letter makes several calls; don't stall each one).
                logger.warning(
                    "DeepSeek %s unreachable (%s) — tripping breaker, failing over",
                    model_name, type(e).__name__,
                )
                _trip_breaker(model_name)
                raise RuntimeError(f"DeepSeek {model_name} unreachable: {e}")

            if resp.status_code == 429:
                wait = 20 * (attempt + 1)  # 20s, 40s, 60s
                logger.warning(
                    "DeepSeek 429, waiting %ds (attempt %d/3)", wait, attempt + 1
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = 10 * (attempt + 1)
                logger.warning(
                    "DeepSeek %d server error, waiting %ds (attempt %d/3)",
                    resp.status_code, wait, attempt + 1,
                )
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()

            if "choices" not in data:
                # e.g. the "900-second timeout" error body — DeepSeek is
                # overloaded. Trip the breaker so the rest of this cover-letter
                # pipeline skips DeepSeek instead of re-hitting the same wall.
                logger.error("DeepSeek %s unexpected response: %s", model_name, str(data)[:300])
                _trip_breaker(model_name)
                raise RuntimeError(f"DeepSeek unexpected: {str(data)[:200]}")

            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if not content:
                # In thinking mode V4 may put the whole answer into
                # reasoning_content. Fall back to it instead of failing.
                content = (msg.get("reasoning_content") or "").strip()
            if not content:
                logger.warning("DeepSeek returned empty content and reasoning")
                raise RuntimeError("DeepSeek returned empty response")
            _cooldown_until[model_name] = 0.0  # healthy — clear this model's breaker
            return content

        # 429 / 5xx retries exhausted — cool down this model and fail over.
        _trip_breaker(model_name)
        raise RuntimeError("DeepSeek API failed after 3 retries")
