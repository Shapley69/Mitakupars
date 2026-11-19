#!/usr/bin/env python3
"""
mitaku_bot.py — Telegram-бот (aiogram 3.7+) для управления парсером косплей-сайтов
(mitaku, cosplaytele, galleryepic, xasiat, haneame) и публикации новых постов в канал.

Настройка через переменные окружения или файл .env (см. .env.example):
  BOT_TOKEN   — токен от @BotFather
  ADMIN_IDS   — Telegram ID через запятую (бот отвечает ТОЛЬКО им)
  MITAKU_OUT  — папка библиотеки (по умолчанию mitaku_data)
  CHANNEL_ID  — (необязательно) канал для публикаций: @username или -100..., бот должен быть
                админом канала; для комментариев у канала должна быть группа обсуждения,
                где бот тоже админ (см. README, раздел «Канал»)
  LOCAL_API_URL — (необязательно) локальный Bot API сервер, например http://localhost:8081:
                  отправка файлов до ~2 ГБ вместо 50 МБ (README, раздел «Большие файлы»)

Запуск:  python mitaku_bot.py
Разовый выход с облачного api.telegram.org перед переходом на локальный сервер:
         python mitaku_bot.py --logout
"""
import asyncio
import html
import itertools
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.methods import DeleteMessage
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument,
                           InputMediaPhoto, Message, ReplyParameters)

import mitaku_parser as mp
import mitaku_sites as ms
from mitaku_jobs import JobManager
from ouo_downloader import OuoClient, OuoError, extract_ouo_url

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
MB = 1024 * 1024
BACKUP_INTERVAL = 24 * 60 * 60
# Облачный Bot API: файлы до 50 МБ. Локальный сервер: до 2000 МБ.
MAX_UPLOAD = (1990 if LOCAL_API_URL else 49) * MB
ZIP_PART_SIZE = (1900 if LOCAL_API_URL else 44) * MB
UPLOAD_TIMEOUT = 3600   # сек на отправку одного файла
SEND_PAUSE = 3          # сек между сообщениями в группу (лимит Telegram ~20 сообщений/мин)
STAGING = Path(OUT) / "staging"   # временные файлы (режим storage=meta); чистятся сразу после выдачи
SHORT = {"mitaku": "MI", "cosplaytele": "CT", "galleryepic": "GE", "xasiat": "XA", "haneame": "HN"}
PAGE = 8
SITE_PAGE = 10
FILE_PAGE = 8
PAGES_CHOICES = (1, 3, 5, 10, 20)
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

HELP = """<b>Косплей-бот</b> — mitaku, cosplaytele, galleryepic, xasiat🧪, haneame🧪

<b>Сайты</b>
/sites — выбрать активные сайты (для /sync, /update, поиска на сайте и канала)

<b>Загрузка файла</b>
Пришлите ссылку ouo.io отдельным сообщением

<b>Загрузка</b> (по умолчанию в базу идут только метаданные, файлы на ПК не сохраняются)
/sync — выбрать сайт → раздел → число страниц кнопками. Или сразу: /sync cosplaytele cosplay-ero 3
/update [сайт] [раздел] — только новое: идёт по страницам, пока не встретит известные
/get &lt;ссылка или slug&gt; — добавить/перепроверить один пост (сайт определяется сам)
/pause · /resume · /stop — пауза, продолжить, остановить (очередь очищается)
/continue — продолжить незавершённую задачу (в т.ч. после перезапуска бота)
/status — прогресс

<b>Поиск и библиотека</b>
/search &lt;текст&gt; — по всему (можно просто написать текст)
/cosplayer &lt;ник&gt; · /game &lt;игра/аниме&gt; · /character &lt;персонаж&gt; · /tag &lt;тег&gt;
Если в библиотеке пусто — предложу поискать на выбранных сайтах.
/list [страница] — последние посты · /stats — статистика
/links [текст] — файл со ссылками на архивы (для ручного открытия)
В карточке поста: превью, 📦 ZIP, 🖼 выбор файла — файлы скачиваются с сайта на лету и сразу удаляются.

<b>Канал</b>
/channel — состояние, вкл/выкл автопубликации, проверка сейчас, тест
/publish &lt;slug или ссылка&gt; — опубликовать пост в канал (фото/видео/ZIP — в комментариях)

<b>Настройки</b>
/set — показать · /set storage meta|full · zip on|off · images on|off · videos on|off ·
workers 1-16 · delay сек · channel on|off · channel_interval мин · channel_media photos|documents ·
channel_zip on|off · channel_videos on|off"""

# --------------------------------------------------------------------------- вспомогательное
def fmt_row(r):
    bits = [x for x in (r.get("cosplayer"), r.get("source"), r.get("character")) if x]
    mark = " ⚠️" if r["status"] == "partial" else ""
    return f"[{short(r.get('site'))}] {esc(r['title'])}{mark}" + (f"\n    <i>{esc(' / '.join(bits))}</i>" if bits else "")


def _fit(lines, limit):
    """Отбрасывает строки с конца, пока текст не влезет (не режем посреди HTML-тега)."""
    lines = list(lines)
    while len("\n".join(lines)) > limit and len(lines) > 1:
        lines.pop()
    return "\n".join(lines)


def card(r, short=False):
    """short=True — подпись к фото (лимит Telegram 1024 символа)."""
    m = json.loads(r["meta_json"] or "{}")
    d = m.get("declared", {})
    lines = [f"<b>{esc(r['title'])}</b>"]
    site = ms.SITES.get(r.get("site"))
    if site:
        lines.append(f"Сайт: {esc(site.title)}")
    for label, key in (("Косплеер", "cosplayer"), ("Персонаж", "character"), ("Игра/аниме", "source")):
        if m.get(key):
            lines.append(f"{label}: {esc(m[key])}")
    if m.get("tags"):
        lines.append("Теги: " + esc(", ".join(m["tags"])[:200]))
    if d.get("content"):
        lines.append(f"Заявлено: {esc(d['content'])}")
    for label, key in (("Разрешение", "resolution"), ("Длительность", "duration"),
                       ("Размер архива", "file_size")):
        if d.get(key):
            lines.append(f"{label}: {esc(d[key])}")
    fm = m.get("formats", {})
    fmt = [f"{k} {ext}×{n}" for k, v in fm.items() for ext, n in v.items()]
    if r["status"] == "meta":
        lines.append(f"Найдено на сайте: {len(m.get('files', []))} файлов"
                     + (f" ({', '.join(fmt)})" if fmt else "") + " — на ПК не хранятся")
    else:
        lines.append(f"Скачано: {len(m.get('files', []))} файлов, {mp.human_size(r['total_bytes'])}"
                     + (f" ({', '.join(fmt)})" if fmt else ""))
    for w in m.get("warnings", [])[:2]:
        lines.append(f"⚠️ {esc(w)}")
    if r["status"] == "partial":
        lines.append("⚠️ Скачано не полностью — /continue или кнопка «Перепроверить»")
    if m.get("password"):
        lines.append(f"Пароль архива: <code>{esc(m['password'])}</code>")
    arch = m.get("archives", [])[:8]
    if arch and not short:
        lines.append("Архивы:\n" + "\n".join(esc(a) for a in arch))
    lines.append(f"Опубликовано: {esc((m.get('published') or '')[:10])}  ·  "
                 f"<a href=\"{esc(r['url'])}\">пост на сайте</a>")
    return _fit(lines, 1000 if short else 4000)


def media_caption(r, filename=None):
    lines = [f"<b>{esc(r['title'])}</b>"]
    for label, key in (("Косплеер", "cosplayer"), ("Персонаж", "character"),
                       ("Игра/аниме", "source")):
        if r.get(key):
            lines.append(f"{label}: {esc(r[key])}")
    if filename:
        lines.append(f"Файл: <code>{esc(filename)}</code>")
    return "\n".join(lines)[:1000]


def post_files(r):
    return json.loads(r["files_json"] or "[]")


def posts_root():
    return (Path(OUT) / "posts").resolve()


