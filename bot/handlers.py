import asyncio
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import TELEGRAM_ADMIN_ID
from bot.keyboards import (
    vacancy_keyboard,
    cover_letter_keyboard,
    tg_draft_keyboard,
    variants_keyboard,
)
from db.storage import (
    save_vacancy,
    save_response,
    mark_skipped,
    get_stats,
    save_filter,
    get_active_filters,
    get_unscored_vacancies,
    update_vacancy_relevance,
    set_vacancy_variant,
    get_vacancy_variant,
    log_action,
)
from ai.resume_variants import variant_label
from ai.resume_feeds import apply_resume_for, apply_resume_hash_for
from parser.hh_client import HHClient
from ai.analyzer import analyze_relevance
from ai.cover_letter import generate_cover_letter, edit_letter
from ai.variant_router import select_resume_variant

logger = logging.getLogger(__name__)
router = Router()

# shared state
hh_client: HHClient | None = None
pending_letters: dict[str, dict] = {}  # vacancy_id -> {letter, vacancy}


def set_hh_client(client: HHClient):
    global hh_client
    hh_client = client


def _is_admin(user_id: int) -> bool:
    return user_id == TELEGRAM_ADMIN_ID


class FilterStates(StatesGroup):
    waiting_name = State()
    waiting_keywords = State()
    waiting_city = State()
    waiting_salary = State()
    waiting_experience = State()


class EditStates(StatesGroup):
    waiting_instruction = State()


# --- Commands ---


@router.message(Command("start"))
async def cmd_start(message: Message):
    if not _is_admin(message.from_user.id):
        await message.answer("Доступ запрещен.")
        return

    logged_in = hh_client and hh_client.is_logged_in_sync()
    status = "авторизован" if logged_in else "не авторизован (используй /login)"

    await message.answer(
        f"hh.ru бот готов к работе.\n"
        f"Статус hh.ru: {status}\n\n"
        "Команды:\n"
        "/login - авторизация на hh.ru\n"
        "/search - ручной поиск вакансий\n"
        "/autopilot - запустить цикл автопилота вручную\n"
        "/recheck - переоценить вакансии без оценки\n"
        "/filters - управление фильтрами\n"
        "/addfilter - добавить фильтр\n"
        "/stats - статистика откликов\n\n"
        "Автопилот (автоотклик ВЫКЛЮЧЕН — всё на ручной просмотр):\n"
        "10+ баллов = карточка тебе (90+ помечается 🔥 СИЛЬНОЕ)\n"
        "<10 = автопропуск\n"
        "Откликаешься сам кнопкой «Откликнуться»."
    )


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    if not hh_client:
        await message.answer("hh.ru клиент не инициализирован.")
        return

    await message.answer(
        "Открываю браузер для авторизации.\n"
        "На твоём компьютере появится окно браузера.\n"
        "Залогинься на hh.ru вручную (капча, код, что угодно).\n"
        "Бот подхватит сессию автоматически. Таймаут: 5 минут."
    )

    result = await hh_client.login_interactive()

    if result == "success":
        await message.answer("Авторизация успешна. Можно искать вакансии: /search")
        log_action("login", "success")
    elif result.startswith("error:"):
        error = result.split(":", 1)[1]
        await message.answer(f"Ошибка авторизации: {error}")
    else:
        await message.answer(f"Неизвестный результат: {result}")


@router.message(Command("autopilot"))
async def cmd_autopilot(message: Message):
    if not _is_admin(message.from_user.id):
        return

    if not hh_client:
        await message.answer("hh.ru клиент не инициализирован.")
        return

    if not await hh_client._is_logged_in():
        await message.answer("Сначала авторизуйся: /login")
        return

    await message.answer("Запускаю цикл автопилота...")

    from bot.autopilot import run_search_cycle, send_summary
    await run_search_cycle(message.bot, hh_client)
    await send_summary(message.bot)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _is_admin(message.from_user.id):
        return

    stats = get_stats()
    await message.answer(
        f"Статистика:\n"
        f"Всего вакансий: {stats['total']}\n"
        f"Откликнулся: {stats['sent']}\n"
        f"Пропущено: {stats['skipped']}\n"
        f"Ожидают: {stats['pending']}"
    )


