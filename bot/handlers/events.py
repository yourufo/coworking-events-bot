"""
Обработчики команд про события: /add_event, /all_events, /next_event,
/delete_event, /edit_event (разделы 3.1–3.5 брифа).

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
    update_event,
    upsert_chat,
    upsert_user,
)
from bot.messages import (
    MSG_ALL_EVENTS,
    MSG_ASK_DELETE_EVENT_CHOICE,
    MSG_ASK_DELETE_EVENT_SCOPE,
    MSG_ASK_EDIT_DATE,
    MSG_ASK_EDIT_DESCRIPTION,
    MSG_ASK_EDIT_EVENT_CHOICE,
    MSG_ASK_EDIT_EVENT_SCOPE,
    MSG_ASK_EDIT_TIME,
    MSG_ASK_EDIT_TITLE,
    MSG_ASK_EVENT_DATE,
    MSG_ASK_EVENT_DESCRIPTION,
    MSG_ASK_EVENT_DESCRIPTION_CHOICE,
    MSG_ASK_EVENT_TIME,
    MSG_ASK_EVENT_TIME_CHOICE,
    MSG_ASK_EVENT_TITLE,
    MSG_CANCELLED,
    MSG_DELETE_EVENT_CONFIRM,
    MSG_EDIT_ENTER_DATE,
    MSG_EDIT_ENTER_DESCRIPTION,
    MSG_EDIT_ENTER_TIME,
    MSG_EDIT_ENTER_TITLE,
    MSG_EDIT_NOTHING_CHANGED,
    MSG_EVENT_ADDED,
    MSG_EVENT_ALREADY_GONE,
    MSG_EVENT_DESCRIPTION_NOT_SET,
    MSG_EVENT_EDIT_CONFIRM,
    MSG_EVENT_SAVED,
    MSG_EVENT_TIME_NOT_SET,
    MSG_EVENT_UPDATED,
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


class EditEventStates(StatesGroup):
    """
    Состояния диалога /edit_event (раздел 3.2 брифа). В отличие от
    AddEventStates, каждое поле проходит через пару "спросить да/нет,
    надо ли менять" (asking_*) → при "да" отдельное состояние ждёт новый
    текст (waiting_*_text) → в любом случае (да с текстом или сразу нет)
    переходим к вопросу про следующее поле. Список кнопками и выбор
    конкретного события (до этого класса состояний) идут БЕЗ FSM — это
    чистая навигация по кнопкам, id события едет в callback_data (тот же
    подход, что и в /delete_event) — состояние нужно только с момента,
    когда начинаются да/нет-вопросы с возможным текстовым вводом.
    """

    asking_title = State()
    waiting_title_text = State()
    asking_date = State()
    waiting_date_text = State()
    asking_time = State()
    waiting_time_text = State()
    asking_description = State()
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

# Общая клавиатура для всех 4 "изменить ...?" вопросов в /edit_event —
# callback_data одна и та же на всех 4 шагах, потому что состояние
# (EditEventStates.asking_*) и так однозначно определяет, к какому полю
# относится ответ "да"/"нет".
_EDIT_YES_NO_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Да", callback_data="edit_event:yes"),
            InlineKeyboardButton(text="Нет", callback_data="edit_event:no"),
        ]
    ]
)

_EDIT_CONFIRM_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="edit_event:confirm"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="edit_event:cancel"),
        ]
    ]
)

# Экран "нет событий для правки" (раздел 4 брифа, MSG_NO_EVENTS_TO_EDIT).
_NO_EVENTS_EDIT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Создать событие", callback_data="edit_event:create"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="edit_event:cancel_no_events"),
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


def _register_chat_and_user(chat, user) -> None:
    """
    Регистрирует чат и пользователя в БД, если их там ещё нет — нужно
    ПЕРЕД тем, как начинать /add_event, иначе сохранение события в конце
    диалога упадёт: EVENTS.chat_id/created_by ссылаются на CHATS/USERS
    (внешние ключи, см. database.py). Вынесено в функцию, потому что
    запускать /add_event можно и напрямую командой, и кнопкой «➕ Создать
    событие» с экрана "нет событий" в /edit_event.
    """
    upsert_chat(chat.id, chat.title)
    upsert_user(user_id=user.id, username=user.username, first_name=user.first_name)


@router.message(Command("add_event"))
async def cmd_add_event(message: Message, state: FSMContext) -> None:
    """Шаг 0: запускает диалог, задаёт первый вопрос — название события."""
    _register_chat_and_user(message.chat, message.from_user)
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


# --- Общее для /delete_event и /edit_event: выбор события из списка ---
#
# Обе команды начинаются одинаково: показать предстоящие/прошедшие на
# выбор (если оба списка непустые), затем список кнопками. Разница —
# только в том, что происходит ПОСЛЕ выбора события, поэтому сам выбор
# вынесен в общие функции, параметризованные префиксом callback_data
# ("delete_event" / "edit_event") — так каждый следующий обработчик
# точно знает, из какого диалога пришёл клик.


def _build_scope_keyboard(command: str) -> InlineKeyboardMarkup:
    """
    Кнопки выбора списка: прошедшие/предстоящие. Показывается только
    когда оба списка непустые (см. cmd_delete_event/cmd_edit_event) —
    иначе шаг был бы лишним кликом без реального выбора.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗓 Прошедшие", callback_data=f"{command}:scope:past"),
                InlineKeyboardButton(text="📅 Предстоящие", callback_data=f"{command}:scope:upcoming"),
            ]
        ]
    )