def post_archive_path(r):
    root = posts_root()
    path = (root / f"{r['folder']}.zip").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Некорректный путь к архиву поста")
    return path


def is_meta(r):
    return r.get("status") == "meta"


def stage_dir(r):
    return STAGING / mp.safe_name(r["slug"])


def post_file_path(r, item):
    if is_meta(r):  # файлы не хранятся: берём из временной папки (после stage_post)
        root = STAGING.resolve()
        path = (stage_dir(r) / item["name"]).resolve()
    else:
        root = posts_root()
        path = (root / r["folder"] / item["name"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Некорректный путь к файлу поста")
    return path


STAGE_LOCKS = defaultdict(asyncio.Lock)


async def stage_post(r, names=None):
    """Режим meta: скачивает файлы поста во временную папку. В режиме full ничего не делает."""
    if is_meta(r):
        workers = jm.get_settings().get("workers", 4)
        await asyncio.to_thread(jm.scraper.stage_files, r, stage_dir(r), names, workers)


def unstage_post(r):
    if is_meta(r):
        shutil.rmtree(stage_dir(r), ignore_errors=True)


@asynccontextmanager
async def staged(r, names=None):
    """with staged(r): временно скачать файлы -> использовать -> удалить (режим meta)."""
    async with STAGE_LOCKS[r["slug"]]:
        try:
            await stage_post(r, names)
            yield
        finally:
            unstage_post(r)


@contextmanager
def open_post_file(r, item):
    if r["zipped"]:
        with zipfile.ZipFile(post_archive_path(r)) as archive:
            member = f"{r['folder']}/{item['name']}"
            try:
                source = archive.open(member)
            except KeyError as e:
                raise FileNotFoundError(f"Файл {item['name']} не найден в архиве поста") from e
            with source:
                yield source
    else:
        with post_file_path(r, item).open("rb") as source:
            yield source


def post_file_size(r, item):
    if r["zipped"]:
        with zipfile.ZipFile(post_archive_path(r)) as archive:
            return archive.getinfo(f"{r['folder']}/{item['name']}").file_size
    return post_file_path(r, item).stat().st_size


def read_post_file(r, item, limit):
    if post_file_size(r, item) > limit:
        return None
    with open_post_file(r, item) as source:
        data = source.read(limit + 1)
    return data if len(data) <= limit else None


@contextmanager
def materialize_post_file(r, item):
    if not r["zipped"]:
        yield post_file_path(r, item)
        return
    with tempfile.TemporaryDirectory(prefix="mitaku-file-") as temp_dir:
        path = Path(temp_dir) / item["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open_post_file(r, item) as source, path.open("wb") as target:
            shutil.copyfileobj(source, target)
        yield path


def create_zip_parts(r, selected_files=None):
    temp_dir = tempfile.TemporaryDirectory(prefix="mitaku-zips-")
    parts = []
    split_files = []
    files = selected_files if selected_files is not None else post_files(r)
    if is_meta(r):  # часть файлов могла не скачаться — не падаем из-за них
        files = [f for f in files if post_file_path(r, f).exists()]
    current, current_size = [], 0
    prefix = mp.safe_name(r["folder"])

    def next_part():
        return Path(temp_dir.name) / f"{prefix}_{len(parts) + 1:03d}.zip"

    def flush():
        nonlocal current, current_size
        if not current:
            return
        path = next_part()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for item in current:
                with open_post_file(r, item) as source:
                    with archive.open(item["name"], "w") as target:
                        shutil.copyfileobj(source, target)
        parts.append(path)
        current, current_size = [], 0

    for item in files:
        size = post_file_size(r, item)
        if size > ZIP_PART_SIZE:
            flush()
            part_count = 0
            with open_post_file(r, item) as source:
                while True:
                    path = next_part()
                    next_number = part_count + 1
                    part_name = f"{item['name']}.part{next_number:03d}"
                    written = 0
                    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
                        with archive.open(part_name, "w") as target:
                            while written < ZIP_PART_SIZE:
                                chunk = source.read(min(1024 * 1024, ZIP_PART_SIZE - written))
                                if not chunk:
                                    break
                                target.write(chunk)
                                written += len(chunk)
                        if written:
                            archive.writestr(
                                "README.txt",
                                "This media file is split into numbered .part files. "
                                "Extract all ZIP parts, then join the .part files in order.\n")
                    if not written:
                        path.unlink()
                        break
                    part_count = next_number
                    parts.append(path)
                    if written < ZIP_PART_SIZE:
                        break
            split_files.append((item["name"], part_count))
            continue
        if current and current_size + size > ZIP_PART_SIZE:
            flush()
        current.append(item)
        current_size += size
    flush()
    return temp_dir, parts, split_files


def load_preview(r, item, max_side=1280):
    """JPEG-превью фото поста (Pillow, если установлен). None — если не получилось."""
    if post_file_size(r, item) > 60 * MB:
        return None
    with open_post_file(r, item) as source:
        return mp.make_preview(source.read(), max_side=max_side)


def jpg_name(name):
    return Path(name).stem + ".jpg"


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def button_title(title, limit=48):
    title = re.sub(r"\s+", " ", title or "").strip() or "Без названия"
    return title if len(title) <= limit else title[:limit - 1] + "…"


def new_search(q, field):
    tok = next(_tok)
    SEARCHES[tok] = {"q": q, "field": field, "items": []}
    for old in list(SEARCHES)[:-100]:
        SEARCHES.pop(old, None)
    return tok


def parse_sync_args(args, default_last):
    """'cosplaytele cosplay-ero 3 force' -> (сайты, раздел, с, по, флаги)."""
    sites, section, first, last, flags = [], None, 1, default_last, {}
    for t in (args or "").lower().split():
        if t in ms.SITES:
            sites.append(t)
        elif re.fullmatch(r"\d+-\d+", t):
            a, _, b = t.partition("-")
            first, last = int(a), int(b)
        elif re.fullmatch(r"\d+", t):
            first, last = 1, int(t)
        elif t in ("force", "recheck"):
            flags[t] = True
        else:
            section = t
    return sites, section, first, last, flags


def section_targets(sites, section):
    """Выбранные сайты + раздел -> ([(ключ_сайта, url)], предупреждения)."""
    urls, warns = [], []
    for key in (sites or jm.get_active_sites()):
        secs = ms.SITES[key].sections()
        name = section or "all"
        if name not in secs:
            warns.append(f"{ms.SITES[key].title}: нет раздела «{name}» (есть: {', '.join(secs)})")
            continue
        urls.append((key, secs[name]))
    return urls, warns


async def delete_message(message):
    try:
        await message.delete()
    except TelegramBadRequest as e:
        logging.warning("Не удалось удалить сообщение %s: %s", message.message_id, e)
        if isinstance(message.bot, TrackingBot):
            message.bot.forget_message(message.chat.id, message.message_id)


async def queue_job(message, urls, *, source_message=None, status_text=None, **kwargs):
    status = await message.answer(status_text or "📥 Задача добавлена в очередь.")
    job = jm.submit(urls, chat_id=message.chat.id,
                    status_message_id=status.message_id, **kwargs)
    if source_message:
        await delete_message(source_message)
    return job


# --------------------------------------------------------------------------- команды
@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.answer(HELP)


async def choose_site(m, mode, edit=False):
    act = jm.get_active_sites()
    rows = [[btn(("✅ " if k in act else "") + site.title, f"sy:{mode}:{k}")]
            for k, site in ms.SITES.items()]
    rows.append([btn("🌐 Все выбранные (/sites)", f"sy:{mode}:act")])
    text = "С какого сайта " + ("парсить?" if mode == "s" else "обновить?")
    if edit:
        await m.edit_text(text, reply_markup=kb(*rows))
    else:
        await m.answer(text, reply_markup=kb(*rows))


async def start_sync(m, args, mode):
    if not (args or "").strip():
        return await choose_site(m, mode)
    sites, section, first, last, flags = parse_sync_args(args, 5 if mode == "s" else 50)
    targets, warns = section_targets(sites, section)
    if not targets:
        return await m.answer("❌ " + ("\n".join(esc(w) for w in warns) or
                                       "Не выбран ни один сайт — /sites"))
    names = ", ".join(ms.SITES[k].title for k, _ in targets)
    prefix = "Обновление" if mode == "u" else "Загрузка"
    await queue_job(m, [u for _, u in targets], first=first, last=last,
                    status_text=f"📥 {prefix}: {esc(names)}, страницы {first}–{last}."
                                + ("\n" + "\n".join(esc(w) for w in warns) if warns else ""),
                    until_known=(mode == "u"), **flags)


@router.message(Command("sync"))
async def cmd_sync(m: Message, command: CommandObject):
    await start_sync(m, command.args, "s")


@router.message(Command("update"))
async def cmd_update(m: Message, command: CommandObject):
    await start_sync(m, command.args, "u")


@router.message(Command("sites"))
async def cmd_sites(m: Message):
    await show_sites(m)


async def show_sites(m, edit=False):
    act = jm.get_active_sites()
    rows = [[btn(("✅ " if k in act else "⬜ ") + site.title, f"st:{k}")]
            for k, site in ms.SITES.items()]
    rows.append([btn("Готово", "st:done")])
    text = ("Активные сайты — их используют /sync, /update, поиск на сайте и автопубликация "
            "в канал.\n🧪 — экспериментальные адаптеры, не проверены на живом сайте.")
    if edit:
        await m.edit_text(text, reply_markup=kb(*rows))
    else:
        await m.answer(text, reply_markup=kb(*rows))


@router.message(Command("get"))
async def cmd_get(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        return await m.answer("Использование: /get &lt;ссылка на пост или slug&gt;")
    if mp.is_post_url(arg):
        url, flags = arg, {}
    else:
        rec = jm.lib.get(arg)
        if not rec:
            return await m.answer("Такого slug нет в библиотеке. Пришлите полную ссылку.")
        url, flags = rec["url"], {"recheck": True}
    await queue_job(m, [url], **flags)


@router.message(Command("pause"))
async def cmd_pause(m: Message):
    jm.pause()
    await m.answer("⏸ Пауза (текущие загрузки замрут на ближайшем куске). /resume — продолжить.")


@router.message(Command("resume"))
async def cmd_resume(m: Message):
    jm.resume()
    await m.answer("▶️ Продолжаю.")


@router.message(Command("stop"))
async def cmd_stop(m: Message):
    jm.stop()
    await m.answer("⏹ Останавливаю, очередь очищена. Прогресс сохранён — /continue продолжит.")


@router.message(Command("continue"))
async def cmd_continue(m: Message):
    status = await m.answer("📥 Продолжение добавлено в очередь.")
    job = jm.resume_last(chat_id=m.chat.id, status_message_id=status.message_id)
    if not job:
        await delete_message(status)
        return await m.answer("Незавершённых задач нет.")


@router.message(Command("status"))
async def cmd_status(m: Message):
    st = jm.status()
    p = st["progress"]
    icon = {"idle": "💤", "running": "⏳", "paused": "⏸"}.get(st["state"], "•")
    if st["state"] == "idle":
        text = f"{icon} Простаиваю. В очереди: {st['queued']}.\nПоследняя задача: {mp.summary_text(p)}"
        pend = jm.pending_job()
        if pend:
            text += f"\n⚠️ Есть незавершённая задача #{pend['id']} ({pend['state']}) — /continue"
        return await m.answer(text)
    job = st["job"]
    await m.answer(
        f"{icon} Задача #{job['id']} — {st['state']}\n"
        f"Страница: {p['page']}\nПост: {esc(p['post'])}\n"
        f"Файлы поста: {p['file_i']}/{p['file_n']}\n"
        f"Итого: {mp.summary_text(p)}\nВ очереди: {st['queued']}")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    s = jm.lib.stats()
    secs = ", ".join(f"{k}: {v}" for k, v in s["sections"].items())
    sites = ", ".join(f"{short(k)}: {v}" for k, v in s["sites"].items())
    await m.answer(f"📚 Постов: {s['posts']} ({sites})\nКосплееров: {s['cosplayers']}\n"
                   f"Хранится на диске: {mp.human_size(s['bytes'])}\nСтатусы: {s['status']}\n"
                   f"Разделы: {secs}")


@router.message(Command("list"))
async def cmd_list(m: Message, command: CommandObject):
    page = int(command.args) - 1 if (command.args or "").strip().isdigit() else 0
    tok = new_search("", None)
    await show_local(m, tok, max(page, 0), edit=False)


@router.message(Command("links"))
async def cmd_links(m: Message, command: CommandObject):
    text = mp.export_links(jm.lib, command.args or "")
    if not text.strip():
        return await m.answer("Ссылок на архивы не найдено.")
    await m.answer_document(BufferedInputFile(text.encode(), "links.txt"),
                            caption="Откройте ссылки сами (капча), итоговые Mediafire/Pixeldrain "
                                    "положите в links.txt и запустите: "
                                    "python mitaku_parser.py --archives links.txt")


@router.message(Command("set"))
async def cmd_set(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) == 2:
        try:
            v = jm.set_setting(parts[0].lower(), parts[1])
            await m.answer(f"✅ {parts[0]} = {v} (применится к следующей задаче)")
        except ValueError as e:
            await m.answer(f"❌ {esc(str(e))}")
        return
    s = jm.get_settings()
    await m.answer("⚙️ Настройки:\n" + "\n".join(f"{k} = {v}" for k, v in s.items())
                   + "\n\nИзменить: /set zip on")


# --------------------------------------------------------------------------- поиск
async def run_search(m: Message, q, field):
    q = (q or "").strip()
    if not q:
        return await m.answer("Что искать? Например: /cosplayer Byoru")
    tok = new_search(q, field)
    _, total = jm.lib.search(q, field, 1)
    if total:
        return await show_local(m, tok, 0, edit=False)
    status = await m.answer("В библиотеке ничего нет, ищу на сайте…")
    await show_site(status, tok, edit=True)


async def show_local(m, tok, page, edit):
    s = SEARCHES.get(tok)
    if not s:
        return await m.answer("Поиск устарел, повторите команду.")
    rows, total = jm.lib.search(s["q"], s["field"], PAGE, page * PAGE)
    label = s["q"] or "все посты"
    lines = [f"🔎 <b>{esc(label)}</b> — найдено {total}\n"]
    lines += [f"{i}. {fmt_row(r)}" for i, r in enumerate(rows, 1)]
    nums = [btn(button_title(r["title"]), f"i:{r['id']}:s:{tok}:{page}") for r in rows]
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"s:{tok}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(btn("▶️", f"s:{tok}:{page + 1}"))
    rows_kb = [nums[:4], nums[4:]]
    if nav:
        rows_kb.append(nav)
    if s["q"]:
        rows_kb.append([btn("🌐 Искать на сайте", f"w:{tok}:{page}")])
    markup = kb(*[r for r in rows_kb if r])
    text = "\n".join(lines)[:4000]
    if edit:
        await m.edit_text(text, reply_markup=markup)
    else:
        await m.answer(text, reply_markup=markup)


async def show_site(m, tok, edit=False, return_page=0):
    s = SEARCHES[tok]
    try:
        items = await asyncio.to_thread(jm.scraper.site_search, s["q"], None,
                                        jm.get_active_sites())
    except Exception as e:  # noqa: BLE001
        text = f"❌ Поиск на сайте не удался: {esc(str(e))}"
        markup = kb([btn("↩️ К библиотеке", f"s:{tok}:{return_page}")])
        if edit:
            return await m.edit_text(text, reply_markup=markup)
        return await m.answer(text, reply_markup=markup)
    s["items"] = items
    if not items:
        errs = "".join(f"\n⚠️ {esc(k)}: {esc(e[:150])}" for k, e in jm.scraper.search_errors)
        text = "На выбранных сайтах тоже ничего не найдено (/sites)." + errs
        markup = kb([btn("↩️ К библиотеке", f"s:{tok}:{return_page}")])
        if edit:
            return await m.edit_text(text, reply_markup=markup)
        return await m.answer(text, reply_markup=markup)
    await render_site(m, tok, 0, edit=edit, return_page=return_page)


async def render_site(m, tok, page, edit=False, return_page=0):
    s = SEARCHES.get(tok)
    if not s:
        return await m.answer("Поиск устарел, повторите команду.")
    items = s["items"]
    start = page * SITE_PAGE
    page_items = items[start:start + SITE_PAGE]
    lines = [f"🌐 На сайтах по запросу <b>{esc(s['q'])}</b> — "
             f"найдено {len(items)} (страница {page + 1}):\n"]
    buttons = []
    for i, it in enumerate(page_items, start):
        have = " ✅" if jm.lib.get(mp.slug_of(it["url"])) else ""
        lines.append(f"{i + 1}. [{short(it.get('site'))}] {esc(it['title'])}{have}")
        buttons.append(btn(button_title(f"{short(it.get('site'))} {it['title']}"),
                           f"d:{tok}:{i}:{page}:{return_page}"))
    rows_kb = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"sp:{tok}:{page - 1}:{return_page}"))
    if start + SITE_PAGE < len(items):
        nav.append(btn("▶️", f"sp:{tok}:{page + 1}:{return_page}"))
    if nav:
        rows_kb.append(nav)
    rows_kb.append([btn("↩️ К библиотеке", f"s:{tok}:{return_page}")])
    rows_kb.append([btn("⬇️ Скачать все", f"da:{tok}:{page}:{return_page}")])
    text = "\n".join(lines)[:4000]
    if edit:
        await m.edit_text(text, reply_markup=kb(*rows_kb))
    else:
        await m.answer(text, reply_markup=kb(*rows_kb))


@router.message(Command("search"))
async def cmd_search(m: Message, command: CommandObject):
    await run_search(m, command.args, None)


@router.message(Command("cosplayer"))
async def cmd_cosplayer(m: Message, command: CommandObject):
    await run_search(m, command.args, "cosplayer")


@router.message(Command("game"))
async def cmd_game(m: Message, command: CommandObject):
    await run_search(m, command.args, "game")


@router.message(Command("character"))
async def cmd_character(m: Message, command: CommandObject):
    await run_search(m, command.args, "character")


@router.message(Command("tag"))
async def cmd_tag(m: Message, command: CommandObject):
    await run_search(m, command.args, "tag")


@router.message(lambda m: m.text and extract_ouo_url(m.text))
async def handle_ouo_link(m: Message):
    url = extract_ouo_url(m.text or "")
    if not url:
        return

    status = await m.answer(f"Загружаю файл с {esc(url)}…")
    file_path = None
    try:
        file_path, filename, size = await OuoClient().download(url)
    except OuoError as e:
        await status.edit_text(f"Не удалось скачать файл:\n{esc(str(e))}")
        return
    except Exception as e:  # noqa: BLE001
        logging.exception("Неожиданная ошибка при загрузке с ouo.io")
        await status.edit_text(
            f"Неожиданная ошибка:\n{esc(type(e).__name__)}: {esc(str(e))}")
        return

    try:
        if size > MAX_UPLOAD:
            await status.edit_text(
                f"Файл скачан, но превышает настроенный лимит отправки "
                f"({MAX_UPLOAD // MB} МБ).")
            return

        await m.answer_document(
            document=FSInputFile(file_path, filename=filename),
            caption=f"Из {esc(url)}",
        )
        await status.delete()
    except Exception as e:  # noqa: BLE001
        await status.edit_text(
            f"Файл скачан ({size} байт), но Telegram не смог его отправить:\n"
            f"{esc(str(e))}")
    finally:
        if file_path:
            file_path.unlink(missing_ok=True)


@router.message(F.text & ~F.text.startswith("/"))
async def plain_text(m: Message):
    await run_search(m, m.text, None)


# --------------------------------------------------------------------------- кнопки
@router.callback_query(F.data.startswith("s:"))
async def cb_page(c: CallbackQuery):
    _, tok, page = c.data.split(":")
    if c.message.photo:
        await delete_message(c.message)
        await show_local(c.message, int(tok), int(page), edit=False)
    else:
        await show_local(c.message, int(tok), int(page), edit=True)
    await c.answer()


@router.callback_query(F.data.startswith("w:"))
async def cb_web(c: CallbackQuery):
    parts = c.data.split(":")
    tok = int(parts[1])
    return_page = int(parts[2]) if len(parts) > 2 else 0
    if tok not in SEARCHES:
        return await c.answer("Поиск устарел", show_alert=True)
    await c.answer("Ищу на сайте…")
    await show_site(c.message, tok, edit=True, return_page=return_page)


@router.callback_query(F.data.startswith("sp:"))
async def cb_site_page(c: CallbackQuery):
    parts = c.data.split(":")
    tok, page = int(parts[1]), int(parts[2])
    return_page = int(parts[3]) if len(parts) > 3 else 0
    await render_site(c.message, tok, page, edit=True, return_page=return_page)
    await c.answer()


@router.callback_query(F.data.startswith("i:"))
async def cb_info(c: CallbackQuery):
    parts = c.data.split(":", 3)
    r = jm.lib.get_by_id(int(parts[1]))
    back_data = parts[2] + ":" + parts[3] if len(parts) > 3 else (
        parts[2] if len(parts) > 2 else None)
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    await c.answer()
    files = post_files(r)
    buttons = [
        btn("🔁 Перепроверить",
            f"r:{r['id']}:{back_data}" if back_data else f"r:{r['id']}"),
        btn("📄 meta.json",
            f"j:{r['id']}:{back_data}" if back_data else f"j:{r['id']}"),
    ]
    if files:
        return_card = f"i:{r['id']}:{back_data}" if back_data else f"i:{r['id']}"
        buttons.extend([
            btn("📦 Скачать ZIP", f"pz:{r['id']}"),
            btn("🖼 Выбрать файл", f"pf:{r['id']}:0:{return_card}"),
        ])
    if json.loads(r["meta_json"] or "{}").get("archives"):
        buttons.append(btn("🔗 Архивы", f"a:{r['id']}"))
    if CHANNEL_ID and files:
        buttons.append(btn("📣 В канал", f"pub:{r['id']}"))
    if back_data:
        buttons.append(btn("↩️ Назад к результатам", back_data))
    markup = kb(buttons)
    details = card(r)

    preview = next((f for f in files if f["kind"] == "photo"), None)
    if preview:
        data = None
        try:
            async with staged(r, {preview["name"]}):
                data = await asyncio.to_thread(load_preview, r, preview)
        except (OSError, zipfile.BadZipFile) as e:
            logging.warning("Не удалось получить превью поста %s: %s", r["slug"], e)
        if data:
            await c.message.answer_photo(
                BufferedInputFile(data, jpg_name(preview["name"])),
                caption=card(r, short=True), reply_markup=markup)
            await delete_message(c.message)
            return
        details += "\n\nПревью недоступно: фото не удалось получить или оно слишком большое."
    elif files:
        details += "\n\nПревью недоступно: в посте нет фотографий."

    if c.message.text is None:
        await c.message.answer(details, reply_markup=markup)
        await delete_message(c.message)
    else:
        await c.message.edit_text(details, reply_markup=markup)


@router.callback_query(F.data.startswith("a:"))
async def cb_archives(c: CallbackQuery):
    r = jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    m = json.loads(r["meta_json"] or "{}")
    links = m.get("archives", [])
    if not links:
        return await c.answer("Ссылок на архивы нет", show_alert=True)
    text = "🔗 Архивы:\n" + "\n".join(esc(x) for x in links[:20])
    if m.get("password"):
        text += f"\nПароль: <code>{esc(m['password'])}</code>"
    await c.message.answer(text)
    await c.answer()


@router.callback_query(F.data.startswith("j:"))
async def cb_json(c: CallbackQuery):
    parts = c.data.split(":", 2)
    r = jm.lib.get_by_id(int(parts[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    back_data = parts[2] if len(parts) > 2 else None
    data = json.dumps(json.loads(r["meta_json"]), ensure_ascii=False, indent=2).encode()
    markup = kb([btn("↩️ К карточке", f"i:{r['id']}:{back_data}"
                     if back_data else f"i:{r['id']}")])
    await c.message.answer_document(
        BufferedInputFile(data, f"{r['slug']}.json"), reply_markup=markup)
    await delete_message(c.message)
    await c.answer()


@router.callback_query(F.data.startswith("r:"))
async def cb_recheck(c: CallbackQuery):
    parts = c.data.split(":", 2)
    r = jm.lib.get_by_id(int(parts[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    back_data = f"i:{r['id']}:{parts[2]}" if len(parts) > 2 else f"i:{r['id']}"
    await c.answer()
    await queue_job(c.message, [r["url"]], source_message=c.message,
                    back_data=back_data, recheck=True)


@router.callback_query(F.data.startswith("da:"))
async def cb_download_all(c: CallbackQuery):
    parts = c.data.split(":")
    tok = int(parts[1])
    site_page = int(parts[2]) if len(parts) > 2 else 0
    return_page = int(parts[3]) if len(parts) > 3 else 0
    s = SEARCHES.get(tok)
    if not s or not s["items"]:
        return await c.answer("Поиск устарел", show_alert=True)
    await c.answer()
    await queue_job(
        c.message, [it["url"] for it in s["items"]], source_message=c.message,
        status_text=f"📥 В очереди постов: {len(s['items'])}.",
        back_data=f"sp:{tok}:{site_page}:{return_page}")


@router.callback_query(F.data.startswith("d:"))
async def cb_download(c: CallbackQuery):
    parts = c.data.split(":")
    tok, idx = int(parts[1]), int(parts[2])
    site_page = int(parts[3]) if len(parts) > 3 else idx // SITE_PAGE
    return_page = int(parts[4]) if len(parts) > 4 else 0
    s = SEARCHES.get(tok)
    if not s or idx >= len(s["items"]):
        return await c.answer("Поиск устарел", show_alert=True)
    url = s["items"][idx]["url"]
    await c.answer()
    await queue_job(c.message, [url], source_message=c.message,
                    result_slug=mp.slug_of(url),
                    back_data=f"sp:{tok}:{site_page}:{return_page}")


async def show_download_result(bot, chat_id, rec, partial=False, back_data=None):
    files = post_files(rec)
    preview = next((f for f in files if f["kind"] == "photo"), None)
    if files:
        return_card = f"i:{rec['id']}:{back_data}" if back_data else f"i:{rec['id']}"
        rows = [[btn("📦 Скачать ZIP", f"pz:{rec['id']}"),
                 btn("🖼 Выбрать файл", f"pf:{rec['id']}:0:{return_card}")]]
        if back_data:
            rows.append([btn("↩️ К результатам", back_data)])
        markup = kb(*rows)
    else:
        markup = kb([btn("↩️ К результатам", back_data)]) if back_data else None
    expected = sum(sum(formats.values()) for formats in
                   json.loads(rec["meta_json"] or "{}").get("formats", {}).values())
    warning = (f"\n\n⚠️ Скачано частично: {len(files)} из {expected} файлов. "
               "Можно забрать уже загруженные файлы.") if partial else ""
    if preview:
        data = None
        try:
            async with staged(rec, {preview["name"]}):
                data = await asyncio.to_thread(load_preview, rec, preview)
        except (OSError, zipfile.BadZipFile) as e:
            logging.warning("Не удалось получить превью поста %s: %s", rec["slug"], e)
        if data:
            await bot.send_photo(
                chat_id,
                BufferedInputFile(data, jpg_name(preview["name"])),
                caption=(media_caption(rec) + warning)[:1000], reply_markup=markup)
            return
    await bot.send_message(
        chat_id,
        card(rec) + warning
        + ("\n\nПревью недоступно (нет фото или оно превышает лимит Telegram)."
           if files else "\n\nСкачанных медиафайлов нет."),
        reply_markup=markup)


async def show_post_files(message, rec, page, edit=False, return_card=None):
    files = post_files(rec)
    return_card = return_card or f"i:{rec['id']}"
    start = page * FILE_PAGE
    page_files = files[start:start + FILE_PAGE]
    lines = [f"<b>{esc(rec['title'])}</b> — файлы {start + 1}–"
             f"{min(start + len(page_files), len(files))} из {len(files)}:"]
    buttons = []
    for index, item in enumerate(page_files, start):
        label = f"{index + 1}. {item['name']}"
        buttons.append(btn(label[:60], f"mf:{rec['id']}:{index}"))
        lines.append(f"{index + 1}. {esc(item['name'])}"
                     + (f" ({mp.human_size(item['size_bytes'])})" if item.get("size_bytes") else ""))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"pf:{rec['id']}:{page - 1}:{return_card}"))
    if start + FILE_PAGE < len(files):
        nav.append(btn("▶️", f"pf:{rec['id']}:{page + 1}:{return_card}"))
    if nav:
        rows.append(nav)
    rows.append([btn("📦 Скачать ZIP", f"pz:{rec['id']}")])
    rows.append([btn("↩️ К карточке", return_card)])
    if edit:
        edit_method = message.edit_caption if message.photo else message.edit_text
        await edit_method("\n".join(lines), reply_markup=kb(*rows))
    else:
        await message.answer("\n".join(lines), reply_markup=kb(*rows))


async def send_zip_parts(message, rec, selected_files=None):
    names = {f["name"] for f in selected_files} if selected_files is not None else None
    status = None
    if is_meta(rec):
        status = await message.answer("⏳ Скачиваю файлы с сайта…")
    async with staged(rec, names):
        if status:
            await delete_message(status)
        temp_dir, parts, split_files = await asyncio.to_thread(
            create_zip_parts, rec, selected_files)
        try:
            for index, path in enumerate(parts, 1):
                await tg_call(message.answer_document, FSInputFile(path),
                              caption=f"{esc(rec['title'])} — архив {index}/{len(parts)}")
            if split_files:
                notes = []
                for name, count in split_files:
                    chunks = "+".join(f'"{name}.part{i:03d}"' for i in range(1, count + 1))
                    notes.append(f"{name}: <code>copy /b {chunks} \"{name}\"</code>")
                await message.answer(
                    "Файлы больше лимита Telegram разделены на части внутри ZIP. "
                    "Распакуйте все архивы в одну папку и выполните команды:\n"
                    + "\n".join(notes))
            if not parts:
                await message.answer("Не удалось получить файлы поста (сайт недоступен?).")
        finally:
            temp_dir.cleanup()


@router.callback_query(F.data.startswith("pz:"))
async def cb_post_zip(c: CallbackQuery):
    rec = jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not rec:
        return await c.answer("Пост не найден", show_alert=True)
    await c.answer("Готовлю ZIP-архивы…")
    await send_zip_parts(c.message, rec)


@router.callback_query(F.data.startswith("pf:"))
async def cb_post_files(c: CallbackQuery):
    parts = c.data.split(":", 3)
    record_id, page = int(parts[1]), int(parts[2])
    return_card = parts[3] if len(parts) > 3 else None
    rec = jm.lib.get_by_id(int(record_id))
    if not rec:
        return await c.answer("Пост не найден", show_alert=True)
    await c.answer()
    await show_post_files(c.message, rec, page, edit=True, return_card=return_card)


@router.callback_query(F.data.startswith("mf:"))
async def cb_post_file(c: CallbackQuery):
    _, record_id, index = c.data.split(":")
    rec = jm.lib.get_by_id(int(record_id))
    if not rec:
        return await c.answer("Пост не найден", show_alert=True)
    files = post_files(rec)
    index = int(index)
    if index >= len(files):
        return await c.answer("Файл не найден", show_alert=True)
    item = files[index]
    await c.answer("Готовлю файл…" if is_meta(rec) else "Отправляю файл…")
    async with staged(rec, {item["name"]}):
        try:
            size = await asyncio.to_thread(post_file_size, rec, item)
        except OSError:
            return await c.message.answer("Не удалось скачать файл с сайта.")
        if size > ZIP_PART_SIZE:
            await send_zip_parts(c.message, rec, [item])
            return
        with materialize_post_file(rec, item) as path:
            upload = FSInputFile(path, filename=item["name"])
            caption = media_caption(rec, item["name"])
            if item["kind"] == "photo" and size <= 9 * MB:
                await tg_call(c.message.answer_photo, upload, caption=caption)
            elif item["kind"] == "video":
                await tg_call(c.message.answer_video, upload, caption=caption)
            else:
                await tg_call(c.message.answer_document, upload, caption=caption)


# --------------------------------------------------------------------------- Telegram: повторы при лимитах
async def tg_call(fn, *args, **kwargs):
    for _ in range(8):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            logging.warning("Лимит Telegram, жду %s с", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
    raise RuntimeError("слишком много повторов из-за лимитов Telegram")


async def notify_admins(text):
    for admin in ADMINS:
        try:
            await BOT.send_message(admin, text)
        except Exception as e:  # noqa: BLE001
            logging.warning("Не удалось написать админу %s: %s", admin, e)


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
    database_path = Path(OUT) / "library.db"
    backup_path = Path(OUT) / "library.db.backup"
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
    backup_path = Path(OUT) / "library.db.backup"
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
    for admin in ADMINS:
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
        await asyncio.sleep(BACKUP_INTERVAL)


# --------------------------------------------------------------------------- выбор сайта кнопками
async def ask_pages(m, mode, site, section):
    if mode == "u":
        return await run_sync_choice(m, mode, site, section, 50)
    title = "выбранные сайты" if site == "act" else ms.SITES[site].title
    rows = [[btn(str(n), f"sg:{mode}:{site}:{section}:{n}") for n in PAGES_CHOICES],
            [btn("↩️ Назад", f"sb:{mode}")]]
    await m.edit_text(f"{esc(title)}: сколько страниц брать?", reply_markup=kb(*rows))


async def run_sync_choice(m, mode, site, section, n):
    sites = [] if site == "act" else [site]
    targets, warns = section_targets(sites, None if section == "all" else section)
    if not targets:
        return await m.edit_text("Не выбран ни один сайт — /sites")
    names = ", ".join(ms.SITES[k].title for k, _ in targets)
    prefix = "Обновление" if mode == "u" else "Загрузка"
    await queue_job(m, [u for _, u in targets], source_message=m, first=1, last=n,
                    until_known=(mode == "u"),
                    status_text=f"📥 {prefix}: {esc(names)}, до {n} стр.")


@router.callback_query(F.data.startswith("sy:"))
async def cb_sync_site(c: CallbackQuery):
    _, mode, site = c.data.split(":")
    await c.answer()
    if site != "act":
        secs = list(ms.SITES[site].sections())
        if len(secs) > 1:
            rows = [[btn(n, f"sc:{mode}:{site}:{n}") for n in secs[i:i + 2]]
                    for i in range(0, len(secs), 2)]
            rows.append([btn("↩️ Назад", f"sb:{mode}")])
            return await c.message.edit_text(f"{esc(ms.SITES[site].title)}: какой раздел?",
                                             reply_markup=kb(*rows))
    await ask_pages(c.message, mode, site, "all")


@router.callback_query(F.data.startswith("sc:"))
async def cb_sync_section(c: CallbackQuery):
    _, mode, site, section = c.data.split(":", 3)
    await c.answer()
    await ask_pages(c.message, mode, site, section)


@router.callback_query(F.data.startswith("sb:"))
async def cb_sync_back(c: CallbackQuery):
    await c.answer()
    await choose_site(c.message, c.data.split(":")[1], edit=True)


@router.callback_query(F.data.startswith("sg:"))
async def cb_sync_go(c: CallbackQuery):
    _, mode, site, section, n = c.data.split(":")
    await c.answer()
    await run_sync_choice(c.message, mode, site, section, int(n))


@router.callback_query(F.data.startswith("st:"))
async def cb_site_toggle(c: CallbackQuery):
    key = c.data.split(":")[1]
    if key == "done":
        await delete_message(c.message)
        return await c.answer()
    jm.toggle_site(key)
    await c.answer()
    await show_sites(c.message, edit=True)


# --------------------------------------------------------------------------- канал и комментарии
channel_router = Router()   # без фильтра по админам: автопересылки приходят от Telegram, не от человека
AUTO_FWD: dict = {}         # id поста в канале -> (id группы обсуждения, id сообщения-копии)
PUBLISH_Q: asyncio.Queue = None


@channel_router.message(F.is_automatic_forward)
async def on_auto_forward(m: Message):
    origin = getattr(m, "forward_origin", None)
    ch_msg = getattr(origin, "message_id", None) or getattr(m, "forward_from_message_id", None)
    if ch_msg:
        AUTO_FWD[ch_msg] = (m.chat.id, m.message_id)
        while len(AUTO_FWD) > 500:
            AUTO_FWD.pop(next(iter(AUTO_FWD)))


async def wait_forward(channel_msg_id, timeout=90):
    for _ in range(timeout):
        if channel_msg_id in AUTO_FWD:
            return AUTO_FWD.pop(channel_msg_id)
        await asyncio.sleep(1)
    return None


def channel_text(rec):
    """Текст поста в канале: название, модель, персонаж, игра, счётчики, теги, источник."""
    m = json.loads(rec["meta_json"] or "{}")
    fm = m.get("formats", {})
    n_ph, n_vid = sum(fm.get("photo", {}).values()), sum(fm.get("video", {}).values())
    site = ms.SITES.get(rec.get("site"))
    lines = [f"<b>{esc(rec['title'])}</b>", ""]
    for icon, label, key in (("👤", "Модель", "cosplayer"), ("🎭", "Персонаж", "character"),
                             ("🎮", "Игра/аниме", "source")):
        if m.get(key):
            lines.append(f"{icon} <b>{label}:</b> {esc(m[key])}")
    counts = ([f"🖼 {n_ph} фото"] if n_ph else []) + ([f"🎬 {n_vid} видео"] if n_vid else [])
    if counts:
        lines.append(" · ".join(counts))
    size = m.get("declared", {}).get("file_size")
    if size:
        lines.append(f"💾 {esc(size)}")
    tags = [re.sub(r"\W+", "_", t).strip("_") for t in m.get("tags", [])[:8]]
    tags = ["#" + t for t in tags if t]
    if tags:
        lines.append(" ".join(tags))
    lines += ["", f"🔗 <a href=\"{esc(rec['url'])}\">{esc(site.title if site else 'Источник')}</a>",
              "⬇️ Фото, видео и архивы — в комментариях"]
    return _fit(lines, 1000)


def photo_bytes(rec, item):
    """Фото для альбома: JPEG, который Telegram примет (≤10 МБ, разумные размеры)."""
    for side, quality in ((2560, 90), (1920, 85), (1280, 80)):
        with open_post_file(rec, item) as source:
            out = mp.make_preview(source.read(), max_side=side, quality=quality)
        if out and len(out) <= 9.5 * MB:
            return out
    return None


async def send_parts(chat_id, rp, rec, selected, label):
    """ZIP-части (≤ лимита Telegram) ответами в комментарии."""
    temp_dir, parts, split_files = await asyncio.to_thread(create_zip_parts, rec, selected)
    try:
        for i, path in enumerate(parts, 1):
            await tg_call(BOT.send_document, chat_id=chat_id, document=FSInputFile(path),
                          caption=f"{esc(rec['title'])} — {label} {i}/{len(parts)}",
                          reply_parameters=rp)
            await asyncio.sleep(SEND_PAUSE)
        if split_files:
            notes = []
            for name, count in split_files:
                chunks = "+".join(f'"{name}.part{i:03d}"' for i in range(1, count + 1))
                notes.append(f"{name}: <code>copy /b {chunks} \"{name}\"</code>")
            await tg_call(BOT.send_message, chat_id=chat_id, reply_parameters=rp,
                          text="Большие файлы разделены на части внутри ZIP. Распакуйте всё в одну "
                               "папку и объедините:\n" + "\n".join(notes))
    finally:
        temp_dir.cleanup()
    return len(parts)


async def send_comments(group_id, reply_to, rec, settings):
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
    files = [f for f in post_files(rec) if post_file_path(rec, f).exists()]
    photos = [f for f in files if f["kind"] == "photo"]
    videos = [f for f in files if f["kind"] == "video"]
    stats = {"photos": 0, "videos": 0, "zips": 0}

    if photos and settings.get("channel_media") == "documents":
        for i in range(0, len(photos), 10):
            chunk = photos[i:i + 10]
            media = [InputMediaDocument(media=FSInputFile(post_file_path(rec, f), filename=f["name"]))
                     for f in chunk]
            if len(media) == 1:
                await tg_call(BOT.send_document, chat_id=group_id, document=media[0].media,
                              reply_parameters=rp)
            else:
                await tg_call(BOT.send_media_group, chat_id=group_id, media=media,
                              reply_parameters=rp)
            stats["photos"] += len(chunk)
            await asyncio.sleep(SEND_PAUSE)
    elif photos:
        for i in range(0, len(photos), 10):
            media, too_big = [], []
            for f in photos[i:i + 10]:
                data = await asyncio.to_thread(photo_bytes, rec, f)
                if data:
                    media.append(InputMediaPhoto(media=BufferedInputFile(data, jpg_name(f["name"]))))
                else:
                    too_big.append(f)
            if len(media) == 1:
                await tg_call(BOT.send_photo, chat_id=group_id, photo=media[0].media,
                              reply_parameters=rp)
            elif media:
                await tg_call(BOT.send_media_group, chat_id=group_id, media=media,
                              reply_parameters=rp)
            stats["photos"] += len(media)
            for f in too_big:  # не удалось сжать до лимита — отправляем файлом
                await tg_call(BOT.send_document, chat_id=group_id, reply_parameters=rp,
                              document=FSInputFile(post_file_path(rec, f), filename=f["name"]))
                stats["photos"] += 1
            await asyncio.sleep(SEND_PAUSE)

    if settings.get("channel_videos"):
        for f in videos:
            path = post_file_path(rec, f)
            if path.stat().st_size <= MAX_UPLOAD:
                await tg_call(BOT.send_video, chat_id=group_id, reply_parameters=rp,
                              video=FSInputFile(path, filename=f["name"]),
                              caption=esc(f["name"]), supports_streaming=True)
                stats["videos"] += 1
                await asyncio.sleep(SEND_PAUSE)
            else:  # видео больше лимита: ZIP-частями
                stats["zips"] += await send_parts(group_id, rp, rec, [f], f["name"])
                stats["videos"] += 1

    if settings.get("channel_zip") and files:
        stats["zips"] += await send_parts(group_id, rp, rec, None, "архив")
    return stats


async def publish_post(slug, force=False):
    """Пост в канал + фото/видео/ZIP в комментарии. Возвращает текст-итог."""
    rec = jm.lib.get(slug)
    if not rec:
        raise RuntimeError("пост не найден в библиотеке")
    if not CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID не задан")
    if not force and jm.lib.kv_get(f"published:{slug}"):
        return "уже опубликован"
    settings = jm.get_settings()
    chat = await BOT.get_chat(CHANNEL_ID)
    group_id = getattr(chat, "linked_chat_id", None)
    preview_item = next((f for f in post_files(rec) if f["kind"] == "photo"), None)

    async with staged(rec):  # режим meta: на время публикации скачиваем файлы, потом удаляем
        prev = None
        if preview_item and post_file_path(rec, preview_item).exists():
            prev = await asyncio.to_thread(load_preview, rec, preview_item)
        caption = channel_text(rec)
        if prev:
            msg = await tg_call(BOT.send_photo, chat_id=chat.id,
                                photo=BufferedInputFile(prev, "preview.jpg"), caption=caption)
        else:
            msg = await tg_call(BOT.send_message, chat_id=chat.id, text=caption)
        jm.lib.kv_set(f"published:{slug}", str(msg.message_id))
        if not group_id:
            return "пост в канале, но у канала нет группы обсуждения — комментарии пропущены"
        fwd = await wait_forward(msg.message_id)
        if not fwd:
            return ("пост в канале, но копия в группе обсуждения не появилась — бот должен быть "
                    "администратором группы (или отключите Privacy mode у @BotFather)")
        stats = await send_comments(fwd[0], fwd[1], rec, settings)
    return f"фото {stats['photos']}, видео {stats['videos']}, ZIP {stats['zips']}"


async def publisher():
    while True:
        slug, force = await PUBLISH_Q.get()
        try:
            result = await publish_post(slug, force)
            rec = jm.lib.get(slug)
            await notify_admins(f"📣 {esc(rec['title'] if rec else slug)}: {esc(result)}")
        except Exception as e:  # noqa: BLE001
            logging.exception("Публикация %s", slug)
            await notify_admins(f"❌ Публикация {esc(slug)} не удалась: {esc(str(e))}")
        await asyncio.sleep(2)


async def watch_tick():
    """Один проход: для каждого активного сайта — проверка первых страниц на новые посты."""
    if jm.status()["state"] != "idle" or jm.q.qsize():
        return False
    admin = sorted(ADMINS)[0]
    for key in jm.get_active_sites():
        baseline = jm.lib.kv_get(f"baseline:{key}") == "1"
        # Первая проверка сайта только запоминает посты (baseline), ничего не публикуя.
        jm.submit([ms.SITES[key].sections()["all"]], first=1, last=3, chat_id=admin,
                  silent=True, until_known=True, publish=baseline, extra={"baseline_site": key})
    return True


async def watcher():
    last = 0.0
    while True:
        await asyncio.sleep(30)
        s = jm.get_settings()
        if CHANNEL_ID and s["channel"] and time.time() - last >= s["channel_interval"] * 60:
            if await watch_tick():
                last = time.time()


async def channel_status():
    if not CHANNEL_ID:
        return "CHANNEL_ID не задан в .env — публикация в канал отключена.", None
    s = jm.get_settings()
    try:
        chat = await BOT.get_chat(CHANNEL_ID)
    except Exception as e:  # noqa: BLE001
        return (f"❌ Не могу открыть канал {esc(CHANNEL_ID)}: {esc(str(e))}\n"
                "Бот должен быть администратором канала."), None
    linked = getattr(chat, "linked_chat_id", None)
    sites = ", ".join(ms.SITES[k].title for k in jm.get_active_sites()) or "—"
    text = (f"📣 <b>Канал:</b> {esc(chat.title or str(chat.id))}\n"
            f"Комментарии: {'✅ группа обсуждения подключена' if linked else '❌ у канала нет группы обсуждения'}\n"
            f"Автопубликация: {'✅ вкл' if s['channel'] else '⬜ выкл'}, проверка каждые {s['channel_interval']} мин\n"
            f"Сайты: {esc(sites)}\n"
            f"В очереди на публикацию: {PUBLISH_Q.qsize()}\n"
            f"Комментарии: фото — {s['channel_media']}, видео — {'да' if s['channel_videos'] else 'нет'}, "
            f"ZIP — {'да' if s['channel_zip'] else 'нет'}")
    markup = kb([btn("⏹ Выключить" if s["channel"] else "▶️ Включить",
                     "ch:off" if s["channel"] else "ch:on")],
                [btn("🔍 Проверить сейчас", "ch:now"), btn("🧪 Тест", "ch:test")])
    return text, markup


@router.message(Command("channel"))
async def cmd_channel(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg in ("on", "off"):
        jm.set_setting("channel", arg)
    text, markup = await channel_status()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("ch:"))
async def cb_channel(c: CallbackQuery):
    action = c.data.split(":")[1]
    if action in ("on", "off"):
        jm.set_setting("channel", action)
        await c.answer("Включено: первая проверка только запомнит текущие посты" if action == "on"
                       else "Выключено", show_alert=action == "on")
    elif action == "now":
        started = await watch_tick()
        await c.answer("Проверка запущена" if started else "Бот занят задачей, попробуйте позже",
                       show_alert=not started)
    elif action == "test":
        try:
            msg = await BOT.send_message(CHANNEL_ID, "🧪 Тест: бот подключён к каналу")
            await asyncio.sleep(2)
            await msg.delete()
            await c.answer("Сообщение отправлено и удалено ✅", show_alert=True)
        except Exception as e:  # noqa: BLE001
            await c.answer(f"Не вышло: {e}"[:190], show_alert=True)
    text, markup = await channel_status()
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


@router.message(Command("publish"))
async def cmd_publish(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not CHANNEL_ID:
        return await m.answer("CHANNEL_ID не задан в .env")
    if not arg:
        return await m.answer("Использование: /publish &lt;slug или ссылка на пост&gt;")
    is_url = mp.is_post_url(arg)
    slug = mp.slug_of(arg) if is_url else arg
    if jm.lib.get(slug):
        PUBLISH_Q.put_nowait((slug, True))
        return await m.answer("📣 Пост поставлен в очередь публикации.")
    if is_url:
        return await queue_job(m, [arg], publish=True,
                               status_text="📥 Поста нет в библиотеке — добавляю и публикую.")
    await m.answer("Такого slug нет в библиотеке. Пришлите полную ссылку на пост.")


@router.callback_query(F.data.startswith("pub:"))
async def cb_publish(c: CallbackQuery):
    r = jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Пост не найден", show_alert=True)
    PUBLISH_Q.put_nowait((r["slug"], True))
    await c.answer("📣 В очереди публикации", show_alert=False)


# --------------------------------------------------------------------------- запуск
async def main():
    global jm, BOT, PUBLISH_Q
    if not TOKEN:
        sys.exit("Задайте BOT_TOKEN (переменные окружения или файл .env)")
    if "--logout" in sys.argv:  # разовый выход с облачного api.telegram.org (перед локальным сервером)
        cloud = Bot(TOKEN)
        print("logOut:", await cloud.log_out())
        await cloud.session.close()
        return
    if not ADMINS:
        sys.exit("Задайте ADMIN_IDS (Telegram ID через запятую)")
    logging.basicConfig(level=logging.INFO)
    if not DATABASE_URL:
        restore_database()
    api = (TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True) if LOCAL_API_URL
           else None)
    session = (AiohttpSession(api=api, timeout=UPLOAD_TIMEOUT) if api
               else AiohttpSession(timeout=UPLOAD_TIMEOUT))
    bot = TrackingBot(TOKEN, session=session,
                      default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                   link_preview_is_disabled=True))
    BOT = bot
    PUBLISH_Q = asyncio.Queue()
    shutil.rmtree(STAGING, ignore_errors=True)  # остатки прошлого запуска
    loop = asyncio.get_running_loop()

    async def update_job_message(job, text=None, delete=False, reply_markup=None):
        message_id = job.get("status_message_id")
        if not message_id:
            if text:
                await bot.send_message(
                    job["chat_id"], text, reply_markup=reply_markup)
            return
        try:
            if delete:
                await bot.delete_message(job["chat_id"], message_id)
            elif text:
                await bot.edit_message_text(
                    text, chat_id=job["chat_id"], message_id=message_id,
                    reply_markup=reply_markup)
        except TelegramBadRequest as e:
            logging.warning("Не удалось обновить сообщение задачи #%s: %s", job["id"], e)
            if delete:
                bot.forget_message(job["chat_id"], message_id)
            if text and not delete:
                status = await bot.send_message(
                    job["chat_id"], text, reply_markup=reply_markup)
                job["status_message_id"] = status.message_id

    async def send_job_event(job, kind, text):
        if kind == "started":
            await update_job_message(
                job, f"⏳ Задача #{job['id']} выполняется. Статус — /status")
            return
        if kind != "finished" or not job.get("result_slug"):
            markup = (kb([btn("↩️ Назад", job["back_data"])])
                      if job.get("back_data") else None)
            await update_job_message(job, text, reply_markup=markup)
            return
        if not job.get("result_ready"):
            markup = (kb([btn("↩️ Назад", job["back_data"])])
                      if job.get("back_data") else None)
            await update_job_message(job, text, reply_markup=markup)
            return
        rec = jm.lib.get(job["result_slug"])
        if not rec:
            await update_job_message(
                job, "Не удалось сохранить пост в библиотеку — ZIP и файлы недоступны.")
            return
        files = post_files(rec)
        if not files and rec["status"] != "done":
            await update_job_message(
                job, "Пост не удалось скачать: в библиотеке нет доступных медиафайлов.")
            return
        await show_download_result(
            bot, job["chat_id"], rec, partial=rec["status"] != "done",
            back_data=job.get("back_data"))
        await update_job_message(job, delete=True)

    def on_event(job, kind, text):  # вызывается из рабочего потока
        if kind == "post":  # пост только что сохранён в библиотеку
            if job.get("flags", {}).get("publish") and text.startswith("new:"):
                loop.call_soon_threadsafe(PUBLISH_Q.put_nowait, (text.split(":", 1)[1], False))
            return
        if kind == "finished" and job.get("baseline_site"):  # первая проверка сайта завершена
            jm.lib.kv_set(f"baseline:{job['baseline_site']}", "1")
        if job.get("silent") and kind in ("started", "finished"):
            return
        if not job.get("chat_id"):
            return
        future = asyncio.run_coroutine_threadsafe(send_job_event(job, kind, text), loop)

        def report_error(done):
            try:
                done.result()
            except Exception:
                logging.exception("Не удалось отправить уведомление о задаче #%s", job["id"])

        future.add_done_callback(report_error)

    jm = JobManager(OUT, on_event)
    dp = Dispatcher()
    dp.include_router(channel_router)
    dp.include_router(router)
    asyncio.create_task(publisher())
    asyncio.create_task(watcher())
    await bot.set_my_commands([
        BotCommand(command="sites", description="Выбрать сайты"),
        BotCommand(command="sync", description="Парсить сайт (кнопками)"),
        BotCommand(command="update", description="Только новое"),
        BotCommand(command="channel", description="Публикация в канал"),
        BotCommand(command="publish", description="Опубликовать пост в канал"),
        BotCommand(command="get", description="Добавить один пост"),
        BotCommand(command="pause", description="Пауза"),
        BotCommand(command="resume", description="Продолжить"),
        BotCommand(command="stop", description="Остановить"),
        BotCommand(command="continue", description="Продолжить незавершённую задачу"),
        BotCommand(command="status", description="Прогресс"),
        BotCommand(command="search", description="Поиск"),
        BotCommand(command="cosplayer", description="Поиск по нику"),
        BotCommand(command="game", description="Поиск по игре/аниме"),
        BotCommand(command="list", description="Последние посты"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="links", description="Ссылки на архивы"),
        BotCommand(command="set", description="Настройки"),
    ])
    if jm.pending_job():
        for admin in ADMINS:
            try:
                await bot.send_message(admin, "⚠️ Есть незавершённая задача — /continue")
            except Exception:  # noqa: BLE001
                pass
    health_server = await asyncio.start_server(
        health_check, "0.0.0.0", HEALTH_PORT)
    logging.info("Health endpoint слушает 0.0.0.0:%s/health", HEALTH_PORT)
    backup_task = (asyncio.create_task(daily_backup(bot, jm.lib))
                   if not jm.lib.postgres else None)
    try:
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        if backup_task:
            backup_task.cancel()
            try:
                await backup_task
            except asyncio.CancelledError:
                pass
            try:
                await backup_database(bot, jm.lib)
            except Exception:
                logging.exception("Не удалось создать резервную копию при завершении")
        health_server.close()
        await health_server.wait_closed()
        if jm.lib.postgres:
            jm.lib.db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
