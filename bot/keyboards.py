from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import MANUAL_MODE
from ai.resume_variants import VARIANT_ORDER, variant_label, normalize_key


def vacancy_keyboard(
    vacancy_id: str, url: str, variant_key: str | None = None
) -> InlineKeyboardMarkup:
    # 3rd row shows the routed résumé variant and opens the override menu. The
    # label is always the EFFECTIVE variant (variant_label normalizes None to
    # the default), so it never lies about which résumé "Откликнуться" will use.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Откликнуться", callback_data=f"apply:{vacancy_id}"
                ),
                InlineKeyboardButton(
                    text="Пропустить", callback_data=f"skip:{vacancy_id}"
                ),
            ],
            [
                InlineKeyboardButton(text="Открыть на hh.ru", url=url),
            ],
            [
                InlineKeyboardButton(
                    text=f"Резюме: {variant_label(variant_key)} ▸ сменить",
                    callback_data=f"varmenu:{vacancy_id}",
                ),
            ],
        ]
    )


def variants_keyboard(
    vacancy_id: str, current_key: str | None = None
) -> InlineKeyboardMarkup:
    """The résumé-override menu: one row per variant (current marked ✓) plus a
    back button. Picking a row fires setvar:{vid}:{key}; back restores the card."""
    current = normalize_key(current_key)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if k == current else ''}{variant_label(k)}",
                callback_data=f"setvar:{vacancy_id}:{k}",
            )
        ]
        for k in VARIANT_ORDER
    ]
    rows.append(
        [InlineKeyboardButton(text="← назад", callback_data=f"varback:{vacancy_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cover_letter_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    # Manual mode: the bot only drafts — you send on hh.ru yourself. Hide the
    # auto-apply "Отправить" button (it submits via the browser, which is
    # exactly what manual mode avoids). Keep rewrite/cancel.
    if MANUAL_MODE:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Переписать",
                        callback_data=f"rewrite:{vacancy_id}",
                    ),
                    InlineKeyboardButton(
                        text="Поправить", callback_data=f"edit:{vacancy_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена", callback_data=f"cancel:{vacancy_id}"
                    ),
                ],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить",
                    callback_data=f"send:{vacancy_id}",
                ),
                InlineKeyboardButton(
                    text="Переписать",
                    callback_data=f"rewrite:{vacancy_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Поправить", callback_data=f"edit:{vacancy_id}"
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=f"cancel:{vacancy_id}"
                ),
            ],
        ]
    )


def tg_vacancy_keyboard(vacancy_id: str, url: str) -> InlineKeyboardMarkup:
    """Keyboard for a Telegram-sourced vacancy card.

    Unlike hh.ru vacancies there is NO auto-apply — on Telegram you apply
    by messaging a recruiter / filling a form yourself. So the actions are:
    open the original post, draft a message you can copy-paste, or skip.
    The "Пропустить" button reuses the existing `skip:` callback (marks the
    vacancy skipped and removes the card).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Открыть в Telegram", url=url),
            ],
            [
                InlineKeyboardButton(
                    text="Черновик письма", callback_data=f"tgdraft:{vacancy_id}"
                ),
                InlineKeyboardButton(
                    text="Пропустить", callback_data=f"skip:{vacancy_id}"
                ),
            ],
        ]
    )


def tg_draft_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    """Keyboard under a drafted message for a Telegram vacancy.

    No "Отправить" — the bot can't send on the user's behalf in a foreign
    channel/DM. The draft is for copy-paste. Offer regenerate + dismiss.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Переписать", callback_data=f"tgredraft:{vacancy_id}"
                ),
                InlineKeyboardButton(
                    text="Готово", callback_data=f"tgdone:{vacancy_id}"
                ),
            ],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data="confirm_yes"),
                InlineKeyboardButton(text="Нет", callback_data="confirm_no"),
            ],
        ]
    )
