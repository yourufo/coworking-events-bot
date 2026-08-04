"""
Модуль работы с базой данных.

Здесь — и только здесь — описана структура таблиц (схема БД) и живут функции,
которые с этой структурой работают. Такая централизация полезна: если завтра
понадобится что-то поменять в БД, известно, что смотреть нужно в одном месте.

Почему SQLite (а не PostgreSQL/MySQL):
- вся база — это один файл на диске, отдельный сервер БД разворачивать не нужно;
- ничего дополнительно не устанавливается и не настраивается;
- для нагрузки "бот в нескольких групповых чатах коворкинга" производительности
  с большим запасом.
Если проект сильно вырастет — можно будет перейти на PostgreSQL, изменится
только способ подключения, а не сама структура таблиц ниже.

Мы используем "сырой" SQL напрямую (без ORM-библиотек вроде SQLAlchemy) —
так весь путь данных виден в одном месте, без промежуточного слоя, который
пришлось бы объяснять отдельно.
"""

import sqlite3
from datetime import date
from pathlib import Path

from bot.config import DB_PATH


def get_connection() -> sqlite3.Connection:
    """
    Открывает новое соединение с файлом БД.

    Почему открываем новое соединение на каждую операцию, а не держим одно
    "глобальное": объект sqlite3.Connection в стандартной библиотеке Python
    не гарантированно безопасен для использования из разных задач одновременно
    (а бот на aiogram обрабатывает сообщения асинхронно, то есть потенциально
    "параллельно"). Открыть/закрыть соединение — дешёвая операция, а лишний
    класс проблем с гонками данных так проще избежать полностью.
    """
    # Создаём папку под файл БД, если её ещё нет (например, "data/").
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    # По умолчанию SQLite не проверяет внешние ключи (исторически, для
    # совместимости) — эту проверку нужно включать явно на каждом соединении.
    # Без неё можно случайно записать в EVENTS.chat_id значение, которого
    # нет в CHATS, и это никак не подсветится.
    conn.execute("PRAGMA foreign_keys = ON")

    # row_factory позволяет обращаться к колонкам результата по имени
    # (row["title"]), а не только по номеру (row[0]) — надёжнее читать код
    # и меньше риска перепутать порядок колонок.
    conn.row_factory = sqlite3.Row
    return conn


# Схема БД — соответствует разделу 2 PROJECT_BRIEF.md.
#
# Отдельное архитектурное решение по датам: в брифе указано, что пользователь
# вводит и видит даты в формате ДД-ММ-ГГГГ. Но ХРАНИМ мы их в БД в формате
# ГГГГ-ММ-ДД (стандарт ISO 8601). Почему не так, как вводит пользователь:
# строки вида "2026-06-28" сортируются как текст точно так же, как и как даты
# (текстовое и хронологическое упорядочивание совпадают) — это нужно для
# запроса "ближайшее событие". Строки вида "28-06-2026" так не сортируются:
# "28-06-2026" встанет раньше "05-07-2026" при обычной текстовой сортировке,
# хотя по факту дата позже. Поэтому конвертация формата (ДД-ММ-ГГГГ ⇆
# ГГГГ-ММ-ДД) — задача уровня "обработчик команд", а не БД: пользователь
# видит привычный формат, а БД хранит удобный для сортировки.
#
# CREATE TABLE IF NOT EXISTS — безопасно выполнять при каждом запуске бота:
# если таблицы уже созданы, ничего не произойдёт и данные не пострадают.
SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id                     INTEGER PRIMARY KEY,   -- ID чата из Telegram
    chat_title                  TEXT,
    pinned_message_id           INTEGER,
    pinned_message_created_at   TEXT                    -- ISO datetime строкой
);

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,   -- ID пользователя из Telegram
    username    TEXT,                  -- может отсутствовать (NULL)
    first_name  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL REFERENCES chats(chat_id),
    created_by  INTEGER NOT NULL REFERENCES users(user_id),
    title       TEXT NOT NULL,
    event_date  TEXT NOT NULL,   -- 'ГГГГ-ММ-ДД', см. пояснение выше
    start_time  TEXT,            -- 'ЧЧ:ММ', NULL — если время не указали
    description TEXT             -- свободный текст, NULL — если пропустили
);

