"""Multi-résumé support — route each vacancy to the best-fitting résumé and
write the cover letter in that résumé's register.

This is the single source of truth for the résumé variants you target. Define
ONE entry per résumé you publish on hh.ru. For every vacancy the bot picks the
best-fitting variant (see ai/variant_router.py) and writes the cover letter in
that variant's REGISTER — all from ONE prompts/candidate.txt (the register
switches, the profile facts do not).

────────────────────────────────────────────────────────────────────────────
THIS SHIPS AN ILLUSTRATIVE EXAMPLE (3 generic variants). Replace RESUME_VARIANTS
and REGISTER_BASE with your own identities before relying on the feature.
If you publish a SINGLE résumé, just leave the per-variant feeds unconfigured
(resume-variants/feeds.yaml absent) — the bot then falls back to the legacy
single-résumé flow automatically and these definitions go unused.
────────────────────────────────────────────────────────────────────────────

The semi-personal per-variant data — the résumé hash for the search feed and
the résumé title to pick in the apply modal — lives in the local (gitignored)
resume-variants/feeds.yaml (see ai/resume_feeds.py), NOT here. Keep this module
free of personal data so it stays public-safe.

No imports from config — keep this leaf-level so cover_letter / analyzer /
autopilot can import it without cycles.
"""
from __future__ import annotations

# Variant used when nothing routed a vacancy to a specific résumé: a pasted
# link, a Telegram post, or the single-feed setup before feeds.yaml is filled.
# Must be a key of RESUME_VARIANTS below.
DEFAULT_VARIANT = "1"


# Per-variant metadata. EXAMPLE — replace with your own résumés.
#   short            label for the Telegram card and the override menu;
#   desired          the hh "Желаемая должность" (a sensible default apply hint
#                    until feeds.yaml supplies the exact résumé title);
#   register         which REGISTER_BASE family the letter uses;
#   register_accent  variant-specific steering appended to that base — STRUCTURE
#                    ONLY; pull concrete facts/numbers from prompts/candidate.txt
#                    at runtime, never hardcode personal data here;
#   signals          the compact identity the router matches a vacancy against.
RESUME_VARIANTS: dict[str, dict] = {
    "1": {
        "short": "Product Manager",
        "desired": "Product Manager",
        "register": "product",
        "register_accent": (
            "Это продуктовый вариант. Веди продуктом: discovery, гипотезы, "
            "метрики, приоритизация, roadmap, ценность для пользователя. "
            "Техническую глубину подтверждай одним штрихом из профайла, не "
            "телом письма."
        ),
        "signals": (
            "продуктовое управление: discovery, продуктовые метрики, "
            "приоритизация, roadmap, плотная работа с командой разработки"
        ),
    },
    "2": {
        "short": "Engineering Lead",
        "desired": "Engineering Lead / Tech Lead",
        "register": "engineering",
        "register_accent": (
            "Это инженерный вариант. Веди архитектурой и обоснованием решений: "
            "system design, trade-offs, выбор стека, end-to-end разработка. "
            "Стек называй гладко и по делу, не дампом тегов."
        ),
        "signals": (
            "техническое лидерство: архитектура, system design, технические "
            "trade-offs, end-to-end разработка продукта, выбор стека"
        ),
    },
    "3": {
        "short": "Project / Delivery Manager",
        "desired": "Project Manager",
        "register": "leadership",
        "register_accent": (
            "Это управленческий вариант. Веди доставкой и результатом: скоуп, "
            "сроки, бюджет, стейкхолдеры, приёмка, команда. Глубину "
            "подтверждай фактом из профайла, не перечнем стека."
        ),
        "signals": (
            "руководство проектами и доставкой: скоуп, сроки, бюджет, "
            "стейкхолдеры, приёмка, управление командой и подрядчиками"
        ),
    },
}

# Canonical order = insertion order of the variants above.
VARIANT_ORDER = list(RESUME_VARIANTS.keys())


