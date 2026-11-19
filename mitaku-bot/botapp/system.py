"""Справка, проверка здоровья, резервные копии и восстановление базы."""
import asyncio
import logging
import os
import sqlite3
import tempfile
import time
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from pathlib import Path
from . import core, texts, ui

# --------------------------------------------------------------------------- команды
@core.router.message(Command("start", "help"))
async def cmd_help(m: Message):
    await ui.drop_reply_keyboard(m.bot, m.chat.id)
    await m.answer(texts.HELP)


async def health_check(reader, writer):
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
    except asyncio.TimeoutError:
        status, body = "408 Request Timeout", b"request timeout\n"
    else:
        parts = request_line.split()
        if len(parts) >= 2 and parts[0] == b"GET" and parts[1] == b"/health":
            status, body = "200 OK", b"ok\n"
        else:
            status, body = "404 Not Found", b"not found\n"
    writer.write(
        f"HTTP/1.1 {status}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Content-Type: text/plain\r\n\r\n".encode("ascii") + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def _database_is_valid(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def restore_database():
    database_path = Path(core.OUT) / "library.db"
    backup_path = Path(core.OUT) / "library.db.backup"
    database_path.parent.mkdir(parents=True, exist_ok=True)

    if database_path.exists() and _database_is_valid(database_path):
        return
    if not backup_path.exists():
        if database_path.exists():
            raise RuntimeError(
                f"{database_path} повреждена, а резервная копия {backup_path} отсутствует"
            )
        return
    if not _database_is_valid(backup_path):
        raise RuntimeError(f"Резервная копия {backup_path} повреждена")

    fd, temporary_path = tempfile.mkstemp(
        prefix="library.db.restore.", suffix=".tmp", dir=database_path.parent)
    os.close(fd)
    try:
        source = sqlite3.connect(backup_path)
        destination = sqlite3.connect(temporary_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{database_path}{suffix}").unlink(missing_ok=True)
        os.replace(temporary_path, database_path)
        logging.warning("База восстановлена из резервной копии %s", backup_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def create_database_backup(library):
    backup_path = Path(core.OUT) / "library.db.backup"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix="library.db.backup.", suffix=".tmp", dir=backup_path.parent)
    os.close(fd)
    try:
        with library.lock:
            destination = sqlite3.connect(temporary_path)
            try:
                library.db.backup(destination)
            finally:
                destination.close()
        os.replace(temporary_path, backup_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return backup_path


async def backup_database(bot, library):
    backup_path = await asyncio.to_thread(create_database_backup, library)
    caption = f"Резервная копия library.db — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
    for admin in core.ADMINS:
        try:
            await bot.send_document(
                admin, FSInputFile(str(backup_path), filename="library.db"),
                caption=caption)
        except Exception as e:  # noqa: BLE001
            logging.exception("Не удалось отправить резервную копию админу %s: %s", admin, e)


async def daily_backup(bot, library):
    while True:
        try:
            await backup_database(bot, library)
        except Exception:
            logging.exception("Не удалось создать или отправить резервную копию базы")
        await asyncio.sleep(core.BACKUP_INTERVAL)
