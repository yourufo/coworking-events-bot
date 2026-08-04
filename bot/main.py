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

from bot.config import BOT_TOKEN
from bot.database import init_db
from bot.handlers import start


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

    # Подключаем роутер с обработчиком /start.
    # По мере роста бота здесь появятся другие include_router(...) —
    # по одному на каждую группу команд из брифа (события, дни рождения…).
    dp.include_router(start.router)

    # Перед запуском сбрасываем "зависшие" апдейты, которые Telegram мог
    # накопить, пока бот был выключен — иначе после запуска бот попытается
    # ответить на устаревшие сообщения.
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
