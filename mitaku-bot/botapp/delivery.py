"""Отправка в Telegram: повторы при лимитах, отправка файлов (Bot API и MTProto), архивы, видео."""
import asyncio
import logging
import mitaku_parser as mp
import os
import shutil
import time
import video_fetch
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile, LinkPreviewOptions
from contextlib import suppress
from filesplit import iter_zip_parts, join_hint
from mtproto_sender import MTError
from pathlib import Path
from . import core, postfiles, publishing, util

async def send_with_preview(bot, chat_id, r, caption, text, markup=None):
    """Сообщение с превью поста — всегда. Порядок: сжатое фото -> фото по ссылке сайта ->
    текст со ссылочным превью Telegram. caption ≤ 1024 символов, text — полный текст."""
    prev = await postfiles.get_preview(r)
    if prev:
        data, name = prev
        try:
            return await tg_call(bot.send_photo, chat_id, BufferedInputFile(data, name),
                                 caption=caption, reply_markup=markup)
        except TelegramBadRequest as e:
            logging.warning("Превью %s не принято Telegram: %s", r["slug"], e)
    for url in postfiles.photo_urls(r)[:3]:
        try:
            return await tg_call(bot.send_photo, chat_id, url, caption=caption,
                                 reply_markup=markup)
        except TelegramBadRequest as e:
            logging.warning("Фото по ссылке %s не принято: %s", url, e)
    return await tg_call(
        bot.send_message, chat_id, text, reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=False, url=r["url"],
                                                show_above_text=True))


class KvStore:
    """Хранилище для MTProto-модуля: сессия и access_hash лежат в БД (kv)."""

    def __init__(self, lib):
        self.lib = lib

    def get(self, key):
        return self.lib.kv_get(key) or None

    def set(self, key, value):
        self.lib.kv_set(key, value)


def apply_limits():
    """Лимиты отправки по факту: локальный Bot API или запущенный MTProto — до 2 ГБ, иначе 50 МБ."""
    big = bool(core.LOCAL_API_URL) or core.MT is not None
    core.MAX_UPLOAD = (1990 if big else 49) * core.MB
    core.ZIP_PART_SIZE = (1900 if big else 44) * core.MB


def download_limit():
    """Сколько байт можно скачать на диск бота: свободное место минус запас под ZIP-часть."""
    core.STAGING.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(core.STAGING).free
    reserve = min(core.ZIP_PART_SIZE, 2000 * core.MB) + 200 * core.MB
    return max(0, min(free - reserve, 8 * 1024 * core.MB))


def upload_progress(status, name):
    """Колбэк прогресса загрузки через MTProto: правит сообщение статуса не чаще раза в 4 с."""
    last = [0.0]

    def callback(sent, total):
        now = time.monotonic()
        if now - last[0] < 4 and sent < total:
            return
        last[0] = now
        asyncio.ensure_future(edit_quiet(
            status, f"📤 Отправляю {core.esc(name)}: {sent / core.MB:.0f} / {total / core.MB:.0f} МБ"))
    return callback


def thread_progress(status, label):
    """Колбэк прогресса для загрузки в потоке (скачано, всего): правит статус не чаще раза в 4 с."""
    loop, last = asyncio.get_running_loop(), [0.0]

    def callback(done, total):
        if time.monotonic() - last[0] < 4:
            return
        last[0] = time.monotonic()
        size = f"{done / core.MB:.1f} / {total / core.MB:.1f} МБ" if total else f"{done / core.MB:.1f} МБ"
        asyncio.run_coroutine_threadsafe(edit_quiet(status, f"{label}\n{size}"), loop)
    return callback


async def download_video(url, rec, progress=None):
    """Скачивает видео из плеера/потока (yt-dlp) во временную папку; возвращает путь к файлу."""
    limit = download_limit()
    if limit <= 0:
        raise RuntimeError("на диске бота не хватает места для скачивания видео")
    work = core.STAGING / "videos" / f"{mp.safe_name(rec['slug'])}-{int(time.time() * 1000)}"
    try:
        return await asyncio.to_thread(video_fetch.download, url, work, rec["url"], limit, progress)
    except video_fetch.VideoError as e:
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"видео из плеера: {e}") from e


def message_id_of(sent):
    return getattr(sent, "message_id", sent)