@router.message(Command("filters"))
async def cmd_filters(message: Message):
    if not _is_admin(message.from_user.id):
        return

    filters = get_active_filters()
    if not filters:
        await message.answer(
            "Нет активных фильтров. Добавь через /addfilter"
        )
        return

    text = "Активные фильтры:\n\n"
    for f in filters:
        text += (
            f"#{f['id']} {f['name']}\n"
            f"  Ключевые слова: {f['keywords']}\n"
            f"  Город: {f['city'] or 'любой'}\n"
            f"  Зарплата от: {f['salary_from'] or 'не указана'}\n"
            f"  Опыт: {f['experience'] or 'любой'}\n\n"
        )
    await message.answer(text)


@router.message(Command("addfilter"))
async def cmd_addfilter(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return

    await message.answer("Название фильтра (например: PM remote):")
    await state.set_state(FilterStates.waiting_name)


@router.message(FilterStates.waiting_name)
async def filter_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer(
        "Ключевые слова для поиска (например: руководитель проектов):"
    )
    await state.set_state(FilterStates.waiting_keywords)


@router.message(FilterStates.waiting_keywords)
async def filter_keywords(message: Message, state: FSMContext):
    await state.update_data(keywords=message.text)
    await message.answer("Город (или 'любой'):")
    await state.set_state(FilterStates.waiting_city)


@router.message(FilterStates.waiting_city)
async def filter_city(message: Message, state: FSMContext):
    city = message.text if message.text.lower() != "любой" else ""
    await state.update_data(city=city)
    await message.answer("Зарплата от (число или 'любая'):")
    await state.set_state(FilterStates.waiting_salary)


@router.message(FilterStates.waiting_salary)
async def filter_salary(message: Message, state: FSMContext):
    salary = None
    if message.text.lower() != "любая":
        try:
            salary = int(message.text.replace(" ", ""))
        except ValueError:
            pass
    await state.update_data(salary_from=salary)
    await message.answer(
        "Опыт работы:\n"
        "noExperience - нет опыта\n"
        "between1And3 - 1-3 года\n"
        "between3And6 - 3-6 лет\n"
        "moreThan6 - более 6 лет\n"
        "Или 'любой':"
    )
    await state.set_state(FilterStates.waiting_experience)


@router.message(FilterStates.waiting_experience)
async def filter_experience(message: Message, state: FSMContext):
    exp = message.text if message.text.lower() != "любой" else ""
    data = await state.get_data()
    data["experience"] = exp

    save_filter(data)
    log_action("filter_added", data["name"])

    await message.answer(f"Фильтр '{data['name']}' сохранен.")
    await state.clear()


@router.message(Command("search"))
async def cmd_search(message: Message):
    if not _is_admin(message.from_user.id):
        return

    if not hh_client:
        await message.answer("hh.ru клиент не инициализирован.")
        return

    if not await hh_client._is_logged_in():
        await message.answer("Сначала авторизуйся: /login")
        return

    filters = get_active_filters()
    if not filters:
        await message.answer("Нет фильтров. Добавь через /addfilter")
        return

    await message.answer("Ищу вакансии...")

    total_found = 0
    for f in filters:
        try:
            vacancies = await hh_client.search_vacancies(f)

            for v in vacancies:
                desc = await hh_client.get_vacancy_description(v["url"])
                v["description"] = desc

                analysis = await analyze_relevance(v)
                v.update(analysis)

                if save_vacancy(v):
                    total_found += 1
                    relevance_emoji = {
                        "high": "[!!!]",
                        "medium": "[!!]",
                        "low": "[!]",
                    }.get(v.get("relevance", ""), "[?]")

                    text = (
                        f"{relevance_emoji} {v['title']}\n"
                        f"{v['company']}\n"
                        f"Зарплата: {v.get('salary', 'не указана')}\n"
                        f"Город: {v.get('city', '')}\n"
                        f"Релевантность: {v.get('relevance_score', 0)}/100\n"
                        f"Причина: {v.get('reason', '')}"
                    )

                    await message.answer(
                        text, reply_markup=vacancy_keyboard(v["id"], v["url"])
                    )

                    # rate limit cooldown
                    await asyncio.sleep(5)

            log_action("search", f"filter={f['name']}, found={len(vacancies)}")

        except Exception as e:
            logger.error("Search error for filter %s: %s", f["name"], e)
            await message.answer(f"Ошибка поиска по фильтру '{f['name']}': {e}")

    await message.answer(f"Поиск завершен. Новых вакансий: {total_found}")


@router.message(Command("recheck"))
async def cmd_recheck(message: Message):
    if not _is_admin(message.from_user.id):
        return

    vacancies = get_unscored_vacancies()
    if not vacancies:
        await message.answer("Нет вакансий без оценки.")
        return

    await message.answer(f"Переоцениваю {len(vacancies)} вакансий через GLM...")

    done = 0
    for v in vacancies:
        try:
            analysis = await analyze_relevance(v)
            update_vacancy_relevance(
                v["id"],
                analysis.get("relevance", "unknown"),
                analysis.get("relevance_score", 0),
                analysis.get("reason", ""),
            )
            done += 1

            relevance_emoji = {
                "high": "[!!!]",
                "medium": "[!!]",
                "low": "[!]",
            }.get(analysis.get("relevance", ""), "[?]")

            text = (
                f"{relevance_emoji} {v['title']}\n"
                f"{v.get('company', '')}\n"
                f"Зарплата: {v.get('salary', 'не указана')}\n"
                f"Город: {v.get('city', '')}\n"
                f"Релевантность: {analysis.get('relevance_score', 0)}/100\n"
                f"Причина: {analysis.get('reason', '')}"
            )

            await message.answer(
                text, reply_markup=vacancy_keyboard(v["id"], v["url"])
            )

            # rate limit cooldown
            await asyncio.sleep(5)
        except Exception as e:
            logger.error("Recheck error for %s: %s", v["id"], e)
            await asyncio.sleep(3)

    await message.answer(f"Переоценка завершена: {done}/{len(vacancies)}")
    log_action("recheck", f"done={done}")


# --- Apply by pasted vacancy link ---


@router.message(F.text.contains("hh.ru/vacancy/"))
async def on_vacancy_link(message: Message):
    """User pasted an hh.ru vacancy URL — fetch it, score it, draft a
    cover letter, and show it with the standard Send/Rewrite/Cancel
    keyboard. Reuses the existing cb_send / cb_rewrite / cb_cancel flow.
    """
    if not _is_admin(message.from_user.id):
        return

    if not hh_client:
        await message.answer("hh.ru клиент не инициализирован.")
        return

    vacancy_id = HHClient.extract_vacancy_id(message.text)
    if not vacancy_id:
        await message.answer(
            "Не смог разобрать ссылку. Жду ссылку вида "
            "https://hh.ru/vacancy/123456789"
        )
        return

    # Browser access is shared with the autopilot — serialise via the lock.
    from bot.autopilot import HH_LOCK

    async with HH_LOCK:
        if not await hh_client._is_logged_in():
            await message.answer("Сначала авторизуйся: /login")
            return

        # Already applied? Tell the user instead of double-sending.
        from db.models import get_connection
        conn = get_connection()
        existing = conn.execute(
            "SELECT status FROM responses WHERE vacancy_id = ?",
            (vacancy_id,),
        ).fetchone()
        conn.close()
        if existing and existing["status"] == "sent":
            await message.answer(
                f"На эту вакансию уже был отклик (id {vacancy_id}). "
                "Повторно не отправляю."
            )
            return

        await message.answer(f"Открываю вакансию {vacancy_id}, читаю...")
        vacancy = await hh_client.get_vacancy_by_url(message.text)

    if not vacancy:
        await message.answer(
            "Не удалось прочитать вакансию. Возможные причины: ссылка "
            "битая, вакансия удалена, или hh.ru показал анти-бот "
            "проверку. Попробуй позже или открой /login."
        )
        return

    # Score it — even though the user picked it manually, the score and
    # reason are useful context before sending.
    await message.answer("Оцениваю релевантность...")
    try:
        analysis = await analyze_relevance(vacancy)
        vacancy.update(analysis)
    except Exception as e:
        logger.warning("on_vacancy_link: analyze failed for %s: %s",
                       vacancy_id, e)
        vacancy.setdefault("relevance_score", 0)
        vacancy.setdefault("relevance", "unknown")
        vacancy.setdefault("reason", "анализ не удался")

    # Route to the best-fitting résumé variant (a pasted link has no source
    # feed, so this is pure semantic). Stored so the letter register and the
    # apply-modal résumé selection both honour it.
    variant_key, _vr = await select_resume_variant(vacancy)
    vacancy["resume_variant"] = variant_key

    save_vacancy(vacancy)

    score = vacancy.get("relevance_score", 0)
    relevance_emoji = {
        "high": "[!!!]", "medium": "[!!]", "low": "[!]",
    }.get(vacancy.get("relevance", ""), "[?]")
    await message.answer(
        f"{relevance_emoji} {vacancy['title']}\n"
        f"{vacancy.get('company', '')}\n"
        f"Зарплата: {vacancy.get('salary') or 'не указана'}\n"
        f"Город: {vacancy.get('city', '')}\n"
        f"Релевантность: {score}/100\n"
        f"Резюме: {variant_label(variant_key)}\n"
        f"Причина: {vacancy.get('reason', '')}\n\n"
        "Генерирую сопроводительное..."
    )

    letter = await generate_cover_letter(vacancy)
    if not letter:
        await message.answer(
            "Не удалось сгенерировать сопроводительное. Попробуй позже."
        )
        return

    pending_letters[vacancy_id] = {"letter": letter, "vacancy": vacancy}
    await message.answer(
        f"Сопроводительное:\n\n{letter}",
        reply_markup=cover_letter_keyboard(vacancy_id),
    )
    log_action("link_apply_draft", f"vacancy={vacancy_id} score={score}")


# --- Callbacks ---


@router.callback_query(F.data.startswith("apply:"))
async def cb_apply(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    # Ack now — cover-letter generation takes 30-60s, callback expires in ~15s.
    await callback.answer()

    vacancy_id = callback.data.split(":", 1)[1]

    # Remember the vacancy-card message id — when Send/Cancel eventually
    # fires we delete this together with the cover-letter card so the
    # chat self-cleans instead of accumulating dead cards.
    card_msg_id = callback.message.message_id

    await callback.message.edit_reply_markup(reply_markup=None)
    progress_msg = await callback.message.answer("Генерирую сопроводительное...")

    from db.models import get_connection

    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)
    ).fetchone()
    conn.close()

    if not row:
        await callback.message.answer("Вакансия не найдена в базе.")
        return

    vacancy = dict(row)
    letter = await generate_cover_letter(vacancy)

    # Best-effort cleanup of the progress hint — keeps chat tight.
    try:
        await progress_msg.delete()
    except Exception:
        pass

    if not letter:
        await callback.message.answer(
            "Не удалось сгенерировать сопроводительное. Попробуй позже."
        )
        return

    letter_msg = await callback.message.answer(
        f"Сопроводительное:\n\n{letter}",
        reply_markup=cover_letter_keyboard(vacancy_id),
    )

    pending_letters[vacancy_id] = {
        "letter": letter,
        "vacancy": vacancy,
        "card_msg_id": card_msg_id,        # the original vacancy card
        "letter_msg_id": letter_msg.message_id,  # the cover-letter card
        "chat_id": callback.message.chat.id,
    }


