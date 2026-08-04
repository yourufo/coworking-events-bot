"""
Обработчики команд про события. Пока только `/add_event` (раздел 3.1 брифа).

Как устроен пошаговый диалог (FSM — "конечный автомат")
---------------------------------------------------------
Обычный обработчик (см. handlers/start.py) реагирует на одно сообщение и
сразу отвечает. Но `/add_event` — это диалог из нескольких шагов: бот
спрашивает название, потом дату, потом время, потом описание, и должен
на каждом шаге
"помнить", на каком именно шаге находится каждый конкретный пользователь
в каждом конкретном чате (мало ли кто-то параллельно начнёт тот же диалог
в другом чате).

Для этого в aiogram есть FSMContext ("Finite State Machine" — конечный
автомат): пока идёт диалог, для пары (чат, пользователь) хранится текущее
"состояние" (на каком вопросе мы остановились) и уже собранные данные
(что пользователь успел ввести). Состояния объявляются один раз списком
в классе AddEventStates ниже, а дальше каждый обработчик:
1. Фильтром StateFilter(...) подписывается только на "своё" состояние —
   то есть срабатывает, только если пользователь именно сейчас отвечает
   именно на этот вопрос, а не просто написал что-то в чат.
2. Проверяет введённый текст.
3. Либо просит повторить (если формат неверный), либо сохраняет ответ
   в FSMContext и переключает пользователя на следующее состояние.

Хранилище состояний по умолчанию — в оперативной памяти процесса бота
(MemoryStorage, aiogram подключает её сама, если явно не указано другое).
Это значит: если бот перезапустится посреди диалога — начатый, но не
подтверждённый диалог "потеряется", и пользователю нужно будет набрать
/add_event заново. Ничего не сохранённого в БД при этом не пострадает —
именно поэтому в диалоге ничего не пишется в БД до экрана подтверждения.
"""

from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.database import (
    delete_event,
    get_event_by_id,
    get_past_events,
    get_upcoming_events,
    insert_event,
    upsert_chat,
    upsert_user,
)
from bot.messages import (
    MSG_ALL_EVENTS,
    MSG_ASK_DELETE_EVENT_CHOICE,
    MSG_ASK_DELETE_EVENT_SCOPE,
    MSG_ASK_EVENT_DATE,
    MSG_ASK_EVENT_DESCRIPTION,
    MSG_ASK_EVENT_DESCRIPTION_CHOICE,
    MSG_ASK_EVENT_TIME,
    MSG_ASK_EVENT_TIME_CHOICE,
    MSG_ASK_EVENT_TITLE,
    MSG_CANCELLED,
    MSG_DELETE_EVENT_CONFIRM,
    MSG_EVENT_ADDED,
    MSG_EVENT_ALREADY_GONE,
    MSG_EVENT_DESCRIPTION_NOT_SET,
    MSG_EVENT_SAVED,
    MSG_EVENT_TIME_NOT_SET,
    MSG_INVALID_DATE,
    MSG_INVALID_TIME,
    MSG_NEXT_EVENT,
    format_days_until,
    random_no_events,
)

router = Router()


class AddEventStates(StatesGroup):
    """Все состояния, через которые проходит диалог /add_event по порядку."""

    waiting_title = State()
    waiting_date = State()
    # Время — необязательное поле: сначала спрашиваем через кнопки,
    # вводить его вообще или пропустить (waiting_time_choice), и только
    # если выбрали "Добавить время" — ждём текст (waiting_time_text).
    waiting_time_choice = State()
    waiting_time_text = State()
    # Описание — тот же паттерн, что и время выше: кнопки выбора, и только
    # при "Добавить описание" — ждём текст.
    waiting_description_choice = State()
    waiting_description_text = State()
    confirm = State()


# Формат дат в диалогах с пользователем — как в брифе (раздел 3.1/4).
# В БД дата хранится иначе (см. пояснение в database.py) — конвертация
# туда-обратно происходит прямо здесь, на границе "пользователь ⇆ БД".
_USER_DATE_FORMAT = "%d.%m.%Y"
_DB_DATE_FORMAT = "%Y-%m-%d"
_USER_TIME_FORMAT = "%H:%M"

_CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="add_event:confirm"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="add_event:cancel"),
        ]
    ]
)

_TIME_CHOICE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Добавить время", callback_data="add_event:time:enter"),
            InlineKeyboardButton(text="Пропустить", callback_data="add_event:time:skip"),
        ]
    ]
)

