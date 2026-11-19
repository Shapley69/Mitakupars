"""Загрузка архивов по ссылкам (Mediafire, Pixeldrain, ouo.io), MTProto-команды."""
import asyncio
import logging
import mitaku_parser as mp
import shutil
import time
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from contextlib import suppress
from mediafire import is_mediafire_url
from ouo_downloader import OuoCaptcha, OuoError, extract_archive_urls, fetch_archive, is_ouo
from pathlib import Path
from . import core, delivery, util

@core.router.message(Command("mtproto"))
async def cmd_mtproto(m: Message):
    if not core.MT_ENABLED:
        return await m.answer(
            "MTProto выключен: бот может отправлять файлы только до 50 МБ.\n\n"
            "Чтобы включить (до 2 ГБ): получите api_id и api_hash на https://my.telegram.org "
            "(API development tools), задайте их в переменных MT_API_ID и MT_API_HASH, "
            "установите зависимости (pip install -r requirements.txt) и перезапустите бота.")
    if core.MT is None:
        return await m.answer("MTProto включён в настройках, но не запустился — см. логи бота.")
    known = [a for a in sorted(core.ADMINS) if core.jm.lib.kv_get(f"mt_hash:{a}")]
    await m.answer(
        "✅ <b>MTProto активен</b>\n"
        f"Файлы до {core.MAX_UPLOAD // core.MB} МБ: до 50 МБ — обычным способом, больше — через MTProto; "
        f"архивы режутся на ZIP-части по {core.ZIP_PART_SIZE // core.MB} МБ.\n"
        "В канал и группу большие файлы идут через личный чат с админом (загрузка → копирование).\n"
        f"Админов, известных MTProto: {len(known)} из {len(core.ADMINS)}"
        + ("" if len(known) == len(core.ADMINS) else " — остальным нужно написать боту любое сообщение")
        + "\n\nПроверка: /mttest 100 (в этот чат) или /mttest 100 group (в группу обсуждения канала).")


@core.router.message(Command("mttest"))
async def cmd_mttest(m: Message, command: CommandObject):
    """Отправляет тестовый файл N МБ через MTProto и показывает скорость."""
    args = (command.args or "").lower().split()
    mb = int(args[0]) if args and args[0].isdigit() else 60
    to_group = "group" in args
    if core.MT is None:
        return await m.answer("MTProto не запущен — см. /mtproto.")
    if not 1 <= mb <= 1900:
        return await m.answer("Размер теста: от 1 до 1900 МБ.")
    dest = m.chat.id
    if to_group:
        chat = await core.BOT.get_chat(core.CHANNEL_ID) if core.CHANNEL_ID else None
        dest = getattr(chat, "linked_chat_id", None)
        if not dest:
            return await m.answer("У канала нет группы обсуждения (или CHANNEL_ID не задан).")
    status = await m.answer(f"🧪 Создаю тестовый файл {mb} МБ…")
    work = core.STAGING / "mttest"
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"mttest_{mb}MB.bin"
    try:
        with path.open("wb") as fh:
            fh.truncate(mb * core.MB)
        started = time.monotonic()
        sent_id = await delivery.send_file_any(dest, path, caption=f"🧪 Тест MTProto, {mb} МБ", name=path.name,
                                      progress=delivery.upload_progress(status, path.name))
        seconds = max(time.monotonic() - started, 0.1)
        text = f"✅ {mb} МБ отправлено за {seconds:.0f} с (≈{mb / seconds:.1f} МБ/с)"
        if to_group:
            with suppress(Exception):
                await core.BOT.delete_message(dest, sent_id)
            text += " — тестовое сообщение в группе удалено"
        await status.edit_text(text)
    except Exception as e:  # noqa: BLE001
        logging.exception("mttest")
        await status.edit_text(f"❌ Тест не прошёл: {core.esc(str(e))}")
    finally:
        path.unlink(missing_ok=True)


