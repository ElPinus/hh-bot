"""Route a vacancy to the single best-fitting résumé variant.

You may publish several hh.ru résumés (one identity each — see
ai/resume_variants.py). For every vacancy the bot must pick ONE to apply with,
and write the cover letter in that variant's register.

Two signals feed the choice:
  1. SOURCE FEED — which résumé's "similar vacancies" feed surfaced it. A soft
     prior only: the feeds overlap, and the early `vacancy_exists` skip lets
     whichever feed runs first claim a vacancy, so the source is order-dependent
     and must not be trusted blindly.
  2. SEMANTIC MATCH — what the role actually is, matched against the variant
     identities. This is the PRIMARY decision; the feed is only a tiebreak.

Kept separate from analyze_relevance on purpose: scoring and routing are
different jobs. One cheap call per NEW vacancy is negligible at the bot's
search cadence.
"""
import json
import logging
import re

from ai.llm_client import llm_chat
from ai.resume_variants import (
    VARIANT_ORDER,
    normalize_key,
    variant_label,
    classifier_catalog,
)

logger = logging.getLogger(__name__)


def _try_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, flags=re.DOTALL)
    if m:
        t = m.group(1)
    else:
        first, last = t.find("{"), t.rfind("}")
        if first >= 0 and last > first:
            t = t[first : last + 1]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _build_prompt(vacancy: dict, source_variant_hint: str | None) -> str:
    title = vacancy.get("title", "")
    company = vacancy.get("company", "")
    description = vacancy.get("description", "") or ""
    if len(description) > 2500:
        description = description[:2500] + "..."

    # Extra signal from the prior analyze_relevance pass (harmless if absent) —
    # the scorer already reasoned about the true role type, AI-centrality, level
    # and the strongest match / main gap. Feed all of it to sharpen routing.
    bits = []
    for label, key in (
        ("role_type", "role_type"),
        ("ai_focus", "ai_focus"),
        ("role_level", "role_level"),
        ("key_match", "key_match"),
        ("key_gap", "key_gap"),
    ):
        val = vacancy.get(key)
        if val:
            bits.append(f"{label}={val}")
    extra = (
        "\nИз предварительного разбора: " + "; ".join(bits) + "."
    ) if bits else ""

    if source_variant_hint and source_variant_hint in VARIANT_ORDER:
        hint_line = (
            f"ПОДСКАЗКА ИЗ ФИДА: вакансия пришла из ленты «Похожие вакансии» "
            f"резюме {variant_label(source_variant_hint)}. Это МЯГКИЙ приоритет "
            f"(ленты резюме пересекаются) - оставь этот вариант, если суть роли "
            f"ему не противоречит, но переопредели, если роль явно ближе к "
            f"другому резюме."
        )
    else:
        hint_line = (
            "ПОДСКАЗКА ИЗ ФИДА: нет (вакансия не из резюме-ленты) - решай чисто "
            "по сути роли."
        )

    return f"""Ты - маршрутизатор резюме. У кандидата несколько опубликованных
резюме под разные роли (каталог ниже). Для ВАКАНСИИ выбери РОВНО ОДНО резюме, с
которым отклик будет сильнее всего. Решай по РЕАЛЬНОЙ сути роли (обязанности +
требования), а не по одному слову в заголовке.

КАТАЛОГ РЕЗЮМЕ:
{classifier_catalog()}

ПРАВИЛА ВЫБОРА:
- Сопоставь суть роли (обязанности + требования) с идентичностями из каталога
  и выбери ближайшую по СУТИ - совпадение по сути важнее одного слова в title.
- Если два резюме близки, выбери то, чьи signals точнее покрывают ОСНОВНУЮ
  функцию роли, а не второстепенные обязанности.
- {hint_line}

ВАКАНСИЯ: {title} в {company}.
{description}{extra}

Верни СТРОГО JSON, без markdown и текста вокруг:
{{"variant": "<ключ резюме из каталога>", "reason": "<одна короткая фраза, почему именно это резюме>"}}"""


async def select_resume_variant(
    vacancy: dict, source_variant_hint: str | None = None
) -> tuple[str, str]:
    """Pick the best résumé variant for a vacancy.

    Returns (variant_key, reason). On any LLM / parse failure falls back
    deterministically to the source-feed hint (when valid) or DEFAULT_VARIANT,
    so a routing failure never blocks the pipeline.
    """
    fallback = normalize_key(source_variant_hint)

    try:
        prompt = _build_prompt(vacancy, source_variant_hint)
        text = await llm_chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=400,
            model_tier="premium",  # runs only on vacancies past the surface gate, so cost is bounded
            response_format={"type": "json_object"},
        )
    except Exception as e:
        logger.warning("variant router LLM failed (%s) - fallback to %s",
                       e, fallback)
        return fallback, "fallback (router error)"

    parsed = _try_json(text)
    if not parsed or "variant" not in parsed:
        logger.warning("variant router unparseable (%r) - fallback to %s",
                       (text or "")[:120], fallback)
        return fallback, "fallback (unparseable)"

    key = str(parsed.get("variant", "")).strip()
    if key not in VARIANT_ORDER:
        logger.warning("variant router bad key %r - fallback to %s",
                       key, fallback)
        return fallback, "fallback (bad key)"

    reason = str(parsed.get("reason") or "").strip()
    logger.info(
        "variant router: %s (hint=%s) - %s",
        variant_label(key), source_variant_hint or "none", reason,
    )
    return key, reason
