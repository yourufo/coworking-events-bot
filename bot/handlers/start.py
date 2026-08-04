"""
Обработчик команды /start.

В aiogram обработчики группируются в "роутеры" (Router) — удобно думать
про это как про папку с проводкой: входящее сообщение от Telegram попадает
диспетчеру (см. bot/main.py), а тот решает, какому роутеру и какой функции
внутри него его передать, сверяясь с условиями-фильтрами (у нас ниже —
фильтр CommandStart(), то есть "сработать только на /start").

Сейчас в проекте один роутер с одним обработчиком. На следующих этапах,
когда появятся /add_event, /add_birthday и другие команды из брифа,
каждая группа команд получит свой файл-роутер (например, handlers/events.py,
handlers/birthdays.py) — так проще ориентироваться в разросшемся коде.
"""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.database import upsert_chat, upsert_user

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    """
    Срабатывает на команду /start.

    Заодно "регистрирует" чат и пользователя в БД через upsert-функции.
    Зачем это нужно уже на этом шаге: в таблицах EVENTS и BIRTHDAYS есть
    внешние ключи на CHATS и USERS (см. database.py) — прежде чем в чате
    можно будет создать событие, сам чат должен существовать в таблице
    CHATS, иначе сохранение события упадёт с ошибкой целостности данных.

    Важная оговорка на будущее: полагаться только на /start для регистрации
    участников нельзя — не все участники чата обязательно её наберут.
    На следующих этапах эту же регистрацию добавим и в другие обработчики
    (например, при получении любого сообщения в чате).
    """
    upsert_chat(message.chat.id, message.chat.title)
    upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    await message.answer(
        "Привет! 👋 Я бот коворкинга — слежу за событиями и днями рождения.\n\n"
        "Пока я умею только здороваться — остальные команды появятся "
        "на следующих этапах разработки."
    )