CREATE TABLE IF NOT EXISTS birthdays (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL REFERENCES chats(chat_id),
    added_by        INTEGER NOT NULL REFERENCES users(user_id),
    person_user_id  INTEGER REFERENCES users(user_id),  -- NULL для "внешнего" именинника
    person_name     TEXT NOT NULL,
    birth_day       INTEGER NOT NULL,   -- 1..31
    birth_month     INTEGER NOT NULL,   -- 1..12
    birth_year      INTEGER             -- NULL, если год неизвестен
);

CREATE TABLE IF NOT EXISTS reminder_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type    TEXT NOT NULL,   -- 'event' или 'birthday'
    source_id      INTEGER NOT NULL,
    interval_type  TEXT NOT NULL,   -- '2_weeks' / '1_week' / '3_days' / '1_day'
    sent_at        TEXT NOT NULL    -- ISO datetime строкой
);
"""



# Колонки, добавленные в схему ПОСЛЕ того, как таблицы могли быть уже
# созданы на чьей-то машине более ранней версией бота.
#
# Почему это вообще нужно: "CREATE TABLE IF NOT EXISTS" в SCHEMA выше
# создаёт таблицу, только если её ещё нет вообще. Если таблица `events`
# уже существует (бот уже запускался раньше), а мы потом добавили в SCHEMA
# новую колонку — IF NOT EXISTS её не увидит и не добавит: физически в
# файле БД колонки не будет, хотя в коде её уже ждут. Итог — ошибка вида
# "table events has no column named description" при попытке что-то
# сохранить, и никакие данные, введённые до этой ошибки, не сохраняются.
#
# Решение — простая ручная миграция: при каждом старте бота проверяем по
# списку "таблица.колонка должна существовать" и через ALTER TABLE
# добавляем недостающие. Уже существующие колонки и данные в них не
# трогаем — только дописываем то, чего не хватает.
_COLUMN_MIGRATIONS = [
    # (таблица, колонка, SQL-тип колонки для ALTER TABLE)
    ("events", "description", "TEXT"),
]


def _run_column_migrations(conn: sqlite3.Connection) -> None:
    """Добавляет недостающие колонки из _COLUMN_MIGRATIONS, если их ещё нет."""
    for table, column, column_type in _COLUMN_MIGRATIONS:
        existing_columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def init_db() -> None:
    """
    Создаёт все таблицы, если их ещё нет, и до-накатывает недостающие
    колонки на уже существующие таблицы (см. _COLUMN_MIGRATIONS выше).

    Вызывается один раз при каждом старте бота (см. bot/main.py) — это
    называется "миграция при запуске" в самом простом варианте: на старте
    гарантируем, что структура БД на месте, прежде чем начать с ней работать.
    """
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _run_column_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def upsert_chat(chat_id: int, chat_title: str | None) -> None:
    """
    Добавляет чат в таблицу CHATS, если его там ещё нет; если уже есть —
    обновляет только название (на случай, если чат переименовали).

    "Upsert" = update + insert одной командой. В SQLite для этого есть
    конструкция "INSERT ... ON CONFLICT ... DO UPDATE": пробуем вставить
    новую строку, а если по PRIMARY KEY уже есть конфликт — вместо ошибки
    выполняем обновление существующей строки.
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO chats (chat_id, chat_title)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET chat_title = excluded.chat_title
            """,
            (chat_id, chat_title),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_user(user_id: int, username: str | None, first_name: str) -> None:
    """Тот же принцип upsert, что и в upsert_chat, но для таблицы USERS."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name),
        )
        conn.commit()
    finally:
        conn.close()