async def deliver_archive(m, url, title=None, password=None, explain=True, rename_to=None):
    """Скачивает файл по ссылке (ouo.io / Mediafire / Pixeldrain) и отправляет в чат.
    Возвращает 'ok' | 'captcha' | 'error'."""
    status = await m.answer(f"📥 Скачиваю: {core.esc(url)}")
    work = core.STAGING / "archives" / str(int(time.time() * 1000))
    try:
        loop, last = asyncio.get_running_loop(), [0.0]

        def on_progress(name, done, total, index, count):   # вызывается из потока загрузки
            if time.monotonic() - last[0] < 4:
                return
            last[0] = time.monotonic()
            size = f"{done / core.MB:.1f} / {total / core.MB:.1f} МБ" if total else f"{done / core.MB:.1f} МБ"
            text = f"📥 Mediafire {index}/{count}: {core.esc(name)}\n{size}"
            asyncio.run_coroutine_threadsafe(delivery.edit_quiet(status, text), loop)

        try:
            limit = delivery.download_limit()
            if limit <= 0:
                await status.edit_text("❌ На диске бота не хватает места для загрузки файла.")
                return "error"
            files = await fetch_archive(url, work, progress=on_progress, max_bytes=limit)
        except OuoCaptcha as e:
            await status.edit_text(f"🔒 {core.esc(url)}\n" + (core.esc(str(e)) if explain else "нужна капча"))
            return "captcha"
        except OuoError as e:
            await status.edit_text(f"❌ {core.esc(url)}\n{core.esc(str(e))}")
            return "error"
        except Exception as e:  # noqa: BLE001
            logging.exception("Загрузка архива %s", url)
            await status.edit_text(f"❌ Неожиданная ошибка: {core.esc(type(e).__name__)}: {core.esc(str(e))}")
            return "error"
        await status.edit_text("📤 Отправляю…")
        lines = [f"<b>{core.esc(util.button_title(title, 200))}</b>"] if title else []
        if password:
            lines.append(f"Пароль архива: <code>{core.esc(password)}</code>")
        if rename_to:   # архив поста называем по подписи: «Ник – персонаж (игра)»
            renamed = []
            for index, (path, name) in enumerate(files, 1):
                stem = mp.safe_name(rename_to if len(files) == 1 else f"{rename_to} ({index})")
                new_path = path.with_name(stem + Path(name).suffix)
                renamed.append((path.replace(new_path) if new_path != path else path, new_path.name))
            files = renamed
        try:
            for path, name in files:
                await delivery.send_big_file(m, path, name,
                                    util._fit(lines + [f"Файл: <code>{core.esc(name)}</code>"], 1000),
                                    progress_for=lambda n: delivery.upload_progress(status, n))
        except Exception as e:  # noqa: BLE001
            logging.exception("Отправка архива %s", url)
            await status.edit_text(f"❌ Скачано, но Telegram не принял файл: {core.esc(str(e))}")
            return "error"
        await util.delete_message(status)
        return "ok"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@core.router.message(Command("mediafire"))
async def cmd_mediafire(m: Message, command: CommandObject):
    """Скачать файл или папку с Mediafire и прислать сюда (большое — ZIP-частями < 50 МБ)."""
    text = command.args or ""
    links = [u for u in extract_archive_urls(text) if is_mediafire_url(u)]
    if not links:
        extra = ("\nСсылки ouo.io бот не раскрывает (там капча): получите итоговую ссылку "
                 "Mediafire в браузере." if any(is_ouo(u) for u in extract_archive_urls(text)) else "")
        return await m.answer("Использование: /mediafire &lt;ссылка на файл или папку Mediafire&gt;\n"
                              "Бот скачает файл(ы), при необходимости разрежет на ZIP-части меньше "
                              "50 МБ и пришлёт сюда." + extra)
    for url in links[:5]:
        await deliver_archive(m, url)