async def _delete_silent(bot, chat_id: int, message_id: int) -> None:
    """delete_message that never raises (best-effort chat cleanup).

    Telegram returns errors for: messages older than 48h, already-deleted
    messages, messages the bot didn't author. None of those matter for a
    cleanup attempt — swallow them.
    """
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


@router.callback_query(F.data.startswith("send:"))
async def cb_send(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    # Ack the button NOW — the browser apply takes 30-60s, but a callback query
    # expires in ~15s; answering at the end throws "query is too old".
    await callback.answer("Отправляю отклик...")

    vacancy_id = callback.data.split(":", 1)[1]
    data = pending_letters.pop(vacancy_id, None)

    if not data or not hh_client:
        await callback.message.answer("Данные не найдены или клиент не готов.")
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    progress = await callback.message.answer("Отправляю отклик...")

    # Browser access is shared with the autopilot loop — serialise via
    # the lock so a manual "Send" can't clobber an in-flight autopilot
    # navigation (or vice versa).
    from bot.autopilot import HH_LOCK
    # Apply with the routed résumé variant (hash preferred, title fallback;
    # both None -> hh default). apply_to_vacancy fails closed if it can't
    # confirm the résumé, so a wrong-résumé send is impossible.
    variant = (data.get("vacancy") or {}).get("resume_variant")
    resume_identifier = apply_resume_for(variant)
    resume_hash = apply_resume_hash_for(variant)
    async with HH_LOCK:
        try:
            success = await hh_client.apply_to_vacancy(
                vacancy_id, data["letter"],
                resume_identifier=resume_identifier, resume_hash=resume_hash,
            )
        except Exception:
            # apply_to_vacancy is meant to return a bool, but a browser/proxy
            # error (e.g. navigation timeout) can still raise. Swallow it to a
            # failure result so the user always gets feedback instead of a
            # frozen "Отправляю отклик..." message.
            logger.exception("apply_to_vacancy raised for %s", vacancy_id)
            success = False

    if success:
        save_response(vacancy_id, data["letter"], "sent")
        log_action("applied", f"vacancy={vacancy_id}")
        # Self-clean: delete the original vacancy card, the cover-letter
        # card, and the "sending..." progress hint. Leave only a short
        # confirmation with the title so the chat keeps a record.
        chat_id = data.get("chat_id") or callback.message.chat.id
        for mid in (data.get("card_msg_id"), data.get("letter_msg_id"), progress.message_id):
            if mid:
                await _delete_silent(callback.bot, chat_id, mid)
        title = (data.get("vacancy") or {}).get("title") or "вакансию"
        await callback.message.answer(f"✓ Отклик отправлен: {title}")
    else:
        # Keep the cards visible so the user can see what failed.
        await _delete_silent(callback.bot, callback.message.chat.id, progress.message_id)
        # Always include the vacancy title + link so the user knows WHICH one
        # failed — this note can land far below the original card in the chat.
        vac = data.get("vacancy") or {}
        title = vac.get("title") or "вакансию"
        url = vac.get("url") or f"https://hh.ru/vacancy/{vacancy_id}"
        await callback.message.answer(
            f"⚠️ Не удалось отправить отклик: {title}\n"
            f"{url}\n\n"
            "Скорее всего нужен доп-шаг (вопросы работодателя / внешняя форма / "
            "релокация) — откликнись вручную. Либо ты уже откликался или "
            "вакансия закрыта."
        )


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    # split(":", 1): Telegram vacancy ids are "tg:<channel>:<msgid>" (they
    # contain colons). A bare split(":")[1] would yield just "tg". hh.ru ids
    # are colon-free, so this is equivalent for them.
    vacancy_id = callback.data.split(":", 1)[1]
    mark_skipped(vacancy_id)
    # Self-clean: delete the vacancy card silently (no confirmation
    # message — the disappearance is the confirmation).
    await _delete_silent(callback.bot, callback.message.chat.id, callback.message.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("rewrite:"))
async def cb_rewrite(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    vacancy_id = callback.data.split(":")[1]
    data = pending_letters.get(vacancy_id)

    if not data:
        await callback.message.answer("Данные вакансии не найдены. Нажми 'Откликнуться' заново.")
        await callback.answer()
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Переписываю...")

    letter = await generate_cover_letter(data["vacancy"])

    if not letter:
        await callback.message.answer("Не удалось сгенерировать. Попробуй позже.")
        await callback.answer()
        return

    pending_letters[vacancy_id] = {"letter": letter, "vacancy": data["vacancy"]}

    await callback.message.answer(
        f"Сопроводительное:\n\n{letter}",
        reply_markup=cover_letter_keyboard(vacancy_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    """User wants to tweak the draft with a free-text instruction. Ask for it
    and remember which vacancy we're editing — the edit itself is applied in
    edit_instruction (the next text message)."""
    if not _is_admin(callback.from_user.id):
        return

    vacancy_id = callback.data.split(":", 1)[1]
    if not pending_letters.get(vacancy_id):
        await callback.answer(
            "Черновик не найден — нажми «Откликнуться» заново.", show_alert=True
        )
        return

    await state.set_state(EditStates.waiting_instruction)
    await state.update_data(edit_vacancy_id=vacancy_id)
    await callback.answer()
    await callback.message.answer(
        "Напиши одним сообщением, что поправить — применю ТОЛЬКО это, "
        "остальной текст не трону.\n"
        "Например: «убери последнюю строку про стек», «второй абзац короче», "
        "«добавь, что готов созвониться на этой неделе»."
    )


@router.message(EditStates.waiting_instruction)
async def edit_instruction(message: Message, state: FSMContext):
    """Apply the user's free-text edit to the pending letter, surgically."""
    if not _is_admin(message.from_user.id):
        return

    st = await state.get_data()
    vacancy_id = st.get("edit_vacancy_id")
    await state.clear()

    data = pending_letters.get(vacancy_id)
    if not data:
        await message.answer("Черновик не найден — нажми «Откликнуться» заново.")
        return

    instruction = (message.text or "").strip()
    if not instruction:
        await message.answer("Пустая правка — оставил прежнюю версию.")
        return

    progress = await message.answer("Применяю правку...")
    try:
        new_letter = await edit_letter(data["letter"], instruction)
    except Exception as e:
        logger.error("edit_letter failed for %s: %s", vacancy_id, e)
        await progress.edit_text("Не удалось применить правку. Попробуй ещё раз.")
        return

    await _delete_silent(message.bot, progress.chat.id, progress.message_id)

    if not new_letter:
        await message.answer("Правка дала пустой результат — оставил прежнюю версию.")
        return

    data["letter"] = new_letter  # keep card_msg_id / chat_id / vacancy intact
    letter_msg = await message.answer(
        f"Сопроводительное (с правкой):\n\n{new_letter}",
        reply_markup=cover_letter_keyboard(vacancy_id),
    )
    data["letter_msg_id"] = letter_msg.message_id
    log_action("letter_edit", f"vacancy={vacancy_id}")


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    vacancy_id = callback.data.split(":")[1]
    data = pending_letters.pop(vacancy_id, None)
    # Self-clean: drop the cover-letter card AND the original vacancy
    # card. No confirmation message needed.
    chat_id = (data or {}).get("chat_id") or callback.message.chat.id
    for mid in (
        (data or {}).get("card_msg_id"),
        callback.message.message_id,  # the cover-letter card we're acting on
    ):
        if mid:
            await _delete_silent(callback.bot, chat_id, mid)
    await callback.answer()


# --- Résumé-variant override (the card's "Сменить резюме" button) ---
# varmenu -> show the variant menu; setvar -> store the pick + restore the
# card with the updated label; varback -> restore the card unchanged. The
# stored variant drives the letter register (cb_apply reads the DB row) and the
# résumé picked in the apply modal. hh vacancy ids are colon-free, so
# setvar:{vid}:{key} splits cleanly with rsplit.


@router.callback_query(F.data.startswith("varmenu:"))
async def cb_varmenu(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    vacancy_id = callback.data.split(":", 1)[1]
    current = get_vacancy_variant(vacancy_id)
    await callback.message.edit_reply_markup(
        reply_markup=variants_keyboard(vacancy_id, current)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("setvar:"))
async def cb_setvar(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    # data = "setvar:{vid}:{key}". hh ids carry no colons, so the LAST colon
    # splits off the variant key and leaves the id intact.
    rest = callback.data.split(":", 1)[1]
    vacancy_id, key = rest.rsplit(":", 1)
    set_vacancy_variant(vacancy_id, key)
    vac = _load_vacancy(vacancy_id)
    url = (vac or {}).get("url") or f"https://hh.ru/vacancy/{vacancy_id}"
    # Keep a pending draft's vacancy dict in sync so a later "Переписать" uses
    # the new register without a stale DB re-read.
    pending = pending_letters.get(vacancy_id)
    if pending and isinstance(pending.get("vacancy"), dict):
        pending["vacancy"]["resume_variant"] = key
    await callback.message.edit_reply_markup(
        reply_markup=vacancy_keyboard(vacancy_id, url, key)
    )
    await callback.answer(f"Резюме: {variant_label(key)}")
    log_action("variant_override", f"vacancy={vacancy_id} variant={key}")


@router.callback_query(F.data.startswith("varback:"))
async def cb_varback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    vacancy_id = callback.data.split(":", 1)[1]
    vac = _load_vacancy(vacancy_id)
    url = (vac or {}).get("url") or f"https://hh.ru/vacancy/{vacancy_id}"
    current = (vac or {}).get("resume_variant")
    await callback.message.edit_reply_markup(
        reply_markup=vacancy_keyboard(vacancy_id, url, current)
    )
    await callback.answer()


# --- Telegram-sourced vacancy callbacks ---
# These back the tg_vacancy_keyboard / tg_draft_keyboard. There is no
# auto-apply path: a Telegram vacancy is applied to by messaging a recruiter
# yourself, so the bot only DRAFTS a message for you to copy-paste. The
# "Пропустить" button on a TG card reuses the shared `skip:` callback above.
# NOTE: TG vacancy ids are "tg:<channel>:<msgid>" — always split(":", 1).


def _load_vacancy(vacancy_id: str) -> dict | None:
    from db.models import get_connection

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


_TG_DRAFT_NOTE = "Черновик (под hh-формат, подредактируй под личное сообщение):"


@router.callback_query(F.data.startswith("tgdraft:"))
async def cb_tg_draft(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    vacancy_id = callback.data.split(":", 1)[1]
    await callback.answer()
    progress = await callback.message.answer("Генерирую черновик...")

    vacancy = _load_vacancy(vacancy_id)
    if not vacancy:
        await progress.edit_text("Вакансия не найдена в базе.")
        return

    letter = await generate_cover_letter(vacancy)
    await _delete_silent(callback.bot, progress.chat.id, progress.message_id)

    if not letter:
        await callback.message.answer(
            "Не удалось сгенерировать черновик. Попробуй позже."
        )
        return

    await callback.message.answer(
        f"{_TG_DRAFT_NOTE}\n\n{letter}",
        reply_markup=tg_draft_keyboard(vacancy_id),
    )
    log_action("tg_draft", f"vacancy={vacancy_id}")


@router.callback_query(F.data.startswith("tgredraft:"))
async def cb_tg_redraft(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    vacancy_id = callback.data.split(":", 1)[1]
    await callback.answer("Переписываю...")

    vacancy = _load_vacancy(vacancy_id)
    if not vacancy:
        await callback.message.answer("Вакансия не найдена в базе.")
        return

    letter = await generate_cover_letter(vacancy)
    if not letter:
        await callback.message.answer("Не удалось сгенерировать. Попробуй позже.")
        return

    text = f"{_TG_DRAFT_NOTE}\n\n{letter}"
    try:
        await callback.message.edit_text(
            text, reply_markup=tg_draft_keyboard(vacancy_id)
        )
    except Exception:
        # edit_text fails if the new text is identical or the message is too
        # old — fall back to a fresh message.
        await callback.message.answer(
            text, reply_markup=tg_draft_keyboard(vacancy_id)
        )


@router.callback_query(F.data.startswith("tgdone:"))
async def cb_tg_done(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return

    await _delete_silent(
        callback.bot, callback.message.chat.id, callback.message.message_id
    )
    await callback.answer()