def _build_event_choice_keyboard(command: str, events: list) -> InlineKeyboardMarkup:
    """Одна кнопка на событие: "дата — название"; id события — в callback_data."""
    rows = [
        [
            InlineKeyboardButton(
                text=_format_event_title_line(event, _event_date(event)),
                callback_data=f"{command}:select:{event['id']}",
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
            MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("delete_event", upcoming)
        )
        return

    if past and not upcoming:
        await message.answer(
            MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("delete_event", past)
        )
        return

    await message.answer(MSG_ASK_DELETE_EVENT_SCOPE, reply_markup=_build_scope_keyboard("delete_event"))


@router.callback_query(F.data == "delete_event:scope:upcoming")
async def process_delete_scope_upcoming(callback: CallbackQuery) -> None:
    """Шаг 1а: выбрали «Предстоящие» — показываем список этого списка."""
    events = get_upcoming_events(callback.message.chat.id)
    await callback.message.edit_text(
        MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("delete_event", events)
    )
    await callback.answer()


@router.callback_query(F.data == "delete_event:scope:past")
async def process_delete_scope_past(callback: CallbackQuery) -> None:
    """Шаг 1б: выбрали «Прошедшие» — показываем список этого списка."""
    events = get_past_events(callback.message.chat.id)
    await callback.message.edit_text(
        MSG_ASK_DELETE_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("delete_event", events)
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


# --- /edit_event (раздел 3.2 брифа) -------------------------------------
#
# Шаги 1–2 (список, с делением на предстоящие/прошедшие, и выбор события)
# переиспользуют _build_scope_keyboard/_build_event_choice_keyboard из
# блока /delete_event выше — тот же паттерн, другой префикс callback_data
# ("edit_event" вместо "delete_event"). С момента выбора события
# начинается FSM (EditEventStates): 4 вопроса "изменить это поле?"
# подряд — название/дата/время/описание. Брифом (раздел 3.2) описаны
# только первые 3 поля — он писался до того, как в событие добавили
# description (раздел 3.1) — четвёртый вопрос добавлен для консистентности
# с /add_event, где описание такое же необязательное поле, как и время.
#
# Накопленные изменения хранятся в FSMContext как data["changes"]
# (словарь колонка→новое значение, ТОЛЬКО реально изменённые поля) вместе
# с data["event_id"] — event_id нужен, чтобы в конце обновить именно
# нужную строку, а неполный набор changes — и чтобы экран подтверждения
# показывал только изменённое (раздел 3.2 брифа, шаг 7), и чтобы
# update_event() в БД не трогала лишние колонки.


@router.message(Command("edit_event"))
async def cmd_edit_event(message: Message) -> None:
    """
    Шаг 1 — начало: та же логика, что в /delete_event (два раздельных
    списка, шаг выбора списка — только если оба непустые). Если событий
    нет вообще — шуточная фраза + кнопки «➕ Создать событие» / «❌ Отмена»
    (раздел 4 брифа, MSG_NO_EVENTS_TO_EDIT).
    """
    upcoming = get_upcoming_events(message.chat.id)
    past = get_past_events(message.chat.id)

    if not upcoming and not past:
        await message.answer(random_no_events(), reply_markup=_NO_EVENTS_EDIT_KEYBOARD)
        return

    if upcoming and not past:
        await message.answer(
            MSG_ASK_EDIT_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("edit_event", upcoming)
        )
        return

    if past and not upcoming:
        await message.answer(
            MSG_ASK_EDIT_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("edit_event", past)
        )
        return

    await message.answer(MSG_ASK_EDIT_EVENT_SCOPE, reply_markup=_build_scope_keyboard("edit_event"))


@router.callback_query(F.data == "edit_event:scope:upcoming")
async def process_edit_scope_upcoming(callback: CallbackQuery) -> None:
    """Выбрали «Предстоящие» — показываем список этого списка."""
    events = get_upcoming_events(callback.message.chat.id)
    await callback.message.edit_text(
        MSG_ASK_EDIT_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("edit_event", events)
    )
    await callback.answer()


@router.callback_query(F.data == "edit_event:scope:past")
async def process_edit_scope_past(callback: CallbackQuery) -> None:
    """Выбрали «Прошедшие» — показываем список этого списка."""
    events = get_past_events(callback.message.chat.id)
    await callback.message.edit_text(
        MSG_ASK_EDIT_EVENT_CHOICE, reply_markup=_build_event_choice_keyboard("edit_event", events)
    )
    await callback.answer()


@router.callback_query(F.data == "edit_event:create")
async def process_edit_create_event(callback: CallbackQuery, state: FSMContext) -> None:
    """
    «➕ Создать событие» с экрана "нет событий для правки" — по брифу
    "ведёт в /add_event": запускает тот же диалог, что и команда напрямую
    (переиспользует _register_chat_and_user из блока /add_event выше).
    """
    _register_chat_and_user(callback.message.chat, callback.from_user)
    await state.set_state(AddEventStates.waiting_title)
    await callback.message.edit_text(MSG_ASK_EVENT_TITLE)
    await callback.answer()


@router.callback_query(F.data == "edit_event:cancel_no_events")
async def process_edit_cancel_no_events(callback: CallbackQuery) -> None:
    """«❌ Отмена» с экрана "нет событий для правки" — просто убираем кнопки."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


@router.callback_query(F.data.startswith("edit_event:select:"))
async def process_edit_select(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Шаг 2: выбрали событие — запускаем FSM и задаём первый из 4
    вопросов "изменить это поле?" (название). changes пока пустой —
    заполняется по мере ответов "да" + ввода текста.
    """
    event_id = int(callback.data.removeprefix("edit_event:select:"))
    event = get_event_by_id(callback.message.chat.id, event_id)

    if event is None:
        await callback.message.edit_text(MSG_EVENT_ALREADY_GONE)
        await callback.answer()
        return

    await state.update_data(event_id=event_id, changes={})
    await state.set_state(EditEventStates.asking_title)
    await callback.message.edit_text(MSG_ASK_EDIT_TITLE, reply_markup=_EDIT_YES_NO_KEYBOARD)
    await callback.answer()


# Шаг 3: «Изменить название?»


@router.callback_query(EditEventStates.asking_title, F.data == "edit_event:yes")
async def process_edit_title_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditEventStates.waiting_title_text)
    await callback.message.edit_text(MSG_EDIT_ENTER_TITLE)
    await callback.answer()


@router.callback_query(EditEventStates.asking_title, F.data == "edit_event:no")
async def process_edit_title_no(callback: CallbackQuery, state: FSMContext) -> None:
    """«Нет» — название не трогаем, сразу переходим к вопросу про дату."""
    await state.set_state(EditEventStates.asking_date)
    await callback.message.edit_text(MSG_ASK_EDIT_DATE, reply_markup=_EDIT_YES_NO_KEYBOARD)
    await callback.answer()


@router.message(EditEventStates.waiting_title_text, F.text)
async def process_edit_title_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    changes = data["changes"]
    changes["title"] = message.text.strip()
    await state.update_data(changes=changes)

    await state.set_state(EditEventStates.asking_date)
    await message.answer(MSG_ASK_EDIT_DATE, reply_markup=_EDIT_YES_NO_KEYBOARD)


# Шаг 4: «Изменить дату?»


@router.callback_query(EditEventStates.asking_date, F.data == "edit_event:yes")
async def process_edit_date_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditEventStates.waiting_date_text)
    await callback.message.edit_text(MSG_EDIT_ENTER_DATE)
    await callback.answer()


@router.callback_query(EditEventStates.asking_date, F.data == "edit_event:no")
async def process_edit_date_no(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditEventStates.asking_time)
    await callback.message.edit_text(MSG_ASK_EDIT_TIME, reply_markup=_EDIT_YES_NO_KEYBOARD)
    await callback.answer()


@router.message(EditEventStates.waiting_date_text, F.text)
async def process_edit_date_text(message: Message, state: FSMContext) -> None:
    """Валидируем дату; при ошибке — переспрашиваем этот же шаг (как в /add_event)."""
    try:
        parsed = datetime.strptime(message.text.strip(), _USER_DATE_FORMAT)
    except ValueError:
        await message.answer(MSG_INVALID_DATE)
        return

    data = await state.get_data()
    changes = data["changes"]
    changes["event_date"] = parsed.strftime(_DB_DATE_FORMAT)
    await state.update_data(changes=changes)

    await state.set_state(EditEventStates.asking_time)
    await message.answer(MSG_ASK_EDIT_TIME, reply_markup=_EDIT_YES_NO_KEYBOARD)


# Шаг 5: «Изменить время?»


@router.callback_query(EditEventStates.asking_time, F.data == "edit_event:yes")
async def process_edit_time_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditEventStates.waiting_time_text)
    await callback.message.edit_text(MSG_EDIT_ENTER_TIME)
    await callback.answer()