_DESCRIPTION_CHOICE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Добавить описание", callback_data="add_event:description:enter"),
            InlineKeyboardButton(text="Пропустить", callback_data="add_event:description:skip"),
        ]
    ]
)


def _format_confirmation(data: dict) -> str:
    """
    Собирает текст экрана подтверждения из данных, накопленных в диалоге.

    Вынесено в отдельную функцию, потому что на экран подтверждения можно
    попасть четырьмя путями (время/описание — ввели текстом ИЛИ нажали
    «Пропустить», в любой комбинации) — незачем дублировать форматирование
    в каждом из обработчиков.
    """
    time_display = data.get("start_time") or MSG_EVENT_TIME_NOT_SET
    description_display = data.get("description") or MSG_EVENT_DESCRIPTION_NOT_SET
    return MSG_EVENT_ADDED.format(
        title=data["title"],
        date=data["event_date_display"],
        time=time_display,
        description=description_display,
    )


@router.message(Command("add_event"))
async def cmd_add_event(message: Message, state: FSMContext) -> None:
    """Шаг 0: запускает диалог, задаёт первый вопрос — название события."""
    # На случай, если пользователь написал /add_event, ни разу не нажав
    # /start в этом чате: регистрируем чат и пользователя здесь же, иначе
    # сохранение события в конце диалога упадёт — EVENTS.chat_id/created_by
    # ссылаются на CHATS/USERS (внешние ключи, см. database.py).
    upsert_chat(message.chat.id, message.chat.title)
    upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    await state.set_state(AddEventStates.waiting_title)
    await message.answer(MSG_ASK_EVENT_TITLE)


@router.message(Command("cancel"), ~StateFilter(None))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """
    /cancel работает на любом шаге любого диалога (не только /add_event).

    Фильтр ~StateFilter(None) читается как "сработать, если у пользователя
    сейчас установлено хоть какое-то состояние" (State(None) — это как раз
    "диалога нет"). Поэтому этот обработчик не привязан к AddEventStates
    напрямую — когда появятся другие диалоги (edit_event, add_birthday...),
    /cancel продолжит работать для них без изменений.
    """
    await state.clear()
    await message.answer(MSG_CANCELLED)


@router.message(AddEventStates.waiting_title, F.text)
async def process_title(message: Message, state: FSMContext) -> None:
    """Шаг 1: получили название, спрашиваем дату."""
    await state.update_data(title=message.text.strip())
    await state.set_state(AddEventStates.waiting_date)
    await message.answer(MSG_ASK_EVENT_DATE)


@router.message(AddEventStates.waiting_date, F.text)
async def process_date(message: Message, state: FSMContext) -> None:
    """Шаг 2: валидируем дату; при ошибке — переспрашиваем этот же шаг."""
    try:
        parsed = datetime.strptime(message.text.strip(), _USER_DATE_FORMAT)
    except ValueError:
        # Состояние НЕ меняем — пользователь остаётся на этом же вопросе,
        # пока не введёт дату в правильном формате (или не наберёт /cancel).
        await message.answer(MSG_INVALID_DATE)
        return

    # Храним оба варианта даты в данных состояния: строку для показа
    # пользователю (как он её ввёл) и строку в формате БД (для сохранения).
    await state.update_data(
        event_date_display=parsed.strftime(_USER_DATE_FORMAT),
        event_date_db=parsed.strftime(_DB_DATE_FORMAT),
    )
    await state.set_state(AddEventStates.waiting_time_choice)
    await message.answer(MSG_ASK_EVENT_TIME_CHOICE, reply_markup=_TIME_CHOICE_KEYBOARD)


@router.callback_query(AddEventStates.waiting_time_choice, F.data == "add_event:time:enter")
async def process_time_choice_enter(callback: CallbackQuery, state: FSMContext) -> None:
    """Нажали «Добавить время» — просим ввести его текстом на следующем шаге."""
    await state.set_state(AddEventStates.waiting_time_text)
    await callback.message.edit_text(MSG_ASK_EVENT_TIME)
    await callback.answer()


@router.callback_query(AddEventStates.waiting_time_choice, F.data == "add_event:time:skip")
async def process_time_choice_skip(callback: CallbackQuery, state: FSMContext) -> None:
    """Нажали «Пропустить» — время не указано, дальше спрашиваем описание."""
    await state.update_data(start_time=None)
    await state.set_state(AddEventStates.waiting_description_choice)
    await callback.message.edit_text(
        MSG_ASK_EVENT_DESCRIPTION_CHOICE, reply_markup=_DESCRIPTION_CHOICE_KEYBOARD
    )
    await callback.answer()


