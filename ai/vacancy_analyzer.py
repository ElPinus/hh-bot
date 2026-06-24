"""Deep vacancy analyzer for high-quality cover letter generation.

Stage 1 of the two-stage pipeline:
  vacancy -> analyze_vacancy_deep -> structured analysis
  candidate + analysis -> generate_cover_letter -> letter

Uses deepseek-v4-pro with thinking enabled to actually reason about the
vacancy: what kind of company, what level of role, what pain points,
what hidden expectations, what to emphasise from the candidate profile.

The output is a Python dict with fields the cover-letter generator can
target precisely instead of writing in generalities.
"""
import json
import logging
import re
from pathlib import Path

from ai.deepseek_client import deepseek_chat, PRO_MODEL, DEFAULT_MODEL

logger = logging.getLogger(__name__)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_CANDIDATE_PATH = _PROMPTS_DIR / "candidate.txt"
_CANDIDATE_EXAMPLE_PATH = _PROMPTS_DIR / "candidate.example.txt"


def _load_candidate_context() -> str:
    if _CANDIDATE_PATH.exists():
        return _CANDIDATE_PATH.read_text(encoding="utf-8").strip()
    if _CANDIDATE_EXAMPLE_PATH.exists():
        return _CANDIDATE_EXAMPLE_PATH.read_text(encoding="utf-8").strip()
    return ""


_ANALYZER_SYSTEM_PROMPT = """Ты — senior tech-рекрутер с инженерным бэкграундом.
Твоя задача — прочитать вакансию глазами и кандидата, и hiring manager'а
одновременно, и подготовить чёткий разбор для следующего этапа: написания
сопроводительного письма.

Не пиши письмо. Пиши разбор в формате JSON ровно по схеме ниже. Никакого
другого текста, никаких комментариев, никакого markdown — только валидный
JSON-объект.

Схема:
{
  "company_type": "стартап / средняя tech-компания / крупная корпорация / агентство / государственный сектор / неизвестно",
  "company_domain": "одна короткая фраза о домене бизнеса компании, например 'B2B SaaS для retail' или 'финтех-приложение для физлиц'",
  "role_level": "junior / middle / senior / lead / head / director / неясно",
  "role_type": "реальный тип роли вакансии — например Product Manager / Project Manager / Implementation / Engineer / Analyst / Lead / другое",
  "ai_focus_intensity": "high (LLM/ML core продукта) / medium (AI как фича) / low (AI не упомянут или вскользь)",
  "main_pains": [
    "Конкретная боль или задача компании — что они хотят решить через найм. Не общие слова, а конкретика из текста вакансии. Список из 1-3 пунктов в порядке важности."
  ],
  "tech_requirements": [
    "Технологии / стек / методологии, явно упомянутые в вакансии. Только то, что реально написано."
  ],
  "instructions_in_letter": "Если в вакансии есть прямое требование что-то указать или написать в сопроводительном — точная цитата или пересказ. Иначе null.",
  "vacancy_tone": "формальный / нейтрально-деловой / дружелюбный-стартаповский / агрессивно-молодёжный",
  "red_flags": [
    "Любые red flags для этой конкретной вакансии: оффлайн при declared 'удалёнка', неадекватные требования (8 языков в 2 года), вилка ниже рынка, размытый scope, и т.п. Список из 0-3 пунктов."
  ],
  "best_fit_from_candidate": "Какой ОДИН проект или факт ИЗ ПРОФАЙЛА кандидата лучше всего привязать к этой вакансии. Бери конкретный продукт / проект / достижение из профайла кандидата, а не общую формулировку.",
  "key_tech_fact": "Один конкретный технический факт из профайла кандидата, который напрямую отвечает на ГЛАВНУЮ боль вакансии. Пиши целое предложение, его можно вставить в письмо как есть.",
  "stack_overlap": [
    "Технологии, которые есть И в требованиях вакансии, И в стеке кандидата. Пустой список если нет пересечений."
  ],
  "what_candidate_lacks": [
    "Технологии или опыт, явно требуемый вакансией, которого НЕТ в профайле кандидата. Пустой список если нет gap."
  ],
  "honest_pivot": "Не используется. Всегда возвращай null — пробелы в письме не упоминаются (тема просто опускается, не проговаривается).",
  "must_avoid_in_letter": [
    "Что КАТЕГОРИЧЕСКИ нельзя писать в этом конкретном письме. Например: 'не упоминать имя текущего работодателя кандидата (NDA)', 'не натягивать навык, которого нет в профайле', 'не использовать стартаповский тон в письме к крупной корпорации'."
  ],
  "letter_strategy": "Одной фразой — стратегия письма: с какого факта/проекта из профайла кандидата открыть, к какой боли вакансии привязать, какой пробел обойти молчанием, чем закрыть. Конкретно под эту вакансию.",
  "recommended_length": "short (60-90 слов) / medium (90-130 слов) / long (130-180 слов) — зависит от наличия инструкции в вакансии и уровня роли"
}

Будь конкретным, не пиши общих слов. Если чего-то в вакансии нет — пиши null
или пустой список, не выдумывай.

Помни: твой разбор увидит другая модель, которая на его основе напишет
финальное письмо. Чем точнее разбор — тем лучше письмо."""


async def analyze_vacancy_deep(vacancy: dict) -> dict:
    """Return a structured analysis of the vacancy for cover letter targeting.

    On any error returns an empty dict — generate_cover_letter then falls
    back to a single-stage flow.
    """
    title = vacancy.get("title", "")
    company = vacancy.get("company", "")
    description = vacancy.get("description", "")

    if len(description) > 4000:
        description = description[:4000] + "..."

    candidate = _load_candidate_context()

    user_prompt = f"""ВАКАНСИЯ:
Должность: {title}
Компания: {company}

Описание:
{description}


ПРОФАЙЛ КАНДИДАТА:
{candidate}


Верни структурированный разбор по схеме из системного промта. Только JSON,
ничего больше."""

    # pro -> flash fallback on the SAME provider: under load pro overloads
    # first (the "900-second timeout"); flash still answers and takes the full
    # prompt (no Groq 413). thinking=False: thinking mode dumped reasoning prose
    # instead of JSON, breaking parsing — pro/flash without thinking honour
    # response_format=json_object reliably.
    raw = None
    for ds_model in (PRO_MODEL, DEFAULT_MODEL):
        try:
            raw = await deepseek_chat(
                messages=[
                    {"role": "system", "content": _ANALYZER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=4000,
                model=ds_model,
                thinking=False,
                response_format={"type": "json_object"},
            )
            break
        except Exception as e:
            logger.warning("vacancy_analyzer %s failed: %s", ds_model, e)
    if raw is None:
        return {}

    # Extract JSON block — model may wrap in ``` or add brief text
    text = raw.strip()
    # try fenced block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    else:
        # try first { ... last }
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            text = text[first : last + 1]

    try:
        analysis = json.loads(text)
        logger.info(
            "Vacancy analysis: role_type=%s level=%s ai_focus=%s pains=%d "
            "stack_overlap=%d gaps=%d strategy=%s",
            analysis.get("role_type", "?"),
            analysis.get("role_level", "?"),
            analysis.get("ai_focus_intensity", "?"),
            len(analysis.get("main_pains") or []),
            len(analysis.get("stack_overlap") or []),
            len(analysis.get("what_candidate_lacks") or []),
            (analysis.get("letter_strategy") or "")[:80],
        )
        return analysis
    except json.JSONDecodeError as e:
        logger.warning("vacancy_analyzer JSON parse failed: %s; raw[:300]=%r", e, raw[:300])
        return {}