@router.callback_query(EditEventStates.asking_time, F.data == "edit_event:no")
async def process_edit_time_no(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditEventStates.asking_description)
    await callback.message.edit_text(MSG_ASK_EDIT_DESCRIPTION, reply_markup=_EDIT_YES_NO_KEYBOARD)
    await callback.answer()


@router.message(EditEventStates.waiting_time_text, F.text)
async def process_edit_time_text(message: Message, state: FSMContext) -> None:
    try:
        parsed = datetime.strptime(message.text.strip(), _USER_TIME_FORMAT)
    except ValueError:
        await message.answer(MSG_INVALID_TIME)
        return

    data = await state.get_data()
    changes = data["changes"]
    changes["start_time"] = parsed.strftime(_USER_TIME_FORMAT)
    await state.update_data(changes=changes)

    await state.set_state(EditEventStates.asking_description)
    await message.answer(MSG_ASK_EDIT_DESCRIPTION, reply_markup=_EDIT_YES_NO_KEYBOARD)


# Шаг 6: «Изменить описание?» — последний вопрос, дальше подтверждение
# (это поле сверх исходных 3 из брифа — см. комментарий в начале блока).


@router.callback_query(EditEventStates.asking_description, F.data == "edit_event:yes")
async def process_edit_description_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditEventStates.waiting_description_text)
    await callback.message.edit_text(MSG_EDIT_ENTER_DESCRIPTION)
    await callback.answer()


