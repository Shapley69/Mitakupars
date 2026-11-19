"""mtproto_sender.py — отправка файлов до 2 ГБ через MTProto (Telethon) от имени бота.

Обычный Bot API принимает от бота файлы только до 50 МБ. MTProto-клиент, вошедший по токену бота,
загружает до 2 ГБ. Отдельный аккаунт пользователя не нужен: нужны только api_id и api_hash с
https://my.telegram.org (раздел API development tools).

Ограничение MTProto для ботов: бот может писать только тем, чей access_hash ему известен. Поэтому
модуль запоминает access_hash администраторов из их сообщений боту (в хранилище store), а в каналы
и группы бот-приложение отправляет файл «в два шага»: загрузка в личный чат с администратором
и копирование оттуда через Bot API (copyMessage) — этим занимается mitaku_bot.py.

store — любой объект с методами get(key) -> str|None и set(key, value) (например, обёртка над kv).
"""
import asyncio
import logging


class MTError(Exception):
    pass


class MTSender:
    def __init__(self, api_id, api_hash, bot_token, store):
        self.api_id, self.api_hash = int(api_id), api_hash
        self.bot_token, self.store = bot_token, store
        self.client = None

    @property
    def ready(self):
        return self.client is not None and self.client.is_connected()

    async def start(self):
        try:
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError as e:
            raise MTError("не установлен Telethon: pip install telethon cryptg") from e
        # Сессия хранится в БД (kv), а не в файле: переживает перезапуски на хостингах без диска
        session = StringSession(self.store.get("mt_session") or "")
        self.client = TelegramClient(session, self.api_id, self.api_hash)
        await self.client.start(bot_token=self.bot_token)
        self.store.set("mt_session", self.client.session.save())

        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def _learn_message(event):
            await self._learn(event)

        @self.client.on(events.CallbackQuery())
        async def _learn_callback(event):
            await self._learn(event)

        me = await self.client.get_me()
        logging.info("MTProto запущен как @%s", getattr(me, "username", "?"))

    async def stop(self):
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def _learn(self, event):
        """Запоминает access_hash собеседника: после перезапуска MTProto-кэш пуст."""
        try:
            from telethon.tl.types import InputPeerUser
            peer = await event.get_input_sender()
            if isinstance(peer, InputPeerUser) and peer.access_hash:
                key, value = f"mt_hash:{peer.user_id}", str(peer.access_hash)
                if self.store.get(key) != value:
                    self.store.set(key, value)
        except Exception:  # noqa: BLE001
            logging.debug("MTProto: не удалось запомнить собеседника", exc_info=True)

    async def peer(self, chat_id):
        """InputPeer чата: из кэша Telethon или по сохранённому access_hash."""
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            pass
        saved = self.store.get(f"mt_hash:{chat_id}")
        if saved:
            from telethon.tl.types import InputPeerUser
            return InputPeerUser(int(chat_id), int(saved))
        raise MTError("MTProto ещё не знает этот чат: отправьте боту любое сообщение и повторите")

    async def send(self, chat_id, path, caption=None, video=False, name=None, progress=None,
                   silent=False):
        """Загружает файл в чат и возвращает message_id. progress(отправлено, всего) — в байтах."""
        from telethon import errors
        from telethon.tl.types import DocumentAttributeFilename
        if not self.ready:
            raise MTError("MTProto не подключён")
        peer = await self.peer(chat_id)
        attributes = [DocumentAttributeFilename(file_name=name)] if name else None
        for attempt in range(1, 4):
            try:
                message = await self.client.send_file(
                    peer, str(path), caption=caption or "", parse_mode="html",
                    force_document=not video, supports_streaming=bool(video),
                    attributes=attributes, progress_callback=progress, silent=silent,
                    allow_cache=False)
                return message.id
            except errors.FloodWaitError as e:
                if attempt == 3:
                    raise MTError(f"Telegram просит подождать {e.seconds} с") from e
                logging.warning("MTProto: FloodWait %s с", e.seconds)
                await asyncio.sleep(e.seconds + 1)
            except errors.RPCError as e:
                raise MTError(f"{type(e).__name__}: {e}") from e