# Register families injected into the cover-letter system prompt in place of a
# single hardcoded register section. Each base carries GENERIC guidance on TONE
# only — the concrete facts and numbers come from the candidate profile
# (prompts/candidate.txt), never from here. register_block() appends the
# variant-specific accent above the chosen base.
REGISTER_BASE: dict[str, str] = {
    "product": """============================================================
РЕГИСТР - ПРОДУКТОВЫЙ (бизнес-результат, не отчёт о реализации)
============================================================

Письмо звучит от человека, который ведёт продукт и мыслит результатом для
бизнеса, а НЕ от инженера, отчитывающегося о коде. Техническую глубину
ПОДТВЕРЖДАЙ одним-двумя штрихами из профайла, но она НЕ составляет тело письма.
- Веди результатом и ценностью, а не реализацией: не «очереди, схема БД,
  деплой», а «какой эффект на бизнес дало решение».
- Технику давай ОБЩО и подчинённо результату: один технический штрих для
  глубины - ок; перечисление стека - нет.
- Продуктовый словарь (гипотеза, метрика, baseline, unit-экономика,
  приоритизация, эффект на бизнес), но без пустого buzz: каждое утверждение
  подкреплено конкретикой / цифрой ИЗ ПРОФАЙЛА, иначе это вода.
- ИСКЛЮЧЕНИЕ - чисто инженерная вакансия: там стек уместен, сдвигай регистр к
  инженерному, но всё равно через «какой эффект давала технология».""",
    "engineering": """============================================================
РЕГИСТР - ИНЖЕНЕРНЫЙ (архитектура и trade-offs)
============================================================

Это технический вариант - стек и технические решения УМЕСТНЫ и ожидаемы. Веди
архитектурой и обоснованием решений, а не продуктовым нарративом, но всё равно
через «какой эффект дала технология».
- Веди trade-offs: выбор архитектуры, стоимость, latency, надёжность, выбор
  «эвристика vs обучение» по цене и срокам, system design.
- Стек называй гладко и по делу (НЕ дамп тегов списком) - из профайла кандидата.
- Числа и факты ИЗ ПРОФАЙЛА подтверждают инженерную зрелость, а не просто
  «я умею X».""",
    "leadership": """============================================================
РЕГИСТР - РУКОВОДИТЕЛЬСКИЙ (результат и управление)
============================================================

Письмо звучит от руководителя, который отвечает за результат - доставку,
команду, рост, - а техническую глубину ПОДТВЕРЖДАЕТ фактом из профайла, а НЕ
перечнем стека.
- Веди бизнес-результатом и управлением: доставка, сроки, бюджет, команда,
  стейкхолдеры, найм.
- Глубину показывай одним фактом из профайла, а НЕ дампом стека.
- Управленческий словарь без пустого buzz: каждое утверждение с конкретикой /
  цифрой ИЗ ПРОФАЙЛА.""",
}


def get_variant(key: str | None) -> dict:
    """Return the variant metadata for `key`, falling back to DEFAULT_VARIANT
    for an unknown / empty key (so a stale DB value never crashes a caller)."""
    if key and key in RESUME_VARIANTS:
        return RESUME_VARIANTS[key]
    return RESUME_VARIANTS[DEFAULT_VARIANT]


def normalize_key(key: str | None) -> str:
    """Coerce an arbitrary value to a valid variant key (DEFAULT if unknown)."""
    return key if (key and key in RESUME_VARIANTS) else DEFAULT_VARIANT


def variant_label(key: str | None) -> str:
    """Short human label for cards / menus, e.g. '#1 Product Manager'."""
    k = normalize_key(key)
    return f"#{k} {RESUME_VARIANTS[k]['short']}"


def register_block(key: str | None) -> str:
    """The register section injected into the cover-letter system prompt:
    the variant's register-family base + its variant-specific accent."""
    v = get_variant(key)
    base = REGISTER_BASE.get(v["register"], REGISTER_BASE[RESUME_VARIANTS[DEFAULT_VARIANT]["register"]])
    return f"{base}\n\n{v['register_accent']}"


def classifier_catalog() -> str:
    """Compact catalog of the variant identities for the router prompt.

    One line per variant: key, short title, desired role and the signals that
    distinguish it. Generated from the registry so the classifier never drifts.
    """
    lines = []
    for k in VARIANT_ORDER:
        v = RESUME_VARIANTS[k]
        lines.append(f"{k}. {v['short']} ({v['desired']}): {v['signals']}.")
    return "\n".join(lines)