@router.callback_query(EditEventStates.asking_description, F.data == "edit_event:no")
async def process_edit_description_no(callback: CallbackQuery, state: FSMContext) -> None:
    """Последний вопрос, ответили «Нет» — переходим к итогу."""
    text, keyboard = await _build_edit_summary(state, callback.message.chat.id)
    if keyboard is None:
        await state.clear()
    else:
        await state.set_state(EditEventStates.confirm)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.message(EditEventStates.waiting_description_text, F.text)
async def process_edit_description_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    changes = data["changes"]
    changes["description"] = message.text.strip()
    await state.update_data(changes=changes)

    text, keyboard = await _build_edit_summary(state, message.chat.id)
    if keyboard is None:
        await state.clear()
    else:
        await state.set_state(EditEventStates.confirm)
    await message.answer(text, reply_markup=keyboard)


async def _build_edit_summary(state: FSMContext, chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Шаг 7: собирает финальный экран после всех 4 вопросов — либо
    подтверждение только с изменёнными полями ("старое → новое", раздел
    3.2 брифа), либо MSG_EDIT_NOTHING_CHANGED, если changes пуст ("выход
    без подтверждения"). Клавиатура=None у второго варианта — сигнал
    вызывающему коду, что состояние надо очистить, а не переводить
    в EditEventStates.confirm.
    """
    data = await state.get_data()
    changes = data.get("changes", {})

    if not changes:
        return MSG_EDIT_NOTHING_CHANGED, None

    event = get_event_by_id(chat_id, data["event_id"])
    if event is None:
        # Событие успели удалить, пока пользователь отвечал на вопросы.
        return MSG_EVENT_ALREADY_GONE, None

    lines = []
    if "title" in changes:
        lines.append(f"Название: {event['title']} → {changes['title']}")
    if "event_date" in changes:
        old_display = _event_date(event).strftime(_USER_DATE_FORMAT)
        new_display = datetime.strptime(changes["event_date"], _DB_DATE_FORMAT).strftime(_USER_DATE_FORMAT)
        lines.append(f"Дата: {old_display} → {new_display}")
    if "start_time" in changes:
        old_display = event["start_time"] or MSG_EVENT_TIME_NOT_SET
        lines.append(f"Время: {old_display} → {changes['start_time']}")
    if "description" in changes:
        old_display = event["description"] or MSG_EVENT_DESCRIPTION_NOT_SET
        lines.append(f"Описание: {old_display} → {changes['description']}")

    return MSG_EVENT_EDIT_CONFIRM.format(changes="\n".join(lines)), _EDIT_CONFIRM_KEYBOARD


# Шаг 8: подтверждение


@router.callback_query(EditEventStates.confirm, F.data == "edit_event:confirm")
async def process_edit_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 8а: подтвердили — сохраняем только реально изменённые поля."""
    data = await state.get_data()
    event_id = data["event_id"]
    changes = data["changes"]

    updated = update_event(callback.message.chat.id, event_id, **changes)
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)

    if not updated:
        # Событие успели удалить, пока экран подтверждения был открыт.
        await callback.message.answer(MSG_EVENT_ALREADY_GONE)
        await callback.answer()
        return

    # Название для MSG_EVENT_UPDATED: новое, если его меняли, иначе —
    # текущее (событие точно ещё существует — update_event вернул True).
    title = changes.get("title")
    if title is None:
        event = get_event_by_id(callback.message.chat.id, event_id)
        title = event["title"]

    await callback.message.answer(MSG_EVENT_UPDATED.format(title=title))
    await callback.answer()

    # TODO(следующий шаг): автообновление закрепа (раздел 3.10 брифа) —
    # пока не реализовано.


@router.callback_query(EditEventStates.confirm, F.data == "edit_event:cancel")
async def process_edit_confirm_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 8б: отменили на экране подтверждения — не сохраняем ничего."""
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(MSG_CANCELLED)
    await callback.answer()
