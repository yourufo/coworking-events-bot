"""
Точка входа в приложение.

Запуск (из корня проекта): python -m bot.main

Что происходит по порядку:
1. Инициализируем БД — создаём таблицы, если их ещё нет (init_db()).
2. Создаём объект Bot — это "телефонная линия" до Telegram: через него
   бот отправляет сообщения.
3. Создаём Dispatcher — диспетчер, который получает входящие сообщения
   от Telegram и решает, какой обработчик должен на них ответить,
   опираясь на подключённые роутеры (см. bot/handlers/).
4. Запускаем polling — бот сам, в цикле, с определённой частотой
   спрашивает у Telegram "есть новые сообщения для меня?" и обрабатывает
   всё, что придёт.
   (Альтернатива polling — webhook, когда Telegram сам присылает сообщения
   на публично доступный адрес нашего сервера. Webhook эффективнее, но
   требует постоянно работающего сервера с HTTPS-адресом; polling проще
   в настройке и отлично подходит для старта проекта и локальной разработки.
   Вернуться к этому выбору можно позже, при развёртывании на боевом сервере.)
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.config import BOT_TOKEN
from bot.database import init_db
from bot.handlers import events, start

# Команды для меню бота в Telegram (кнопка "/" рядом с полем ввода).
# Без этого списка команды всё равно РАБОТАЮТ (можно набрать вручную),
# но не видны в подсказке — пользователю приходится помнить их наизусть.
#
# /cancel сюда намеренно не включена: это служебная команда, которая
# имеет смысл только посреди уже начатого диалога (/add_event и т.п.) —
# в остальное время нажатие на неё в меню ничего не делает (обработчик
# срабатывает только если у пользователя есть активное состояние
# диалога, см. cmd_cancel в bot/handlers/events.py). Как и любую другую
# команду, её всё равно можно набрать руками при необходимости.
BOT_COMMANDS = [
    BotCommand(command="start", description="Начать работу с ботом"),
    BotCommand(command="add_event", description="Добавить событие"),
    BotCommand(command="all_events", description="Все предстоящие события"),
    BotCommand(command="next_event", description="Ближайшее событие"),
    BotCommand(command="delete_event", description="Удалить событие"),
]


async def main() -> None:
    # Включаем базовое логирование, чтобы в консоли было видно, что бот
    # запустился и как обрабатывает сообщения — полезно для отладки.
    logging.basicConfig(level=logging.INFO)

    # Создаём таблицы в БД, если их ещё нет. Безопасно вызывать при каждом
    # запуске бота — уже существующие таблицы и данные в них не затрагиваются.
    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        # ParseMode.HTML разрешает использовать в сообщениях простую HTML-разметку
        # (<b>жирный</b>, <i>курсив</i> и т.п.) без явного указания parse_mode
        # в каждом вызове message.answer(...).
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Подключаем роутеры с обработчиками команд.
    # По мере роста бота здесь появятся другие include_router(...) —
    # по одному на каждую группу команд из брифа (дни рождения, ...).
    dp.include_router(start.router)
    dp.include_router(events.router)

    # Регистрируем список команд в Telegram — это и есть меню по кнопке
    # "/" в интерфейсе. Вызываем при каждом старте бота (не разово вручную
    # через BotFather), чтобы список автоматически оставался актуальным
    # по мере появления новых команд — не нужно отдельно не забыть
    # обновить его вручную при следующем релизе.
    await bot.set_my_commands(BOT_COMMANDS)

    # Перед запуском сбрасываем "зависшие" апдейты, которые Telegram мог
    # накопить, пока бот был выключен — иначе после запуска бот попытается
    # ответить на устаревшие сообщения.
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
