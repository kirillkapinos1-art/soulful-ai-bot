# Soulful AI Telegram Bot

Telegram-бот поддержки и персонального общения на базе **aiogram 3** и **Google Gemini API** с долгосрочной памятью в **SQLite**, распознаванием голосовых сообщений и поддержкой 24/7 webhook-деплоя на **Railway**.

## Возможности

- **Контекстная долгосрочная память (`SQLite` WAL):** хранение истории диалогов с каждого пользователя и фоновая автоматическая суммаризация старых сообщений (`user_summaries`), чтобы бот помнил ключевые факты на дистанции.
- **Мультимодальная обработка голосовых и аудио:** загрузка голосовых сообщений до 15 МБ, распознавание речи и генерация ответа через структурированный JSON (`responseSchema`: `transcript` + `reply`).
- **Два режима запуска:**
  - **Webhook** (`aiohttp` + `SimpleRequestHandler` + healthcheck `/`) при наличии `WEBHOOK_URL` или `RAILWAY_PUBLIC_DOMAIN`.
  - **Long Polling** для локального запуска.
- **Команды:** `/start`, `/reset` (полная очистка памяти диалога).

## Быстрый старт

1. Создайте и активируйте виртуальное окружение, установите зависимости:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Скопируйте `.env.example` в `.env` и заполните переменные:
   - `BOT_TOKEN` — токен от `@BotFather`
   - `AI_API_KEY` — API-ключ Google Gemini
   - `AI_MODEL` — модель Gemini (по умолчанию `gemini-flash-lite-latest`)

3. Запустите бота локально:
   ```bash
   python bot.py
   ```

## Деплой на Railway

В репозитории уже подготовлен `Procfile` (`web: python bot.py`). Достаточно задать переменные окружения `BOT_TOKEN` и `AI_API_KEY` (при желании подключить Volume и указать `RAILWAY_VOLUME_MOUNT_PATH` для сохранения `bot_memory.db` между редеплоями).