async def send_file_any(chat_id, path, *, caption=None, rp=None, video=False, name=None,
                        progress=None):
    """Отправляет файл с диска; возвращает message_id. До лимита Bot API — обычным способом,
    больше — через MTProto (до 2 ГБ). rp — ReplyParameters (комментарии под постом)."""
    path = Path(path)
    size = path.stat().st_size
    name = name or path.name
    if size <= core.BOTAPI_UPLOAD:
        kwargs = {"caption": caption}
        if rp:
            kwargs["reply_parameters"] = rp
        upload = FSInputFile(path, filename=name)
        if video:
            sent = await tg_call(core.BOT.send_video, chat_id=chat_id, video=upload,
                                 supports_streaming=True, **kwargs)
        else:
            sent = await tg_call(core.BOT.send_document, chat_id=chat_id, document=upload, **kwargs)
        return message_id_of(sent)
    if core.MT is None:
        raise RuntimeError(f"файл {size / core.MB:.0f} МБ больше лимита Bot API (50 МБ): включите "
                           "MTProto — задайте MT_API_ID и MT_API_HASH")
    if size > core.MAX_UPLOAD:
        raise RuntimeError(f"файл {size / core.MB:.0f} МБ больше лимита Telegram (2 ГБ)")
    direct = chat_id in core.ADMINS   # личный чат админа: MTProto знает его access_hash
    storage = chat_id if direct else int(os.getenv("MT_STORAGE_CHAT") or sorted(core.ADMINS)[0])
    try:
        mid = await core.MT.send(storage, path, caption=caption, video=video, name=name,
                            progress=progress, silent=not direct)
    except MTError as e:
        raise RuntimeError(f"MTProto: {e}") from e
    if direct:
        return mid
    # Канал/группа: MTProto-боту их access_hash неизвестен, поэтому файл загружен в личный чат,
    # а отсюда копируется через Bot API (copyMessage переносит файл без повторной загрузки)
    try:
        copied = await tg_call(core.BOT.copy_message, chat_id=chat_id, from_chat_id=storage,
                               message_id=mid, reply_parameters=rp)
    finally:
        with suppress(Exception):
            await core.BOT.delete_message(storage, mid)
    return message_id_of(copied)


async def aiter_thread(gen):
    """Итерирует синхронный генератор в потоке, не блокируя цикл событий."""
    while True:
        item = await asyncio.to_thread(next, gen, None)
        if item is None:
            return
        yield item


async def send_big_file(message, path, name, caption, progress_for=None):
    """Файл целиком (если влезает в лимит) или ZIP-частями меньше лимита Telegram.
    progress_for(имя_файла) -> колбэк прогресса загрузки (MTProto)."""
    path = Path(path)
    if path.stat().st_size <= core.MAX_UPLOAD:
        await send_file_any(message.chat.id, path, caption=caption, name=name,
                            progress=progress_for(name) if progress_for else None)
        return
    parts = iter_zip_parts(path, name, path.parent / "parts", core.ZIP_PART_SIZE)
    async for index, total, zip_path in aiter_thread(parts):
        try:
            await send_file_any(message.chat.id, zip_path,
                                caption=util._fit([caption, f"Часть {index}/{total}"], 1000),
                                progress=progress_for(zip_path.name) if progress_for else None)
        finally:
            zip_path.unlink(missing_ok=True)   # сразу освобождаем диск
        await asyncio.sleep(1)
    await message.answer(join_hint(core.esc(name)))


async def edit_quiet(message, text):
    with suppress(Exception):
        await message.edit_text(text)


# --------------------------------------------------------------------------- Telegram: повторы при лимитах
# Автозамедление: после серьёзного флуд-лимита Telegram пауза между постами растёт (×2, до ×8),
# а через 30 минут без лимитов постепенно возвращается к обычной.
FLOOD = {"factor": 1.0, "last": 0.0}


def flood_factor():
    now = time.monotonic()
    if FLOOD["factor"] > 1 and now - FLOOD["last"] > 1800:
        FLOOD["factor"] = max(1.0, FLOOD["factor"] / 2)
        FLOOD["last"] = now
    return FLOOD["factor"]


def note_flood(retry_after):
    """Регистрирует флуд-лимит; возвращает новый множитель, если он вырос."""
    if retry_after < 5:    # мелкие лимиты на отдельные сообщения — не повод замедляться
        return None
    before = FLOOD["factor"]
    FLOOD["factor"] = min(before * 2, 8.0)
    FLOOD["last"] = time.monotonic()
    return FLOOD["factor"] if FLOOD["factor"] > before else None


async def tg_call(fn, *args, **kwargs):
    for _ in range(8):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            logging.warning("Лимит Telegram, жду %s с", e.retry_after)
            grown = note_flood(e.retry_after)
            if grown:
                asyncio.ensure_future(notify_admins(
                    f"⚠️ Telegram ограничил отправку (ждать {e.retry_after} с). Пауза между постами "
                    f"автоматически увеличена ×{grown:g}; через 30 минут без лимитов вернётся."))
            await asyncio.sleep(e.retry_after + 1)
    raise RuntimeError("слишком много повторов из-за лимитов Telegram")


async def notify_admins(text, markup=None):
    for admin in core.ADMINS:
        try:
            await core.BOT.send_message(admin, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            logging.warning("Не удалось написать админу %s: %s", admin, e)


async def send_parts(chat_id, rp, rec, selected, label):
    """ZIP-части (≤ лимита Telegram) ответами в комментарии."""
    temp_dir, parts, split_files = await asyncio.to_thread(postfiles.create_zip_parts, rec, selected)
    try:
        for i, path in enumerate(parts, 1):
            await send_file_any(chat_id, path,
                                caption=f"{core.esc(publishing.archive_label(rec))} — {label} {i}/{len(parts)}", rp=rp)
            await asyncio.sleep(core.SEND_PAUSE)
        for name, _count in split_files:
            await tg_call(core.BOT.send_message, chat_id=chat_id, reply_parameters=rp,
                          text=join_hint(core.esc(name)))
    finally:
        temp_dir.cleanup()
    return len(parts)