def insert_event(
    chat_id: int,
    created_by: int,
    title: str,
    event_date_iso: str,
    start_time: str | None,
    description: str | None,
) -> int:
    """
    Сохраняет новое событие в таблицу EVENTS, возвращает его id.

    event_date_iso передаётся уже в формате 'ГГГГ-ММ-ДД' (см. пояснение
    к SCHEMA выше про хранение дат) — конвертацию из пользовательского
    формата ДД.ММ.ГГГГ делает обработчик команды (bot/handlers/events.py),
    здесь только сохранение "как есть".

    start_time и description могут быть None — оба поля необязательны
    (пользователь мог нажать «Пропустить» на соответствующем шаге диалога
    /add_event).

    chat_id и created_by должны уже существовать в CHATS/USERS
    (внешние ключи) — на практике это гарантируется тем, что upsert_chat/
    upsert_user вызываются раньше в handle_start и будут вызываться
    в каждом обработчике, работающем в чате.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO events (chat_id, created_by, title, event_date, start_time, description)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, created_by, title, event_date_iso, start_time, description),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_upcoming_events(chat_id: int) -> list[sqlite3.Row]:
    """
    Возвращает ВСЕ предстоящие события чата (без ограничения на 30 дней
    вперёд, в отличие от закрепа — см. раздел 3.10 брифа), отсортированные
    от ближайшего к дальнему. Используется в /all_events (раздел 3.5) и
    пригодится для /next_event (раздел 3.4) — там нужно то же самое, только
    взять первую строку результата.

    "Предстоящее" проверяется по event_date >= сегодня, без учёта времени:
    событие сегодняшнего дня остаётся в списке весь день, даже если время
    начала уже прошло — в БД нет времени окончания события, нет надёжного
    способа понять, что оно уже завершилось (важный нюанс из раздела 3.4
    брифа).
    """
    conn = get_connection()
    try:
        # event_date хранится в формате 'ГГГГ-ММ-ДД' (см. пояснение к SCHEMA
        # выше) — date.today().isoformat() даёт строку в том же формате,
        # поэтому сравнение "event_date >= today_iso" — обычное сравнение
        # строк, а не дат, но благодаря одинаковому формату оно работает
        # корректно.
        today_iso = date.today().isoformat()
        cursor = conn.execute(
            """
            SELECT id, title, event_date, start_time, description
            FROM events
            WHERE chat_id = ? AND event_date >= ?
            ORDER BY event_date ASC, start_time ASC
            """,
            (chat_id, today_iso),
        )
        return cursor.fetchall()
    finally:
        conn.close()


def get_all_events(chat_id: int) -> list[sqlite3.Row]:
    """
    Возвращает ВСЕ события чата — в отличие от get_upcoming_events, БЕЗ
    фильтра "только предстоящие". Используется для списка кнопками в
    /edit_event и /delete_event (разделы 3.2/3.3 брифа): в отличие от
    /next_event и /all_events, где смысл команды — "что нас ждёт впереди",
    здесь пользователь может захотеть найти и удалить/поправить и уже
    прошедшее событие (например, добавленное по ошибке с неверной датой).
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT id, title, event_date, start_time, description
            FROM events
            WHERE chat_id = ?
            ORDER BY event_date ASC, start_time ASC
            """,
            (chat_id,),
        )
        return cursor.fetchall()
    finally:
        conn.close()


def get_event_by_id(chat_id: int, event_id: int) -> sqlite3.Row | None:
    """
    Возвращает одно событие по id — с обязательной проверкой, что оно
    принадлежит именно этому чату (chat_id в условии WHERE, не только id).

    Зачем это, если id и так уникален на всю БД: id события летает в
    callback_data инлайн-кнопок между шагами диалога /delete_event. Без
    проверки chat_id пользователь одного чата, теоретически подставив
    чужой id (или просто "угадав" маленькое число), мог бы посмотреть
    или удалить событие из другого чата, где бот тоже работает.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT id, title, event_date, start_time, description
            FROM events
            WHERE id = ? AND chat_id = ?
            """,
            (event_id, chat_id),
        )
        return cursor.fetchone()
    finally:
        conn.close()


def delete_event(chat_id: int, event_id: int) -> bool:
    """
    Удаляет событие по id (с той же проверкой chat_id, что и в
    get_event_by_id — по той же причине). Возвращает True, если строка
    была реально удалена, и False — если события с таким id в этом чате
    уже не было (например, кто-то успел удалить его раньше, пока экран
    подтверждения ещё висел в чате открытым).
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM events WHERE id = ? AND chat_id = ?",
            (event_id, chat_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