@router.message(AddEventStates.waiting_time_text, F.text)
async def process_time_text(message: Message, state: FSMContext) -> None:
    """Шаг 3 (если выбрали ввод): валидируем время, дальше — вопрос про описание."""
    try:
        parsed = datetime.strptime(message.text.strip(), _USER_TIME_FORMAT)
    except ValueError:
        # Состояние не меняем — ждём повторного ввода этого же шага.
        await message.answer(MSG_INVALID_TIME)
        return

    await state.update_data(start_time=parsed.strftime(_USER_TIME_FORMAT))
    await state.set_state(AddEventStates.waiting_description_choice)
    await message.answer(MSG_ASK_EVENT_DESCRIPTION_CHOICE, reply_markup=_DESCRIPTION_CHOICE_KEYBOARD)


@router.callback_query(AddEventStates.waiting_description_choice, F.data == "add_event:description:enter")
async def process_description_choice_enter(callback: CallbackQuery, state: FSMContext) -> None:
    """Нажали «Добавить описание» — просим ввести его текстом на следующем шаге."""
    await state.set_state(AddEventStates.waiting_description_text)
    await callback.message.edit_text(MSG_ASK_EVENT_DESCRIPTION)
    await callback.answer()


@router.callback_query(AddEventStates.waiting_description_choice, F.data == "add_event:description:skip")
async def process_description_choice_skip(callback: CallbackQuery, state: FSMContext) -> None:
    """Нажали «Пропустить» — описание не указано, сразу к экрану подтверждения."""
    await state.update_data(description=None)
    data = await state.get_data()
    await state.set_state(AddEventStates.confirm)
    await callback.message.edit_text(_format_confirmation(data), reply_markup=_CONFIRM_KEYBOARD)
    await callback.answer()


@router.message(AddEventStates.waiting_description_text, F.text)
async def process_description_text(message: Message, state: FSMContext) -> None:
    """Шаг 4 (если выбрали ввод описания): текст без валидации формата, затем подтверждение."""
    await state.update_data(description=message.text.strip())

    data = await state.get_data()
    await state.set_state(AddEventStates.confirm)
    await message.answer(_format_confirmation(data), reply_markup=_CONFIRM_KEYBOARD)


@router.callback_query(AddEventStates.confirm, F.data == "add_event:confirm")
async def process_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 5а: пользователь нажал «Подтвердить» — сохраняем в БД."""
    data = await state.get_data()

    insert_event(
        chat_id=callback.message.chat.id,
        created_by=callback.from_user.id,
        title=data["title"],
        event_date_iso=data["event_date_db"],
        start_time=data["start_time"],
        description=data["description"],
    )

    await state.clear()
    # Убираем кнопки у экрана подтверждения, чтобы нельзя было нажать их
    # повторно, и отдельным сообщением подтверждаем сохранение.
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(MSG_EVENT_SAVED.format(title=data["title"]))
    await callback.answer()

    # TODO(следующий шаг): автообновление закрепа (раздел 3.10 брифа) —
    # пока не реализовано, событие сохраняется только в БД.


@router.callback_query(AddEventStates.confirm, F.data == "add_event:cancel")
async def process_confirm_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 5б: пользователь нажал «Отменить» на экране подтверждения."""
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(MSG_CANCELLED)
    await callback.answer()


# «Ближе чем через 2 недели» из раздела 4 брифа (описание MSG_PIN) —
# порог, при котором первое событие в списке получает пометку «‼️через N».
_NEARBY_MARKER_THRESHOLD_DAYS = 14


def _event_date(event) -> date:
    """Парсит event_date из БД ('ГГГГ-ММ-ДД') в объект date."""
    return datetime.strptime(event["event_date"], _DB_DATE_FORMAT).date()


def _format_event_title_line(event, event_date: date) -> str:
    """
    Строит строку "дата (+ время, если указано) — название" — общий
    формат одной записи, который используют и /all_events, и /next_event.
    event_date передаётся отдельным параметром, чтобы не парсить дату
    из строки повторно там, где она уже понадобилась вызывающему коду
    (например, для расчёта "через сколько дней").
    """
    line = event_date.strftime(_USER_DATE_FORMAT)
    if event["start_time"]:
        line += f" {event['start_time']}"
    line += f" — {event['title']}"
    return line


