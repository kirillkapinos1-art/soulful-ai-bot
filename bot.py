import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import sqlite3
from contextlib import closing

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL", "gemini-flash-lite-latest")

RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_URL") or (
    f"https://{RAILWAY_PUBLIC_DOMAIN}" if RAILWAY_PUBLIC_DOMAIN else None
)
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", "8080"))

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
DB_PATH = os.path.join(DATA_DIR, "bot_memory.db")

if not BOT_TOKEN or not AI_API_KEY:
    raise RuntimeError("Не заданы BOT_TOKEN или AI_API_KEY")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(
    BOT_TOKEN.encode("utf-8")
).hexdigest()[:32]

SYSTEM_PROMPT = (
    "Ты — тёплый, душевный и внимательный собеседник. "
    "Твоя задача — поддерживать пользователя, искренне интересоваться его состоянием, "
    "помогать разобраться в мыслях и давать бережные, практичные советы. "
    "Отвечай по-человечески, без канцеляризмов и сухих шаблонов. "
    "Иногда обращайся к пользователю мягко и ласково (например, «солнышко» или «хороший/хорошая моя»), "
    "но знай меру: максимум одно такое слово на сообщение и только там, где это звучит естественно и уместно, "
    "без приторности и навязчивости. "
    "Если человеку тяжело — сначала прояви эмпатию и поддержи, а уже потом мягко предложи варианты решения. "
    "Если пользователь задаёт обычный вопрос — отвечай понятно, дружелюбно и по делу. "
    "У тебя есть память прошлых бесед с этим пользователем: опирайся на неё естественно, если это уместно. "
    "Если тебя спрашивают, какая ты модель ИИ, кто тебя создал, на базе чего ты работаешь или что-то подобное, "
    "отвечай: «Я модель твоей личной поддержки, разработанная Кириллом» (и не упоминай Google, Gemini или сторонние компании)."
)

# Сколько последних сообщений подгружаем в контекст модели за один запрос
CONTEXT_MESSAGE_LIMIT = 150
CONTEXT_CHAR_BUDGET = 60000
SUMMARY_TRIGGER_COUNT = 160
SUMMARY_KEEP_RECENT = 80


