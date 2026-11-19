"""Настройки из окружения, константы лимитов, общее состояние (jm, BOT, MT, очередь публикации), роутеры aiogram и middleware."""
import asyncio
import html
import itertools
import json
import logging
import os
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from aiogram.types import Message
from mitaku_jobs import JobManager
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
OUT = os.getenv("MITAKU_OUT", "mitaku_data")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
HEALTH_PORT = int(os.getenv("PORT", "10000"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "").strip()
MT_API_ID = os.getenv("MT_API_ID", "").strip()
MT_API_HASH = os.getenv("MT_API_HASH", "").strip()
MT_ENABLED = bool(MT_API_ID and MT_API_HASH)
MT = None   # MTSender; создаётся в main(), если заданы MT_API_ID и MT_API_HASH
MB = 1024 * 1024
BACKUP_INTERVAL = 24 * 60 * 60
# Облачный Bot API принимает от бота файлы до 50 МБ; локальный сервер и MTProto — до 2000 МБ.
# BOTAPI_UPLOAD — что можно отправить обычным способом, MAX_UPLOAD — что вообще можно отправить
# (apply_limits() уточняет значения после запуска MTProto).
BOTAPI_UPLOAD = (1990 if LOCAL_API_URL else 49) * MB
MAX_UPLOAD = (1990 if (LOCAL_API_URL or MT_ENABLED) else 49) * MB
ZIP_PART_SIZE = (1900 if (LOCAL_API_URL or MT_ENABLED) else 44) * MB
UPLOAD_TIMEOUT = 3600   # сек на отправку одного файла
SEND_PAUSE = 3          # сек между сообщениями в группу (лимит Telegram ~20 сообщений/мин)
STAGING = Path(OUT) / "staging"   # временные файлы (режим storage=meta); чистятся сразу после выдачи
SHORT = {"mitaku": "MI", "cosplaytele": "CT", "galleryepic": "GE", "xasiat": "XA", "haneame": "HN"}
PAGE = 8
SITE_PAGE = 10
FILE_PAGE = 8
PAGES_CHOICES = (1, 3, 5, 10, 20)
SEARCH_PAGE = 10
INLINE_MAX = 3   # кнопок на экране больше этого числа -> нижняя клавиатура (строка ввода)
esc = html.escape


def short(site_key):
    return SHORT.get(site_key, (site_key or "?")[:2].upper())


class TrackingBot(Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_message_ids = {}

    @staticmethod
    def _storage_key(chat_id):
        return f"telegram_messages:{chat_id}"

    def _persist_message_ids(self, chat_id):
        if jm is None:
            return
        key = self._storage_key(chat_id)
        ids = sorted(self.sent_message_ids.get(chat_id, set()))
        jm.lib.kv_set(key, json.dumps(ids))

    def forget_message(self, chat_id, message_id):
        ids = self.sent_message_ids.get(chat_id)
        if ids:
            ids.discard(message_id)
        self._persist_message_ids(chat_id)

    def _track_result(self, result):
        messages = result if isinstance(result, list) else [result]
        for message in messages:
            if isinstance(message, Message) and message.chat.id in ADMINS:  # только личные чаты админов
                ids = self.sent_message_ids.setdefault(message.chat.id, set())
                ids.add(message.message_id)
                while len(ids) > 300:
                    ids.discard(min(ids))
                self._persist_message_ids(message.chat.id)

    async def __call__(self, method, request_timeout=None):
        result = await super().__call__(method, request_timeout=request_timeout)
        if isinstance(method, DeleteMessage):
            try:
                chat_id = int(method.chat_id)
            except (TypeError, ValueError):
                chat_id = None
            if chat_id is not None:
                self.forget_message(chat_id, method.message_id)
        self._track_result(result)
        return result

    async def clear_chat_messages(self, chat_id):
        message_ids = set(self.sent_message_ids.get(chat_id, set()))
        if jm is not None:
            raw = jm.lib.kv_get(self._storage_key(chat_id), "[]")
            message_ids.update(json.loads(raw))
        for message_id in sorted(message_ids, reverse=True):
            try:
                await self.delete_message(chat_id, message_id)
            except TelegramBadRequest as e:
                logging.warning("Не удалось удалить сообщение %s: %s", message_id, e)
                self.forget_message(chat_id, message_id)


router = Router()
router.message.filter(F.from_user.id.in_(ADMINS))
router.callback_query.filter(F.from_user.id.in_(ADMINS))


async def clear_before_command(handler, event, data):
    if (isinstance(event, Message) and event.text and event.text.startswith("/")
            and event.from_user and event.from_user.id in ADMINS):
        bot = data.get("bot")
        if isinstance(bot, TrackingBot):
            await bot.clear_chat_messages(event.chat.id)
    return await handler(event, data)


router.message.outer_middleware(clear_before_command)

jm: JobManager = None  # создаётся в main()
SEARCHES: dict = {}    # token -> {"q", "field", "items"}
_tok = itertools.count(1)


# --------------------------------------------------------------------------- канал и комментарии
channel_router = Router()   # без фильтра по админам: автопересылки приходят от Telegram, не от человека
PUBLISH_Q: asyncio.Queue = None
LAST_PUBLISH = 0.0
PUBLISH_LOCK = None   # asyncio.Lock, создаётся в main()

BOT = None   # Bot (TrackingBot); создаётся в app.main()