def _format_events_list(events: list) -> str:
    """
    Собирает текст списка событий для {events_list} в MSG_ALL_EVENTS.
    Тот же формат впоследствии пригодится для закрепа (MSG_PIN, раздел
    3.10 брифа) — там выборка событий будет другая (только ближайшие
    30 дней + дни рождения), но формат каждой записи тот же.

    Каждая запись — это дата (+ время, если указано) — название, и, если
    есть, вторая строка с описанием (описание необязательное — см.
    /add_event, раздел 3.1). Самое первое (то есть самое ближайшее)
    событие в списке дополнительно получает пометку «—‼️через N», если оно
    ближе чем через 2 недели.

    Записи разделяются пустой строкой — иначе двухстрочные записи (с
    описанием) сливались бы визуально с соседними.
    """
    today = date.today()
    entries = []
    for index, event in enumerate(events):
        event_date = _event_date(event)
        line = _format_event_title_line(event, event_date)

        if index == 0:
            days_until = (event_date - today).days
            if days_until < _NEARBY_MARKER_THRESHOLD_DAYS:
                line += f" —‼️{format_days_until(days_until)}"

        if event["description"]:
            line += f"\n{event['description']}"

        entries.append(line)
    return "\n\n".join(entries)


@router.message(Command("all_events"))
async def cmd_all_events(message: Message) -> None:
    """
    /all_events — раздел 3.5 брифа. Чистое чтение, без диалога и без
    состояния FSM: дополняет закреп (тот показывает только ближайшие
    30 дней) полным списком всех предстоящих событий чата, от ближайшего
    к дальнему.
    """
    events = get_upcoming_events(message.chat.id)

    if not events:
        # Та же случайная "нет событий" фраза, что и в /next_event,
        # /edit_event, /delete_event — раздел 4 брифа, MSG_NO_EVENTS.
        await message.answer(random_no_events())
        return

    await message.answer(MSG_ALL_EVENTS.format(events_list=_format_events_list(events)))


@router.message(Command("next_event"))
async def cmd_next_event(message: Message) -> None:
    """
    /next_event — раздел 3.4 брифа. Чистое чтение: показывает только
    самое ближайшее предстоящее событие чата (get_upcoming_events уже
    возвращает события отсортированными от ближайшего к дальнему —
    достаточно взять первую строку).

    Важный нюанс из брифа: событие сегодняшнего дня остаётся "ближайшим"
    весь день, даже если время его начала уже прошло — get_upcoming_events
    фильтрует по дате (event_date >= сегодня), а не по времени.
    """
    events = get_upcoming_events(message.chat.id)

    if not events:
        await message.answer(random_no_events())
        return

    event = events[0]
    event_date = _event_date(event)

    body = _format_event_title_line(event, event_date)
    if event["description"]:
        body += f"\n{event['description']}"
    # В отличие от /all_events, где пометка "через N" появляется только
    # у событий ближе чем через 2 недели, здесь "через сколько дней"
    # показывается всегда — это весь смысл команды (раздел 3.4 брифа).
    body += f"\n{format_days_until((event_date - date.today()).days)}"

    await message.answer(MSG_NEXT_EVENT.format(body=body))


# --- /delete_event (раздел 3.3 брифа) ---------------------------------
#
# В отличие от /add_event, здесь не нужен FSM (StatesGroup): пользователь
# ничего не вводит текстом, только жмёт кнопки. Вся "память" на пути
# список → подтверждение — это id события (и выбранный список — прошедшие
# или предстоящие), зашитые прямо в callback_data кнопок
# ("delete_event:scope:upcoming", "delete_event:select:<id>",
# "delete_event:confirm:<id>"), этого достаточно, чтобы каждый следующий
# шаг знал контекст.


def _build_delete_scope_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопки выбора списка: предстоящие/прошедшие. Показывается только
    когда оба списка непустые (см. cmd_delete_event) — иначе шаг просто
    лишний клик без выбора.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗓 Прошедшие", callback_data="delete_event:scope:past"),
                InlineKeyboardButton(text="📅 Предстоящие", callback_data="delete_event:scope:upcoming"),
            ]
        ]
    )


def _build_delete_choice_keyboard(events: list) -> InlineKeyboardMarkup:
    """Одна кнопка на событие: "дата — название"; id события — в callback_data."""
    rows = [
        [
            InlineKeyboardButton(
                text=_format_event_title_line(event, _event_date(event)),
                callback_data=f"delete_event:select:{event['id']}",
            )
        ]
        for event in events
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_delete_confirm_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_event:confirm:{event_id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data="delete_event:cancel"),
            ]
        ]
    )