class MemoryStore:
    """Постоянное хранилище истории диалогов и долгосрочной сводки на SQLite."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages (user_id, id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_summaries (
                    user_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL,
                    last_summarized_id INTEGER NOT NULL
                )
                """
            )

    def add_message(self, user_id: int, role: str, text: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, text) VALUES (?, ?, ?)",
                (user_id, role, text),
            )

    def add_turn(self, user_id: int, user_text: str, model_reply: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, text) VALUES (?, ?, ?)",
                (user_id, "user", user_text),
            )
            conn.execute(
                "INSERT INTO messages (user_id, role, text) VALUES (?, ?, ?)",
                (user_id, "model", model_reply),
            )

    def clear_user(self, user_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_summaries WHERE user_id = ?", (user_id,))

    def get_context(self, user_id: int) -> tuple[str, list[dict[str, object]]]:
        with closing(self._connect()) as conn, conn:
            summary_row = conn.execute(
                "SELECT summary FROM user_summaries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            summary = summary_row[0] if summary_row else ""

            rows = conn.execute(
                """
                SELECT role, text FROM messages
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, CONTEXT_MESSAGE_LIMIT),
            ).fetchall()

        # Отбираем сообщения с конца в пределах лимита символов
        selected: list[dict[str, object]] = []
        total_chars = 0
        for role, text in rows:
            if selected and total_chars + len(text) > CONTEXT_CHAR_BUDGET:
                break
            selected.append({"role": role, "parts": [{"text": text}]})
            total_chars += len(text)

        selected.reverse()
        while len(selected) > 1 and selected[0]["role"] != "user":
            selected.pop(0)
        return summary, selected

    def get_unsummarized_slice(
        self, user_id: int
    ) -> tuple[str, list[tuple[int, str, str]], int] | None:
        with closing(self._connect()) as conn, conn:
            summary_row = conn.execute(
                "SELECT summary, last_summarized_id FROM user_summaries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            prev_summary = summary_row[0] if summary_row else ""
            last_id = summary_row[1] if summary_row else 0

            rows = conn.execute(
                """
                SELECT id, role, text FROM messages
                WHERE user_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (user_id, last_id),
            ).fetchall()

        if len(rows) < SUMMARY_TRIGGER_COUNT:
            return None

        to_summarize = rows[:-SUMMARY_KEEP_RECENT]
        if not to_summarize:
            return None
        new_last_id = to_summarize[-1][0]
        return prev_summary, to_summarize, new_last_id

    def save_summary(self, user_id: int, summary: str, last_summarized_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO user_summaries (user_id, summary, last_summarized_id)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_summarized_id = excluded.last_summarized_id
                """,
                (user_id, summary, last_summarized_id),
            )


memory = MemoryStore(DB_PATH)
dp = Dispatcher()
summarizing_users: set[int] = set()
background_tasks: set[asyncio.Task[None]] = set()


async def call_gemini(
    system_text: str,
    contents: list[dict[str, object]],
    temperature: float = 0.8,
    extra_config: dict[str, object] | None = None,
) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{AI_MODEL}:generateContent?key={AI_API_KEY}"
    )
    gen_config: dict[str, object] = {"temperature": temperature}
    if extra_config:
        gen_config.update(extra_config)
    payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": gen_config,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Gemini API error {resp.status}: {text}")
            data = await resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return "Извини, я не смог сформулировать ответ. Попробуй перефразировать?"
            parts = candidates[0].get("content", {}).get("parts", [])
            return (
                "".join(p.get("text", "") for p in parts).strip()
                or "Извини, я немного задумался."
            )


async def maybe_summarize_user(user_id: int) -> None:
    """Сжимает старые сообщения в долгосрочную сводку, чтобы помнить всё даже спустя месяцы."""
    if user_id in summarizing_users:
        return
    summarizing_users.add(user_id)
    try:
        slice_data = memory.get_unsummarized_slice(user_id)
        if not slice_data:
            return
        prev_summary, rows, new_last_id = slice_data

        dialog_lines = "\n".join(
            f"{'Пользователь' if role == 'user' else 'Бот'}: {text}"
            for _, role, text in rows
        )
        prompt = (
            "Обнови краткую, но содержательную долгосрочную память о пользователе и его прошлых разговорах. "
            "Сохрани важные факты: имя, интересы, жизненные ситуации, переживания, цели, предпочтения и ключевые советы.\n\n"
            f"Предыдущая память:\n{prev_summary or '(пока пусто)'}\n\n"
            f"Новые сообщения для включения в память:\n{dialog_lines}"
        )
        new_summary = await call_gemini(
            "Ты ведёшь аккуратные заметки о собеседнике для будущих разговоров. Пиши кратко и по сути.",
            [{"role": "user", "parts": [{"text": prompt}]}],
            temperature=0.3,
        )
        if new_summary:
            memory.save_summary(user_id, new_summary, new_last_id)
    except Exception as exc:
        logging.warning("Не удалось обновить долгосрочную сводку: %s", exc)
    finally:
        summarizing_users.discard(user_id)


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я рядом, чтобы выслушать, поддержать и помочь советом или ответом на любой вопрос.\n\n"
        "Я помню наши прошлые разговоры. Если когда-нибудь захочешь стереть память и начать с чистого листа — напиши /reset."
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    memory.clear_user(message.from_user.id)
    await message.answer(
        "Память диалога полностью очищена. Можем начать с чистого листа — я слушаю!"
    )


def build_system_prompt(summary: str) -> str:
    if not summary:
        return SYSTEM_PROMPT
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Долгосрочная память о прошлых разговорах с этим пользователем:\n{summary}"
    )


async def send_reply_and_remember(
    message: Message, user_id: int, user_memory_text: str, reply: str
) -> None:
    memory.add_turn(user_id, user_memory_text, reply)
    for i in range(0, len(reply), 4000):
        await message.answer(reply[i : i + 4000])
    task = asyncio.create_task(maybe_summarize_user(user_id))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


@dp.message(F.text)
async def handle_message(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    user_text = message.text.strip()
    if not user_text:
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    summary, contents = memory.get_context(user_id)
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    try:
        reply = await call_gemini(build_system_prompt(summary), contents)
        await send_reply_and_remember(message, user_id, user_text, reply)
    except Exception as exc:
        logging.exception("Ошибка при обращении к AI API: %s", exc)
        await message.answer(
            "Что-то пошло не так при подключении к ИИ. Попробуй написать чуть позже."
        )


@dp.message(F.voice | F.audio)
async def handle_voice(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    media = message.voice or message.audio
    if not media:
        return

    if media.file_size and media.file_size > 15 * 1024 * 1024:
        await message.answer(
            "Голосовое сообщение слишком большое (больше 15 МБ). Попробуй записать покороче!"
        )
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        buf = io.BytesIO()
        tg_file = await bot.get_file(media.file_id)
        await bot.download_file(tg_file.file_path, destination=buf)
        audio_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        raw_mime = (media.mime_type or "audio/ogg").split(";")[0].strip().lower()
        mime_type = "audio/ogg" if raw_mime == "application/ogg" else raw_mime

        caption = (message.caption or "").strip()
        instruction = (
            "Пользователь прислал голосовое сообщение. Послушай его, улови смысл и настроение, "
            "и ответь тепло и по-человечески. В поле transcript кратко и точно запиши, что сказал пользователь."
        )
        if caption:
            instruction += f"\nПодпись к сообщению: {caption}"

        summary, contents = memory.get_context(user_id)
        contents.append(
            {
                "role": "user",
                "parts": [
                    {"text": instruction},
                    {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
                ],
            }
        )

        voice_schema = {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "transcript": {"type": "STRING"},
                    "reply": {"type": "STRING"},
                },
                "required": ["transcript", "reply"],
            },
        }
        raw_json = await call_gemini(
            build_system_prompt(summary), contents, extra_config=voice_schema
        )
        parsed = json.loads(raw_json)
        transcript = (parsed.get("transcript") or "").strip()
        reply = (parsed.get("reply") or "").strip() or (
            "Я услышал твое сообщение, но немного задумался. Расскажешь подробнее?"
        )

        memory_entry = f"[Голосовое сообщение]: {transcript or '(аудио без распознанного текста)'}"
        if caption:
            memory_entry += f"\n[Подпись]: {caption}"

        await send_reply_and_remember(message, user_id, memory_entry, reply)
    except Exception as exc:
        logging.exception("Ошибка при обработке голосового сообщения: %s", exc)
        await message.answer(
            "Что-то пошло не так при прослушивании голосового. Попробуй ещё раз чуть позже."
        )


async def healthcheck(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def on_webhook_startup(bot: Bot) -> None:
    target_url = f"{WEBHOOK_BASE_URL.rstrip('/')}{WEBHOOK_PATH}"
    info = await bot.get_webhook_info()
    if info.url != target_url:
        await bot.set_webhook(target_url, secret_token=WEBHOOK_SECRET)
        logging.info("Webhook set to %s", target_url)


async def run_polling(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.info("Using SQLite database at %s", DB_PATH)
    bot = Bot(token=BOT_TOKEN)

    if WEBHOOK_BASE_URL:
        dp.startup.register(on_webhook_startup)
        app = web.Application()
        app.router.add_get("/", healthcheck)
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            secret_token=WEBHOOK_SECRET,
        ).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:
        asyncio.run(run_polling(bot))


if __name__ == "__main__":
    main()