def _format_delete_confirmation(event) -> str:
    """Название+дата+время — обязательные детали перед необратимым удалением."""
    time_display = event["start_time"] or MSG_EVENT_TIME_NOT_SET
    return MSG_DELETE_EVENT_CONFIRM.format(
        title=event["title"],
        date=_event_date(event).strftime(_USER_DATE_FORMAT),
        time=time_display,
    )


@router.message(Command("delete_event"))
async def cmd_delete_event(message: Message) -> None:
    """
    Шаг 0: решаем, что показать дальше.

    Списки предстоящих и прошедших событий запрашиваются отдельно (не
    один общий список из всех событий чата) — чат, который живёт больше
    года, иначе выдал бы нечитаемую портянку из давно неактуальной
    лабуды. Логика:
    - оба списка пустые → случайная фраза MSG_NO_EVENTS (брифом сказано
      использовать именно её, отдельного MSG_NO_EVENTS_TO_DELETE не
      заводили — см. messages.py);
    - только один из списков непустой → сразу показываем его, шаг выбора
      списка был бы лишним кликом без реального выбора;
    - оба непустые → спрашиваем, какой список открыть (шаг 1).
    """
    upcoming = get_upcoming_events(message.chat.id)
    past = get_past_events(message.chat.id)

    if not upcoming and not past:
        await message.answer(random_no_events())
        return

    if upcoming and not past:
        await message.answer(
            MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_delete_choice_keyboard(upcoming)
        )
        return

    if past and not upcoming:
        await message.answer(
            MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_delete_choice_keyboard(past)
        )
        return

    await message.answer(MSG_ASK_DELETE_EVENT_SCOPE, reply_markup=_build_delete_scope_keyboard())


@router.callback_query(F.data == "delete_event:scope:upcoming")
async def process_delete_scope_upcoming(callback: CallbackQuery) -> None:
    """Шаг 1а: выбрали «Предстоящие» — показываем список этого списка."""
    events = get_upcoming_events(callback.message.chat.id)
    await callback.message.edit_text(
        MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_delete_choice_keyboard(events)
    )
    await callback.answer()


@router.callback_query(F.data == "delete_event:scope:past")
async def process_delete_scope_past(callback: CallbackQuery) -> None:
    """Шаг 1б: выбрали «Прошедшие» — показываем список этого списка."""
    events = get_past_events(callback.message.chat.id)
    await callback.message.edit_text(
        MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_delete_choice_keyboard(events)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_event:select:"))
async def process_delete_select(callback: CallbackQuery) -> None:
    """Шаг 2: выбрали событие — показываем детали и просим подтвердить удаление."""
    event_id = int(callback.data.removeprefix("delete_event:select:"))
    event = get_event_by_id(callback.message.chat.id, event_id)

    if event is None:
        # Кто-то успел удалить это событие, пока список с кнопками висел в чате.
        await callback.message.edit_text(MSG_EVENT_ALREADY_GONE)
        await callback.answer()
        return

    await callback.message.edit_text(
        _format_delete_confirmation(event),
        reply_markup=_build_delete_confirm_keyboard(event_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_event:confirm:"))
async def process_delete_confirm(callback: CallbackQuery) -> None:
    """
    Шаг 3а: подтвердили — удаляем из БД. Отдельного сообщения об успехе
    НЕТ (раздел 4 брифа, MSG_EVENT_DELETED) — по брифу обновлённого
    закрепа достаточно; вместо него, раз закрепа ещё нет, просто помечаем
    то же сообщение как выполненное.
    """
    event_id = int(callback.data.removeprefix("delete_event:confirm:"))
    event = get_event_by_id(callback.message.chat.id, event_id)

    if event is None:
        await callback.message.edit_text(MSG_EVENT_ALREADY_GONE)
        await callback.answer()
        return

    delete_event(callback.message.chat.id, event_id)

    await callback.message.edit_text(f"🗑 Удалено: «{event['title']}»")
    await callback.answer()

    # TODO(следующий шаг): автообновление закрепа (раздел 3.10 брифа) —
    # пока не реализовано.


@router.callback_query(F.data == "delete_event:cancel")
async def process_delete_cancel(callback: CallbackQuery) -> None:
    """Шаг 3б: отменили — событие остаётся как есть, без изменений."""
    await callback.message.edit_text(MSG_CANCELLED)
    await callback.answer()
