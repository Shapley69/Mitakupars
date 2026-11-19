#!/usr/bin/env python3
"""
mitaku_parser.py — парсер mitaku.net (18+): фото/видео -> папки или zip,
JSON-метаданные по каждому посту, инкрементальные запуски, поиск по библиотеке.

Модуль можно использовать и как программу (CLI), и как библиотеку
(см. README.md: классы Scraper, Library, Options, Control).

  pip install requests beautifulsoup4

Примеры CLI:
  python mitaku_parser.py https://mitaku.net/ --pages 5            # первые 5 страниц
  python mitaku_parser.py https://mitaku.net/ --pages 5 --zip      # то же, но в zip
  python mitaku_parser.py https://mitaku.net/ --pages 50 --until-known   # только новое
  python mitaku_parser.py --search "hana bunny"                    # поиск по библиотеке
  python mitaku_parser.py --search nikke --field game
  python mitaku_parser.py --site-search "byoru"                    # поиск на самом сайте
  python mitaku_parser.py --stats
  python mitaku_parser.py --export-links links_todo.txt            # ссылки на архивы
  python mitaku_parser.py --archives links.txt                     # скачать архивы
"""
import argparse
import hashlib
import html as htmllib
import io
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
import dedupe
import subs
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SITE = "https://mitaku.net"
SITE_HOST = "mitaku.net"  # можно переопределить (тесты, зеркала)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

VID_EXT = {"mp4", "webm", "mov", "m4v", "mkv"}
STREAM_EXT = {"m3u8", "mpd"}   # HLS/DASH: это не файл, а плейлист — нужен загрузчик с ffmpeg
# Видео-плееры и стриминговые хосты: ссылка на них — это видео, а не архив. Сами сайты не отдают
# файл (видео лежит в iframe-плеере), поэтому в meta это поле video_embeds.
VIDEO_EMBED_HOSTS = ("bysesayeveum.com", "cossora.stream", "streamtape.", "doodstream.", "dood.",
                     "filemoon.", "streamwish.", "voe.sx", "mixdrop.", "vidhide", "lulustream",
                     "upstream.to", "vidoza.", "streamsb.", "youtube.com", "youtu.be", "vimeo.com")
_EXT = "jpe?g|png|gif|webp|avif|mp4|webm|mov|m4v|mkv"
_TAIL = r"(?:\?[^\s\"'<>()\\]*)?"
# Ищем ссылки на медиа в сыром HTML: так находятся и lazy-load (data-src),
# и srcset, и ссылки в JSON-атрибутах галерейного плагина.
MEDIA_RE = re.compile(
    rf"(?:(?:https?:)?//[^\s\"'<>()\\]+?|/wp-content/[^\s\"'<>()\\]+?)\.(?:{_EXT}){_TAIL}(?![\w])",
    re.I,
)
THUMB_RE = re.compile(r"-\d{2,4}x\d{2,4}(?=\.\w+$)")  # name-800x445.jpg -> name.jpg
BAD = ("/plugins/", "/themes/", "/wp-includes/", "spacer", "emoji", "gravatar", "dryuu")
SKIP_HOSTS = ("facebook.", "x.com", "twitter.", "instagram.", "t.me", "theporndude",
              "dryuu", "disqus", "wordpress.org", "themegrill")
JUNK_SEL = ("#secondary, aside, .widget, footer, nav, .related-posts, "
            ".post-navigation, #comments")
CONTAINERS = (".entry-content", ".post-content", "article", "main", "#main", "body")
POST_RE = re.compile(r"^/(?!category/|tag/|author/|page/|wp-)[^/]+/[^/]+/?$")

# Ключи блока "Cosplayer: ... / Game: ... / File Size: ..." -> нормальные поля
KEY_GROUPS = {
    "cosplayer": ("cosplayer", "model", "artist"),
    "character": ("character",),
    "source": ("game", "anime", "movie", "series", "manga", "show", "cartoon", "tv show",
               "appear in"),
    "content": ("content", "image", "images", "video", "videos", "pics", "photos"),
    "resolution": ("resolution",),
    "file_size": ("file size", "size"),
    "duration": ("duration",),
    "password": ("password download", "password", "unzip password"),
}
KNOWN_KEYS = {k for g in KEY_GROUPS.values() for k in g}

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
  slug TEXT PRIMARY KEY, url TEXT, title TEXT, section TEXT,
  cosplayer TEXT, character TEXT, source TEXT, tags TEXT,
  listing_sig TEXT, fingerprint TEXT, status TEXT, folder TEXT, zipped INTEGER,
  files_json TEXT, meta_json TEXT, total_bytes INTEGER, updated_at TEXT, published TEXT,
  site TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS publications(
  slug TEXT PRIMARY KEY, title TEXT, cosplayer TEXT, site TEXT, status TEXT, detail TEXT,
  photos_total INTEGER, photos_sent INTEGER, msg_id TEXT, first_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS searches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, q TEXT, field TEXT, hits INTEGER, found INTEGER,
  first_at TEXT, last_at TEXT, UNIQUE(q, field));
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, value TEXT, nvalue TEXT, created_at TEXT,
  last_hit_at TEXT, hits INTEGER, UNIQUE(kind, nvalue));
"""
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
  id BIGINT GENERATED BY DEFAULT AS IDENTITY UNIQUE,
  slug TEXT PRIMARY KEY, url TEXT, title TEXT, section TEXT,
  cosplayer TEXT, character TEXT, source TEXT, tags TEXT,
  listing_sig TEXT, fingerprint TEXT, status TEXT, folder TEXT, zipped INTEGER,
  files_json TEXT, meta_json TEXT, total_bytes BIGINT, updated_at TEXT, published TEXT,
  site TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS publications(
  slug TEXT PRIMARY KEY, title TEXT, cosplayer TEXT, site TEXT, status TEXT, detail TEXT,
  photos_total INTEGER, photos_sent INTEGER, msg_id TEXT, first_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS searches(
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, q TEXT, field TEXT, hits INTEGER,
  found INTEGER, first_at TEXT, last_at TEXT, UNIQUE(q, field));
CREATE TABLE IF NOT EXISTS subscriptions(
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, kind TEXT, value TEXT, nvalue TEXT,
  created_at TEXT, last_hit_at TEXT, hits INTEGER, UNIQUE(kind, nvalue));
"""
COLS = ["slug", "url", "title", "section", "cosplayer", "character", "source", "tags",
        "listing_sig", "fingerprint", "status", "folder", "zipped", "files_json",
        "meta_json", "total_bytes", "updated_at", "published", "site",
        # производные (заполняются в Library.save): быстрый поиск и поиск дубликатов
        "search_text", "ncos", "ntitle", "nphotos"]
COL_TYPES = {"total_bytes": ("BIGINT", "INTEGER"), "zipped": ("INTEGER", "INTEGER"),
             "nphotos": ("INTEGER", "INTEGER")}   # (Postgres, SQLite); остальные — TEXT
# Статусы истории публикаций и их группы для фильтра
HIST_LABELS = {
    "full": "✅ Полностью", "published": "✅ Опубликован (раньше, без подробностей)",
    "partial": "⚠️ Частично", "post_only": "⚠️ Только пост, без комментариев",
    "skip_video": "🎬 Не опубликован: в посте видео",
    "skip_archives": "🗂 Не опубликован: только ссылки на архивы",
    "skip_empty": "⬜ Не опубликован: нет фото",
    "skip_duplicate": "🔁 Не опубликован: дубликат", "error": "❌ Ошибка",
}
HIST_GROUPS = {
    "all": None,
    "ok": ("full", "published"),
    "partial": ("partial", "post_only"),
    "skipped": ("skip_video", "skip_archives", "skip_empty", "skip_duplicate"),
    "error": ("error",),
}
SEARCH_FIELDS = {
    None: ("title", "cosplayer", "character", "source", "tags"),
    "all": ("title", "cosplayer", "character", "source", "tags"),
    "cosplayer": ("cosplayer", "title"),
    "game": ("source",),
    "character": ("character",),
    "tag": ("tags",),
}


def ensure_postgres_schema(connection):
    for statement in POSTGRES_SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(
        "ALTER TABLE posts ADD COLUMN IF NOT EXISTS id "
        "BIGINT GENERATED BY DEFAULT AS IDENTITY UNIQUE")
    for column in COLS:
        if column == "slug":
            continue
        column_type = COL_TYPES.get(column, ("TEXT", "TEXT"))[0]
        connection.execute(
            f"ALTER TABLE posts ADD COLUMN IF NOT EXISTS {column} {column_type}")
    connection.commit()


# --------------------------------------------------------------------------- утилиты
class Stopped(Exception):
    """Задача остановлена пользователем."""


class Control:
    """Пауза / продолжение / остановка. Проверяется между файлами и в цикле загрузки."""

    def __init__(self):
        self.paused = False
        self.stopped = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.stopped = True
        self.paused = False

    def reset(self):
        self.paused = self.stopped = False

    def checkpoint(self):
        while self.paused and not self.stopped:
            time.sleep(0.3)
        if self.stopped:
            raise Stopped()


@dataclass
class Options:
    zip: bool = False          # упаковывать каждый пост в zip
    images: bool = True
    videos: bool = True
    workers: int = 4
    delay: float = 1.0         # пауза между постами, сек
    keep_cover: bool = False   # не пропускать *-Cover.jpg
    dry_run: bool = False      # только показать найденное
    force: bool = False        # перекачать всё
    recheck: bool = False      # не доверять сигнатуре из листинга, открывать каждый пост
    until_known: bool = False  # остановить обход, когда страница листинга целиком известна
    storage: str = "meta"      # meta — только метаданные (файлы не сохраняются), full — качать на диск
    pause_every: int = 0       # длинная пауза после стольких обработанных постов (0 — без неё)
    pause_seconds: float = 0   # длина длинной паузы, сек (защита IP от блокировки)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human_size(n):
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")[:150] or "file"


def slug_of(url):
    """Уникальный ключ поста (для mitaku — как раньше, для остальных сайтов с префиксом)."""
    import mitaku_sites
    return mitaku_sites.slug_of(url)


def fix_enc(r):
    """Если сервер не указал кодировку, requests берёт latin-1 — для сайта нужен utf-8."""
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "utf-8"
    return r


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    return s


def full_size(url):
    p = urlsplit(url)
    return urlunsplit(p._replace(path=THUMB_RE.sub("", p.path), fragment=""))


def mitaku_is_post_url(u):
    p = urlsplit(u)
    return p.netloc.endswith(SITE_HOST) and bool(POST_RE.match(p.path))


def is_post_url(u):
    """Пост ли это на любом из поддерживаемых сайтов."""
    import mitaku_sites
    return mitaku_sites.is_post_url(u)


def listing_page_url(base, n):
    if n == 1:
        return base
    s = urlsplit(base)
    path = re.sub(r"/page/\d+/?$", "", s.path).rstrip("/") + f"/page/{n}/"
    return urlunsplit(s._replace(path=path))


# --------------------------------------------------------------------------- разбор HTML
def scan(text, page_url, keep_cover):
    """Возвращает (images, videos): списки кандидатов [полноразмерный, оригинал]."""
    text = text.replace("\\/", "/")
    seen, images, videos = set(), [], []
    for m in MEDIA_RE.finditer(text):
        url = urljoin(page_url, htmllib.unescape(m.group(0)))
        if any(b in url.lower() for b in BAD):
            continue
        key = full_size(url)
        if key in seen:
            continue
        path = urlsplit(key).path
        if not keep_cover and re.search(r"[-_]cover\.\w+$", path, re.I):
            continue
        seen.add(key)
        cands = [key] if key == url else [key, url]
        ext = path.rsplit(".", 1)[-1].lower()
        (videos if ext in VID_EXT else images).append(cands)
    return images, videos


def is_video_host(url):
    host = urlsplit(url).netloc.lower()
    return any(h in host for h in VIDEO_EMBED_HOSTS)


def split_embeds(links):
    """(архивы, видео-плееры) из списка внешних ссылок поста."""
    embeds = [u for u in links if is_video_host(u)]
    return [u for u in links if u not in embeds], embeds


def video_sources(node, page_url, meta=None):
    """Видео поста, не найденное по расширению в тексте: теги <video>/<source>, og:video, JSON-LD,
    iframe-плееры, HLS/DASH-плейлисты. node — содержимое поста, meta — вся страница (для <head>).
    Возвращает (прямые_ссылки_на_файлы, плееры_и_потоки)."""
    direct, embeds = [], []

    def add(url, kind=""):
        url = urljoin(page_url, htmllib.unescape((url or "").strip()))
        if not url.startswith("http") or any(h in urlsplit(url).netloc.lower() for h in SKIP_HOSTS):
            return
        ext = urlsplit(url).path.rsplit(".", 1)[-1].lower() if "." in urlsplit(url).path else ""
        if ext in VID_EXT or (kind.startswith("video/") and ext not in STREAM_EXT):
            direct.append(url)
        elif url not in embeds:
            embeds.append(url)   # плеер, HLS/DASH или страница с видео

    for tag in node.select("video[src], video[data-src], source[src], source[data-src]"):
        kind = (tag.get("type") or "").lower()
        if tag.name == "video" or kind.startswith("video/") or "mpegurl" in kind or "dash" in kind:
            add(tag.get("src") or tag.get("data-src"), kind)
    for tag in node.select("iframe[src], iframe[data-src]"):
        add(tag.get("src") or tag.get("data-src"))
    scope = meta if meta is not None else node
    for tag in scope.select("meta[property='og:video'], meta[property='og:video:url'], "
                            "meta[property='og:video:secure_url'], meta[name='twitter:player:stream']"):
        add(tag.get("content"), (tag.get("type") or "").lower())
    for script in scope.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack += item
            elif isinstance(item, dict):
                if str(item.get("@type", "")).lower() == "videoobject":
                    add(item.get("contentUrl"), "video/")
                    add(item.get("embedUrl"))
                stack += [v for v in item.values() if isinstance(v, (dict, list))]
    return _uniq(direct), _uniq(embeds)


def attach_video_sources(post, node, page_url, meta=None):
    """Дополняет пост видео из тегов/метаданных и выносит плееры из external в video_embeds."""
    direct, embeds = video_sources(node, page_url, meta)
    seen = {full_size(c[0]) for c in post["videos"]}
    for url in direct:
        key = full_size(url)
        if key not in seen:
            seen.add(key)
            post["videos"].append([key] if key == url else [key, url])
    archives, host_embeds = split_embeds(post["external"])
    post["external"] = archives
    post["video_embeds"] = _uniq(post.get("video_embeds", []) + embeds + host_embeds)
    return post


def external_links(node, page_url):
    out = []
    for tag in node.select("iframe[src], a[href]"):
        u = urljoin(page_url, tag.get("src") or tag.get("href"))
        host = urlsplit(u).netloc.lower()
        if not u.startswith("http") or SITE_HOST in host or u in out:
            continue
        if any(h in host for h in SKIP_HOSTS) or MEDIA_RE.fullmatch(u):
            continue
        out.append(u)
    return out


def parse_info(text):
    """Блок 'Key: value' -> {key: value}, только известные ключи."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    key_re = re.compile(r"^([A-Za-z][A-Za-z /]{1,24}):\s*(.*)$")
    info, i = {}, 0
    while i < len(lines):
        m = key_re.match(lines[i])
        if m and m.group(1).strip().lower() in KNOWN_KEYS:
            k, v = m.group(1).strip().lower(), m.group(2).strip()
            if not v and i + 1 < len(lines) and not key_re.match(lines[i + 1]):
                v = lines[i + 1]
                i += 1
            if len(v) > 80:  # это уже абзац текста, а не значение поля
                v = ""
            info.setdefault(k, v)
        i += 1
    return info


def normalize_info(info, title):
    def pick(group):
        for k in KEY_GROUPS[group]:
            if info.get(k):
                return info[k]
        return ""

    cosplayer = pick("cosplayer")
    if not cosplayer and re.search(r"\s[–—-]\s", title):
        cosplayer = re.split(r"\s[–—-]\s", title, maxsplit=1)[0].strip()
    content_text = " ".join(info.get(k, "") for k in KEY_GROUPS["content"])
    photos = re.search(r"(\d+)\s*(?:pics?|photos?|images?)", content_text, re.I)
    vids = re.search(r"(\d+)\s*videos?", content_text, re.I)
    return {
        "cosplayer": cosplayer,
        "character": pick("character"),
        "source": pick("source"),
        "declared": {
            "content": content_text.strip(),
            "photos": int(photos.group(1)) if photos else None,
            "videos": int(vids.group(1)) if vids else None,
            "resolution": pick("resolution"),
            "file_size": pick("file_size"),
            "duration": pick("duration"),
        },
        "password": pick("password"),
    }


def _uniq(seq):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


def parse_post(page_html, page_url, keep_cover=False):
    soup = BeautifulSoup(page_html, "html.parser")
    scope = soup.select_one("article") or soup
    tags = _uniq(a.get_text(strip=True) for a in scope.select("a[href*='/tag/']"))
    cats = _uniq(a.get_text(strip=True) for a in scope.select("a[href*='/category/']"))
    t_pub = scope.select_one("time.published, time.entry-date")
    t_mod = scope.select_one("time.updated")
    og = soup.select_one("meta[property='article:published_time']")
    mod = soup.select_one("meta[property='article:modified_time']")
    published = (t_pub.get("datetime") if t_pub else None) or (og.get("content") if og else "")
    modified = (t_mod.get("datetime") if t_mod else None) or (mod.get("content") if mod else "")
    h1 = soup.select_one("h1.entry-title, h1")
    title = h1.get_text(strip=True) if h1 else ""

    for junk in soup.select(JUNK_SEL):
        junk.decompose()
    images, videos, ext, info = [], [], [], {}
    for sel in CONTAINERS:
        node = soup.select_one(sel)
        if not node:
            continue
        images, videos = scan(str(node), page_url, keep_cover)
        if images or videos:
            ext = external_links(node, page_url)
            info = parse_info(node.get_text("\n", strip=True))
            break
    if not info:  # нет медиа, но карточка с описанием может быть
        node = soup.select_one(".entry-content") or soup.select_one("article")
        if node:
            ext = external_links(node, page_url)
            info = parse_info(node.get_text("\n", strip=True))
    norm = normalize_info(info, title)
    post = {"title": title, "images": images, "videos": videos, "external": ext,
            "info": info, "norm": norm, "tags": tags, "categories": cats,
            "published": published, "modified": modified}
    content = next((n for n in (soup.select_one(sel) for sel in CONTAINERS) if n), soup)
    return attach_video_sources(post, content, page_url, soup)


def parse_listing(page_html, page_url):
    """Карточки листинга: [{url, title, sig}]. sig меняется, когда пост обновили."""
    soup = BeautifulSoup(page_html, "html.parser")
    items, seen = [], set()
    for art in soup.select("article"):
        a = art.select_one("h2 a[href], h3 a[href], .entry-title a[href]")
        if not a:
            continue
        u = urljoin(page_url, a["href"]).split("#")[0]
        if not mitaku_is_post_url(u) or u in seen:
            continue
        seen.add(u)
        times = [t.get("datetime") or t.get_text(strip=True) for t in art.select("time")]
        title = a.get_text(strip=True)
        items.append({"url": u, "title": title, "sig": "|".join([title] + times)})
    if not items:  # запасной вариант — все ссылки на посты
        for a in soup.select("a[href]"):
            u = urljoin(page_url, a["href"]).split("#")[0]
            if mitaku_is_post_url(u) and u not in seen:
                seen.add(u)
                items.append({"url": u, "title": a.get_text(strip=True), "sig": None})
    return items


def fingerprint(post):
    data = {
        "m": sorted(full_size(c[0]) for c in post["images"] + post["videos"]),
        "e": sorted(post["external"]),
        "v": sorted(post.get("video_embeds", [])),
        "i": post["info"],
    }
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()


def assign_names(media):
    """media: [(cands, kind)] -> [{name, kind, cands}] с уникальными именами файлов."""
    used, out = set(), []
    for cands, kind in media:
        sp = urlsplit(cands[0])
        hint = re.match(r"name=(.+)", sp.fragment)  # сайты без расширений в URL (galleryepic)
        name = safe_name(unquote(hint.group(1) if hint else sp.path.rsplit("/", 1)[-1]))
        if "." not in name:
            name += ".mp4" if kind == "video" else ".jpg"
        base, _, ext = name.rpartition(".")
        n = 2
        while name.lower() in used:
            name = f"{base}_{n}.{ext}"
            n += 1
        used.add(name.lower())
        out.append({"name": name, "kind": kind, "cands": cands})
    return out


# --------------------------------------------------------------------------- библиотека (SQLite)
def derive_fields(rec):
    """Производные колонки поста: search_text (для поиска), ncos/ntitle/nphotos (для дубликатов)."""
    title, cos = rec.get("title") or "", rec.get("cosplayer") or ""
    photos = 0
    try:
        photos = sum(1 for f in json.loads(rec.get("files_json") or "[]") if f.get("kind") == "photo")
        if not photos:
            photos = int(((json.loads(rec.get("meta_json") or "{}").get("declared") or {})
                          .get("photos")) or 0)
    except (ValueError, TypeError, AttributeError):
        pass
    parts = (title, cos, rec.get("character"), rec.get("source"), rec.get("tags"))
    return {"search_text": dedupe.norm_text(" ".join(x for x in parts if x)),
            "ncos": dedupe.norm_name(cos), "ntitle": dedupe.norm_title(title, cos), "nphotos": photos}


class Library:
    def __init__(self, out_dir):
        self.root = Path(out_dir)
        self.posts_dir = self.root / "posts"
        self.posts_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.postgres = bool(os.getenv("DATABASE_URL", "").strip())
        if self.postgres:
            self._connect_pg()
        else:
            self.db = sqlite3.connect(self.root / "library.db", check_same_thread=False)
            self.db.row_factory = sqlite3.Row
        with self.lock:
            if self.postgres:
                ensure_postgres_schema(self.db)
            else:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.executescript(SCHEMA)
                have = {r[1] for r in self.db.execute("PRAGMA table_info(posts)")}
                for c in COLS:  # миграция старой базы: новые колонки
                    if c not in have:
                        self.db.execute(
                            f"ALTER TABLE posts ADD COLUMN {c} {COL_TYPES.get(c, ('TEXT', 'TEXT'))[1]}")
            self._execute("UPDATE posts SET site='mitaku' WHERE site IS NULL OR site=''")
            self.db.commit()
        self._ver, self._cache = 0, {}   # кэш результатов поиска; _ver растёт при любой записи постов
        self._backfill_search()
        self._ensure_indexes()
        self._backfill_history()

    def _backfill_search(self):
        """Заполняет производные колонки у постов, сохранённых прежними версиями."""
        while True:
            rows = self._all("SELECT slug, title, cosplayer, character, source, tags, files_json, "
                             "meta_json FROM posts WHERE search_text IS NULL LIMIT 500")
            if not rows:
                return
            with self.lock:
                for r in rows:
                    d = derive_fields(r)
                    self._execute("UPDATE posts SET search_text=?, ncos=?, ntitle=?, nphotos=? "
                                  "WHERE slug=?", (d["search_text"], d["ncos"], d["ntitle"],
                                                   d["nphotos"], r["slug"]))
                self.db.commit()

    def _ensure_indexes(self):
        """Индексы: по дате (список без фильтра), по нику/названию (дубликаты) и, в Postgres, триграммные
        для поиска по подстроке. Любой индекс, который не удалось создать, пропускается."""
        statements = [
            "CREATE INDEX IF NOT EXISTS posts_pub_idx ON posts ((COALESCE(published, '')) DESC)",
            "CREATE INDEX IF NOT EXISTS posts_ncos_idx ON posts (ncos)",
            "CREATE INDEX IF NOT EXISTS posts_ntitle_idx ON posts (ntitle)",
            "CREATE INDEX IF NOT EXISTS posts_site_idx ON posts (site)",
        ]
        if self.postgres:
            statements += ["CREATE EXTENSION IF NOT EXISTS pg_trgm"] + [
                f"CREATE INDEX IF NOT EXISTS posts_{c}_trgm ON posts USING gin ({c} gin_trgm_ops)"
                for c in ("search_text", "cosplayer", "source", "character", "tags")]
        for sql in statements:
            try:
                with self.lock:
                    self._execute(sql)
                    self.db.commit()
            except Exception as e:  # noqa: BLE001
                print(f"индекс пропущен ({sql[:60]}…): {e}")

    def _connect_pg(self):
        import psycopg
        from psycopg.rows import dict_row
        self.db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)

    def _execute(self, sql, args=()):
        if not self.postgres:
            return self.db.execute(sql, args)
        import psycopg
        sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                return self.db.execute(sql, args)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # Supabase/Render рвут простаивающие соединения: переподключаемся и повторяем
                if attempt == 2:
                    raise
                try:
                    self.db.close()
                except Exception:  # noqa: BLE001
                    pass
                self._connect_pg()
            except Exception:
                self.db.rollback()   # иначе вся транзакция «aborted» до перезапуска
                raise

    def _one(self, sql, args=()):
        with self.lock:
            row = self._execute(sql, args).fetchone()
            if self.postgres:
                self.db.commit()
        return dict(row) if row else None

    def get(self, slug):
        columns = "*" if self.postgres else "rowid AS id, *"
        return self._one(f"SELECT {columns} FROM posts WHERE slug=?", (slug,))

    def get_by_id(self, rid):
        row_id = "id" if self.postgres else "rowid"
        columns = "*" if self.postgres else "rowid AS id, *"
        return self._one(f"SELECT {columns} FROM posts WHERE {row_id}=?", (rid,))

    def folder_owner(self, folder):
        row = self._one("SELECT slug FROM posts WHERE folder=?", (folder,))
        return row["slug"] if row else None

    def save(self, rec):
        rec = {**rec, **derive_fields(rec)}
        vals = [rec.get(c) for c in COLS]
        upd = ",".join(f"{c}=excluded.{c}" for c in COLS if c != "slug")
        with self.lock:
            self._execute(
                f"INSERT INTO posts({','.join(COLS)}) VALUES({','.join('?' * len(COLS))}) "
                f"ON CONFLICT(slug) DO UPDATE SET {upd}", vals)
            self.db.commit()
            self._ver += 1
            self._cache.clear()

    def search(self, query="", field=None, limit=20, offset=0, site=None):
        """(строки, всего). Свободный текст ищется по одной колонке search_text (нижний регистр,
        в том числе кириллица/японский), с полем — по соответствующим колонкам. Одним запросом
        (COUNT(*) OVER()), результаты кэшируются до следующей записи в posts."""
        key = (query, field, limit, offset, site)
        with self.lock:
            hit = self._cache.get(key)
        if hit:
            return [dict(r) for r in hit[0]], hit[1]
        where, args = [], []
        if site:
            where.append("site = ?")
            args.append(site)
        cols = SEARCH_FIELDS.get(field, SEARCH_FIELDS[None])
        like = "ILIKE" if self.postgres else "LIKE"   # в Postgres LIKE чувствителен к регистру
        for w in dedupe.norm_text(query).split():
            w = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            if field in (None, "all"):
                where.append("search_text LIKE ? ESCAPE '\\'")
                args.append(f"%{w}%")
            else:
                where.append("(" + " OR ".join(f"{c} {like} ? ESCAPE '\\'" for c in cols) + ")")
                args += [f"%{w}%"] * len(cols)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        row_id = "id" if self.postgres else "rowid"
        columns = "*" if self.postgres else "rowid AS id, *"
        order = f"ORDER BY COALESCE(published, '') DESC, {row_id} DESC LIMIT ? OFFSET ?"
        with self.lock:
            try:
                rows = [dict(r) for r in self._execute(
                    f"SELECT {columns}, COUNT(*) OVER() AS _total FROM posts{cond} {order}",
                    args + [limit, offset]).fetchall()]
                total = rows[0]["_total"] if rows else None
                for r in rows:
                    r.pop("_total", None)
            except Exception:  # noqa: BLE001  (старый SQLite без оконных функций)
                rows = [dict(r) for r in self._execute(
                    f"SELECT {columns} FROM posts{cond} {order}", args + [limit, offset]).fetchall()]
                total = None
            if total is None:   # страница за пределами результатов или запасной путь
                count = self._execute(f"SELECT COUNT(*) AS count FROM posts{cond}", args).fetchone()
                total = count["count"] if self.postgres else count[0]
            if self.postgres:
                self.db.commit()
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[key] = ([dict(r) for r in rows], total)
        return rows, total

    def get_many(self, slugs):
        """{slug: пост} одним запросом на пачку (вместо запроса на каждый slug)."""
        slugs = list(dict.fromkeys(slugs))
        columns = "*" if self.postgres else "rowid AS id, *"
        out = {}
        for i in range(0, len(slugs), 300):
            chunk = slugs[i:i + 300]
            for r in self._all(f"SELECT {columns} FROM posts WHERE slug IN ({','.join('?' * len(chunk))})", chunk):
                out[r["slug"]] = r
        return out

    def existing_slugs(self, slugs):
        slugs = list(dict.fromkeys(slugs))
        found = set()
        for i in range(0, len(slugs), 300):
            chunk = slugs[i:i + 300]
            found |= {r["slug"] for r in self._all(
                f"SELECT slug FROM posts WHERE slug IN ({','.join('?' * len(chunk))})", chunk)}
        return found

    # ---- дубликаты
    def dup_candidates(self, rec):
        """Посты библиотеки, похожие на rec (тот же косплеер и название; см. dedupe.is_duplicate)."""
        cols = "slug, title, site, ncos, ntitle, nphotos, published"
        if rec.get("ncos"):
            rows = self._all(f"SELECT {cols} FROM posts WHERE ncos=? AND slug!=?", (rec["ncos"], rec["slug"]))
        elif rec.get("ntitle"):
            rows = self._all(f"SELECT {cols} FROM posts WHERE ntitle=? AND slug!=? "
                             "AND (ncos IS NULL OR ncos='')", (rec["ntitle"], rec["slug"]))
        else:
            rows = []
        return [r for r in rows if dedupe.is_duplicate(rec, r)[0]]

    def published_among(self, slugs):
        slugs = list(slugs)
        if not slugs:
            return set()
        return {r["slug"] for r in self._all(
            "SELECT slug FROM publications WHERE status IN ('full','partial','post_only','published') "
            f"AND slug IN ({','.join('?' * len(slugs))})", slugs)}

    def find_duplicate_of(self, rec):
        """Уже опубликованный в канал пост, дублирующий rec, или None."""
        candidates = self.dup_candidates(rec)
        if not candidates:
            return None
        published = self.published_among([c["slug"] for c in candidates])
        return next((c for c in candidates if c["slug"] in published), None)

    def dup_groups(self):
        """Группы дубликатов в библиотеке: [[пост, ...], ...] (по убыванию размера группы).
        У постов поля post_id и is_published."""
        row_id = "id" if self.postgres else "rowid"
        rows = self._all(
            f"SELECT {row_id} AS post_id, slug, title, site, ncos, ntitle, nphotos, published FROM posts "
            "WHERE ncos != '' AND ncos IN (SELECT ncos FROM posts WHERE ncos != '' "
            "GROUP BY ncos HAVING COUNT(*) > 1)")
        by_cos = {}
        for r in rows:
            by_cos.setdefault(r["ncos"], []).append(r)
        groups = []
        for items in by_cos.values():
            parent = list(range(len(items)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    if dedupe.is_duplicate(items[i], items[j])[0]:
                        parent[find(i)] = find(j)
            clusters = {}
            for i, item in enumerate(items):
                clusters.setdefault(find(i), []).append(item)
            groups += [c for c in clusters.values() if len(c) > 1]
        published = self.published_among([r["slug"] for g in groups for r in g])
        for g in groups:
            for r in g:
                r["is_published"] = r["slug"] in published
            g.sort(key=lambda r: (not r["is_published"], r["published"] or ""))
        return sorted(groups, key=lambda g: (-len(g), g[0]["title"] or ""))

    # ---- подписки
    def sub_add(self, kind, value):
        """(id, создана_ли_новая). Одинаковые (тип, значение) не дублируются."""
        nvalue = subs.normalize_value(kind, value)
        if not nvalue:
            raise ValueError("пустое значение подписки")
        row = self._one("SELECT id FROM subscriptions WHERE kind=? AND nvalue=?", (kind, nvalue))
        if row:
            return row["id"], False
        with self.lock:
            self._execute("INSERT INTO subscriptions(kind, value, nvalue, created_at, hits) "
                          "VALUES(?,?,?,?,0)", (kind, value.strip(), nvalue, now_iso()))
            self.db.commit()
        return self._one("SELECT id FROM subscriptions WHERE kind=? AND nvalue=?", (kind, nvalue))["id"], True

    def sub_list(self, limit=10, offset=0):
        with self.lock:
            total = self._execute("SELECT COUNT(*) AS n FROM subscriptions").fetchone()
            rows = self._execute("SELECT * FROM subscriptions ORDER BY id DESC LIMIT ? OFFSET ?",
                                 (limit, offset)).fetchall()
            if self.postgres:
                self.db.commit()
        return [dict(r) for r in rows], (total["n"] if self.postgres else total[0])

    def sub_delete(self, sub_id):
        with self.lock:
            self._execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            self.db.commit()

    def sub_match(self, rec):
        """Подписки, под которые подходит пост rec."""
        return [s for s in self._all("SELECT * FROM subscriptions") if subs.matches(s, rec)]

    def sub_hit(self, sub_ids):
        with self.lock:
            for sid in sub_ids:
                self._execute("UPDATE subscriptions SET hits=COALESCE(hits,0)+1, last_hit_at=? "
                              "WHERE id=?", (now_iso(), sid))
            self.db.commit()

    # ---- история публикаций в канал
    def _backfill_history(self):
        """Один раз переносит старые отметки published:<slug> в таблицу истории."""
        if self.kv_get("history_backfilled"):
            return
        have = {r["slug"] for r in self._all("SELECT slug FROM publications")}
        for key in self.kv_list("published:"):
            slug = key.split(":", 1)[1]
            if slug in have:
                continue
            rec = self.get(slug) or {}
            self.hist_set(slug, "published", "опубликован раньше, подробностей нет",
                          title=rec.get("title") or slug, cosplayer=rec.get("cosplayer") or "",
                          site=rec.get("site") or "", msg_id=self.kv_get(key) or "")
        self.kv_set("history_backfilled", "1")

    def _all(self, sql, args=()):
        with self.lock:
            rows = self._execute(sql, args).fetchall()
            if self.postgres:
                self.db.commit()
        return [dict(r) for r in rows]

    def hist_set(self, slug, status, detail="", **kw):
        """Записывает/обновляет итог публикации поста: status — ключ HIST_LABELS.
        kw: title, cosplayer, site, photos_total, photos_sent, msg_id."""
        old = self._one("SELECT * FROM publications WHERE slug=?", (slug,)) or {}
        now = now_iso()
        row = {"slug": slug, "title": kw.get("title") or old.get("title") or slug,
               "cosplayer": kw.get("cosplayer") if kw.get("cosplayer") is not None
               else old.get("cosplayer") or "",
               "site": kw.get("site") or old.get("site") or "", "status": status,
               "detail": detail, "photos_total": kw.get("photos_total", old.get("photos_total") or 0),
               "photos_sent": kw.get("photos_sent", old.get("photos_sent") or 0),
               "msg_id": str(kw.get("msg_id") or old.get("msg_id") or ""),
               "first_at": old.get("first_at") or now, "updated_at": now}
        cols = list(row)
        upd = ",".join(f"{c}=excluded.{c}" for c in cols if c != "slug")
        with self.lock:
            self._execute(f"INSERT INTO publications({','.join(cols)}) "
                          f"VALUES({','.join('?' * len(cols))}) "
                          f"ON CONFLICT(slug) DO UPDATE SET {upd}", [row[c] for c in cols])
            self.db.commit()

    def export_rows(self):
        """Строки библиотеки для CSV (только нужные столбцы, без тяжёлых JSON)."""
        return self._all("SELECT slug, title, cosplayer, character, source, site, status, published, "
                         "tags, nphotos, url FROM posts ORDER BY COALESCE(published, '') DESC")

    def video_report(self):
        """Статистика видео: у скольких постов заявлено видео, где оно найдено, что нужно перечитать
        (посты, разобранные до появления поля video_embeds)."""
        out = {"declared": 0, "files": 0, "embeds": 0, "missing": 0, "refresh": []}
        for row in self._all("SELECT url, files_json, meta_json FROM posts"):
            meta = json.loads(row["meta_json"] or "{}")
            declared = (meta.get("declared") or {}).get("videos") or 0
            if not declared:
                continue
            out["declared"] += 1
            has_file = any(f.get("kind") == "video" for f in json.loads(row["files_json"] or "[]"))
            has_embed = bool(meta.get("video_embeds")) or any(
                is_video_host(u) for u in meta.get("archives", []))
            out["files" if has_file else "embeds" if has_embed else "missing"] += 1
            if "video_embeds" not in meta:
                out["refresh"].append(row["url"])
        return out

    def hist_delete(self, slug):
        with self.lock:
            self._execute("DELETE FROM publications WHERE slug=?", (slug,))
            self.db.commit()

    def hist_get(self, slug):
        return self._one("SELECT * FROM publications WHERE slug=?", (slug,))

    def hist_list(self, group="all", limit=8, offset=0):
        """(строки, всего) — история публикаций (новые первыми); group — ключ HIST_GROUPS."""
        statuses = HIST_GROUPS.get(group)
        cond, args = "", []
        if statuses:
            cond = " WHERE h.status IN (" + ",".join("?" * len(statuses)) + ")"
            args = list(statuses)
        row_id = "p.id" if self.postgres else "p.rowid"
        with self.lock:
            total = self._execute(f"SELECT COUNT(*) AS n FROM publications h{cond}", args).fetchone()
            rows = self._execute(
                f"SELECT h.*, {row_id} AS post_id, p.url AS url FROM publications h "
                f"LEFT JOIN posts p ON p.slug = h.slug{cond} "
                f"ORDER BY h.updated_at DESC, h.slug LIMIT ? OFFSET ?", args + [limit, offset]
            ).fetchall()
            if self.postgres:
                self.db.commit()
        return [dict(r) for r in rows], (total["n"] if self.postgres else total[0])

    def hist_counts(self):
        """{группа: число постов} по всей истории."""
        by_status = {r["status"]: r["n"] for r in self._all(
            "SELECT status, COUNT(*) AS n FROM publications GROUP BY status")}
        counts = {g: (sum(by_status.get(s, 0) for s in st) if st else sum(by_status.values()))
                  for g, st in HIST_GROUPS.items()}
        return counts

    # ---- история поиска (за всё время)
    def search_log(self, q, field, found):
        q, field, now = q.strip(), field or "", now_iso()
        if not q:
            return
        with self.lock:
            self._execute(
                "INSERT INTO searches(q, field, hits, found, first_at, last_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(q, field) DO UPDATE SET "
                "hits=searches.hits+1, found=excluded.found, last_at=excluded.last_at",
                (q, field, 1, found, now, now))
            self.db.commit()

    def search_list(self, limit=10, offset=0):
        with self.lock:
            total = self._execute("SELECT COUNT(*) AS n FROM searches").fetchone()
            rows = self._execute("SELECT * FROM searches ORDER BY last_at DESC, id DESC "
                                 "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            if self.postgres:
                self.db.commit()
        return [dict(r) for r in rows], (total["n"] if self.postgres else total[0])

    def search_get(self, sid):
        return self._one("SELECT * FROM searches WHERE id=?", (sid,))

    def search_clear(self):
        with self.lock:
            self._execute("DELETE FROM searches")
            self.db.commit()

    def stats(self):
        with self.lock:
            count = self._execute("SELECT COUNT(*) AS count FROM posts").fetchone()
            total_bytes = self._execute(
                "SELECT COALESCE(SUM(total_bytes),0) AS total FROM posts").fetchone()
            statuses = self._execute(
                "SELECT status, COUNT(*) AS count FROM posts GROUP BY status").fetchall()
            sections = self._execute(
                "SELECT section, COUNT(*) AS count FROM posts GROUP BY section").fetchall()
            sites = self._execute(
                "SELECT site, COUNT(*) AS count FROM posts GROUP BY site").fetchall()
            cosplayers = self._execute(
                "SELECT COUNT(DISTINCT cosplayer) AS count FROM posts "
                "WHERE cosplayer!=''").fetchone()
            if self.postgres:
                self.db.commit()
            if self.postgres:
                to_counts = lambda rows, column: {row[column]: row["count"] for row in rows}
                return {
                    "posts": count["count"],
                    "bytes": total_bytes["total"],
                    "status": to_counts(statuses, "status"),
                    "sections": to_counts(sections, "section"),
                    "sites": to_counts(sites, "site"),
                    "cosplayers": cosplayers["count"],
                }
            return {
                "posts": count[0],
                "bytes": total_bytes[0],
                "status": dict(statuses),
                "sections": dict(sections),
                "sites": dict(sites),
                "cosplayers": cosplayers[0],
            }

    def kv_get(self, key, default=None):
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def kv_list(self, prefix):
        """Множество ключей kv, начинающихся с prefix."""
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.lock:
            rows = self._execute("SELECT key FROM kv WHERE key LIKE ? ESCAPE '\\'",
                                 (pattern,)).fetchall()
            if self.postgres:
                self.db.commit()
        return {row["key"] for row in rows}

    def kv_set(self, key, value):
        with self.lock:
            self._execute("INSERT INTO kv(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.db.commit()


# --------------------------------------------------------------------------- основной класс
class Scraper:
    def __init__(self, out_dir, options=None, control=None, log=print):
        self.opt = options or Options()
        self.ctl = control or Control()
        self.log = log
        self.lib = Library(out_dir)
        self.session = make_session()
        self._plock = threading.Lock()
        self.on_post = None          # callback(slug, result) после сохранения поста (new/updated)
        self.search_errors = []      # [(ключ_сайта, текст)] последнего site_search
        self.blocked = False         # последний process_post упёрся в защиту сайта (403/429/503…)
        self.last_error = None
        self._since_pause = 0
        self.progress = {}
        self._reset_progress()
        self.progress["state"] = "idle"

    # ---- прогресс
    def _reset_progress(self):
        self.progress = dict(state="running", page=0, post="", file_i=0, file_n=0, new=0,
                             updated=0, skipped=0, partial=0, errors=0, files=0, bytes=0)

    def _add(self, **kw):
        with self._plock:
            for k, v in kw.items():
                self.progress[k] = self.progress.get(k, 0) + v

    def _set(self, **kw):
        with self._plock:
            self.progress.update(kw)

    def status(self):
        with self._plock:
            return dict(self.progress)

    # ---- паузы (антибан): ожидание, прерываемое остановкой, со случайным разбросом
    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while True:
            self.ctl.checkpoint()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.3, left))

    def _nap(self, base=None, count=True):
        """Пауза между запросами к сайту ±разброс; count — учитывать пост для длинной паузы."""
        base = self.opt.delay if base is None else base
        if base > 0:
            self._sleep(base * random.uniform(0.7, 1.5))
        if count and self.opt.pause_every:
            self._since_pause += 1
            if self._since_pause >= self.opt.pause_every:
                self._since_pause = 0
                pause = self.opt.pause_seconds * random.uniform(0.8, 1.3)
                if pause > 0:
                    self.log(f"   длинная пауза {pause:.0f} с (защита IP от блокировки)")
                    self._sleep(pause)

    def find_last_page(self, site, base_url, limit=5000):
        """Номер последней страницы листинга: растущий шаг ×2, затем двоичный поиск по
        «страница отдаёт 404/пусто». 0 — если у листинга нет ни одной страницы."""
        first = site.fetch_listing(self.session, base_url, 1)
        if not first:
            return 0
        first_urls = {x["url"] for x in first}

        def exists(n):
            self._nap(count=False)
            items = site.fetch_listing(self.session, base_url, n)
            # листинг, «зацикленный» на первую страницу, тоже считаем концом
            return bool(items) and {x["url"] for x in items} != first_urls

        lo, hi = 1, 2
        while hi <= limit and exists(hi):
            lo, hi = hi, hi * 2
        hi = min(hi, limit + 1)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if exists(mid):
                lo = mid
            else:
                hi = mid
        return lo

    # ---- сайты
    @staticmethod
    def site_for(url):
        import mitaku_sites
        site = mitaku_sites.site_for_url(url)
        if not site:
            raise ValueError(f"Неизвестный сайт: {url}")
        return site

    def site_search(self, query, pages=None, sites=None):
        """Поиск на самих сайтах. sites — список ключей (по умолчанию все)."""
        import mitaku_sites
        self.search_errors = []
        items = []
        for key in (sites or list(mitaku_sites.SITES)):
            site = mitaku_sites.SITES[key]
            try:
                items += site.search(self.session, query, pages)
            except Exception as e:  # noqa: BLE001
                self.search_errors.append((key, str(e)))
                self.log(f"   ! поиск на {key}: {e}")
        return items

    # ---- пути
    def _paths(self, folder, zipped):
        pd = self.lib.posts_dir
        if zipped:
            return pd / f"{folder}.zip", pd / f"{folder}.json"
        return None, pd / folder / "meta.json"

    def _output_exists(self, rec):
        if rec["status"] == "meta":  # метаданные есть всегда; в режиме full их надо дополнить файлами
            return self.opt.storage == "meta"
        z, j = self._paths(rec["folder"], bool(rec["zipped"]))
        files = json.loads(rec["files_json"] or "[]")
        return j.exists() and (z is None or z.exists() or not files)

    def _pick_folder(self, site, title, slug):
        name = safe_name(title) or slug
        if site.prefix:  # у остальных сайтов своя подпапка: posts/<сайт>/<название>
            name = f"{site.key}/{name}"
        owner = self.lib.folder_owner(name)
        return f"{name} [{slug}]" if owner and owner != slug else name

    # ---- обход листингов
    def sync(self, urls, first=1, last=1, limit=0):
        """Обходит ссылки (посты и/или листинги любых поддерживаемых сайтов)."""
        self._reset_progress()
        try:
            for u in urls:
                site = self.site_for(u)
                if site.is_post_url(u):
                    self.process_post({"url": site.canonical(u), "title": "", "sig": None})
                    continue
                count, seen = 0, set()
                for n in range(first, last + 1):
                    self.ctl.checkpoint()
                    items = site.fetch_listing(self.session, u, n)
                    if not items:
                        break
                    items = [x for x in items if x["url"] not in seen]  # повторы/сайдбары
                    if not items:
                        break
                    seen.update(x["url"] for x in items)
                    if limit:
                        items = items[: limit - count]
                    self._set(page=n)
                    self.log(f"[{site.key}] страница {n}: постов {len(items)}")
                    results = [self.process_post(it) for it in items]
                    count += len(items)
                    if limit and count >= limit:
                        break
                    if self.opt.until_known and all(x == "skipped" for x in results):
                        self.log("Страница целиком известна — останавливаю обход (until-known)")
                        break
                    self._nap(self.opt.delay, count=False)
            self._set(state="finished")
        except Stopped:
            self._set(state="stopped")
        return self.status()

    # ---- один пост
    def process_post(self, item):
        url, opt = item["url"], self.opt
        site = self.site_for(url)
        slug = site.slug(url)
        rec = self.lib.get(slug)
        try:
            if (rec and rec["status"] in ("done", "meta") and not opt.force and not opt.recheck
                    and item.get("sig") and rec["listing_sig"] == item["sig"]
                    and self._output_exists(rec)):
                self._add(skipped=1)
                return "skipped"
            self._set(post=item.get("title") or slug, file_i=0, file_n=0)
            self.blocked, self.last_error = False, None
            res = self._process(item, slug, rec, site)
            self._nap()
            return res
        except Stopped:
            raise
        except Exception as e:  # noqa: BLE001
            self.log(f"   ! ошибка поста {url}: {e}")
            self._add(errors=1)
            self.last_error = e
            # защита сайта (403/429/503, исчерпанные повторы 429) — вызывающий код может переждать
            self.blocked = (type(e).__name__ == "SiteBlocked"
                            or isinstance(e, requests.exceptions.RetryError))
            return "error"

    def _build_meta(self, site, url, slug, title, post, plan, files, fp, status):
        formats = {"photo": {}, "video": {}}
        for p in plan:
            ext = p["name"].rsplit(".", 1)[-1].lower()
            formats[p["kind"]][ext] = formats[p["kind"]].get(ext, 0) + 1
        norm = post["norm"]
        return {
            "site": site.key, "slug": slug, "url": url, "title": title,
            "section": site.section_of(url, post),
            "categories": post["categories"], "tags": post["tags"],
            "cosplayer": norm["cosplayer"], "character": norm["character"],
            "source": norm["source"], "published": post["published"], "modified": post["modified"],
            "declared": norm["declared"], "formats": formats,
            "archives": post["external"], "video_embeds": post.get("video_embeds", []),
            "password": norm["password"],
            "warnings": post.get("warnings", []),
            "files": files, "total_bytes": sum(f["size_bytes"] for f in files),
            "status": status, "fingerprint": fp, "saved_at": now_iso(),
        }

    def _record(self, item, site, slug, title, post, meta, folder, zipped):
        norm = post["norm"]
        self.lib.save({
            "slug": slug, "url": meta["url"], "title": title, "section": meta["section"],
            "cosplayer": norm["cosplayer"], "character": norm["character"],
            "source": norm["source"], "tags": ", ".join(post["tags"]),
            "listing_sig": item.get("sig"), "fingerprint": meta["fingerprint"],
            "status": meta["status"], "folder": folder, "zipped": int(zipped),
            "files_json": json.dumps(meta["files"], ensure_ascii=False),
            "meta_json": json.dumps(meta, ensure_ascii=False), "total_bytes": meta["total_bytes"],
            "updated_at": now_iso(), "published": post["published"] or post["modified"] or "",
            "site": site.key,
        })

    def _notify(self, slug, result):
        if self.on_post:
            try:
                self.on_post(slug, result)
            except Exception as e:  # noqa: BLE001
                self.log(f"   on_post: {e}")

    def _process(self, item, slug, rec, site):
        url, opt = item["url"], self.opt
        post, page_html = site.fetch_post(self.session, url, opt.keep_cover)
        self.ctl.checkpoint()
        media = []
        if opt.images:
            media += [(c, "photo") for c in post["images"]]
        if opt.videos:
            media += [(c, "video") for c in post["videos"]]
        fp = fingerprint(post)
        title = post["title"] or item.get("title") or slug
        exists = bool(rec) and self._output_exists(rec)

        if rec and rec["status"] in ("done", "meta") and rec["fingerprint"] == fp \
                and exists and not opt.force:
            rec["listing_sig"] = item.get("sig") or rec["listing_sig"]
            self.lib.save(rec)
            self._add(skipped=1)
            return "skipped"

        declared = post["norm"]["declared"].get("photos") or 0
        if (not media and declared > 0) or post.get("warnings"):
            dbg = self.lib.root / f"debug_{slug}.html"
            self.log(f"   ? {'; '.join(post.get('warnings', [])) or 'фото заявлено, но не найдено'}"
                     f" — сохраняю {dbg.name} для отладки")
            dbg.write_text(page_html, encoding="utf-8")

        plan = assign_names(media)
        self.log(f"[{site.key}] {title}: фото={len(post['images'])}, видео={len(post['videos'])}, "
                 f"плееров={len(post.get('video_embeds', []))}, "
                 f"ссылок на архивы={len(post['external'])}")
        if opt.dry_run:
            for p in plan:
                self.log("    " + p["cands"][0])
            for u in post["external"]:
                self.log("    [archive] " + u)
            return "dry"

        # ---- режим «только метаданные»: ничего не качаем и не пишем на диск
        if opt.storage == "meta":
            files = [{"name": p["name"], "kind": p["kind"], "size_bytes": 0, "url": p["cands"][0],
                      "cands": p["cands"]} for p in plan]
            meta = self._build_meta(site, url, slug, title, post, plan, files, fp, "meta")
            self._record(item, site, slug, title, post, meta, "", False)
            result = "updated" if rec else "new"
            self._add(**{result: 1})
            self._notify(slug, result)
            return result

        # ---- режим full: качаем файлы на диск (как раньше)
        have = {}
        if rec and exists and not opt.force:
            have = {f["name"]: f for f in json.loads(rec["files_json"] or "[]")}
        todo = [p for p in plan if p["name"] not in have]
        self._set(file_n=len(todo), file_i=0)

        folder = rec["folder"] if rec and rec["folder"] else self._pick_folder(site, title, slug)
        zip_path, json_path = self._paths(folder, opt.zip)
        work = (self.lib.posts_dir / ".tmp" / slug) if opt.zip else (self.lib.posts_dir / folder)
        if opt.force and zip_path and zip_path.exists():
            zip_path.unlink()
        work.mkdir(parents=True, exist_ok=True)

        got, stopped = {}, False
        with ThreadPoolExecutor(max_workers=opt.workers) as pool:
            futs = [(p, pool.submit(self._download, p, work, site.referer(url))) for p in todo]
            try:
                for p, fut in futs:
                    try:
                        size = fut.result()
                    except Stopped:
                        stopped = True
                        continue
                    if size is not None:
                        got[p["name"]] = {"name": p["name"], "kind": p["kind"],
                                          "size_bytes": size, "url": p["cands"][0]}
            except BaseException:  # Ctrl+C и т.п. — гасим воркеров
                self.ctl.stop()
                raise

        files = [have.get(p["name"]) or got.get(p["name"]) for p in plan]
        files = [f for f in files if f]
        complete = len(files) == len(plan) and not stopped

        if opt.zip:
            if got:
                zip_path.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_STORED) as zf:
                    for name in got:
                        zf.write(work / name, f"{folder}/{name}")
            shutil.rmtree(work, ignore_errors=True)

        meta = self._build_meta(site, url, slug, title, post, plan, files, fp,
                                "done" if complete else "partial")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self._record(item, site, slug, title, post, meta, folder, opt.zip)
        if complete:
            self._notify(slug, "updated" if rec else "new")
        if stopped:
            raise Stopped()
        if not complete:
            self._add(partial=1)
            return "partial"
        result = "updated" if rec else "new"
        self._add(**{result: 1})
        return result

    # ---- загрузка файлов
    def _fetch_file(self, cands, dest, referer, ctl=None, count=True, reuse=False):
        """Скачивает файл из первого рабочего URL. Возвращает размер или None."""
        ctl = ctl or self.ctl
        dest = Path(dest)
        if dest.exists() and dest.stat().st_size > 0 and (reuse or not self.opt.force):
            size = dest.stat().st_size
            if count:
                self._add(file_i=1, files=1, bytes=size)
            return size
        err = None
        for url in cands:
            ctl.checkpoint()
            tmp = dest.with_name(dest.name + ".part")
            try:
                with self.session.get(url, headers={"Referer": referer}, stream=True,
                                      timeout=(10, 60)) as r:
                    if r.status_code == 404:
                        err = "404"
                        continue
                    r.raise_for_status()
                    if "text/html" in r.headers.get("Content-Type", ""):
                        err = "пришёл HTML вместо файла"
                        continue
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            ctl.checkpoint()
                            f.write(chunk)
                tmp.replace(dest)
                size = dest.stat().st_size
                if count:
                    self._add(file_i=1, files=1, bytes=size)
                return size
            except Stopped:
                tmp.unlink(missing_ok=True)
                raise
            except Exception as e:  # noqa: BLE001
                err = e
                tmp.unlink(missing_ok=True)
        self.log(f"   ! не скачалось: {cands[-1]} ({err})")
        return None

    def _download(self, p, work, referer):
        return self._fetch_file(p["cands"], work / p["name"], referer)

    def page_cover(self, url):
        """Байты обложки поста (og:image со страницы) или None. Запасное превью для постов,
        у которых не нашлось ни одной скачиваемой фотографии."""
        try:
            site = self.site_for(url)
            page = site._get(self.session, url)
            tag = BeautifulSoup(page.text, "html.parser").select_one(
                "meta[property='og:image'], meta[name='twitter:image']")
            if not tag or not tag.get("content"):
                return None
            image_url = urljoin(url, tag["content"].strip())
            with self.session.get(image_url, headers={"Referer": site.referer(url)},
                                  timeout=(10, 30), stream=True) as r:
                r.raise_for_status()
                if "image" not in r.headers.get("Content-Type", "image"):
                    return None
                if int(r.headers.get("Content-Length") or 0) > 30 * 1024 * 1024:
                    return None
                return r.content
        except Exception as e:  # noqa: BLE001
            self.log(f"   ! обложка {url}: {e}")
            return None

    def stage_files(self, rec, dest, names=None, workers=4):
        """Временно скачивает файлы поста из записи БД в папку dest (для выдачи через бота).
        names — только эти файлы. Возвращает {имя: размер} успешно скачанных."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        files = json.loads(rec["files_json"] or "[]")
        if names is not None:
            files = [f for f in files if f["name"] in names]
        referer = self.site_for(rec["url"]).referer(rec["url"])
        free = Control()  # пауза/стоп задач парсинга на выдачу файлов не влияют

        def one(f):
            return f["name"], self._fetch_file(f.get("cands") or [f["url"]], dest / f["name"],
                                               referer, ctl=free, count=False, reuse=True)

        done = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for name, size in pool.map(one, files):
                if size is not None:
                    done[name] = size
        return done


# --------------------------------------------------------------------------- превью
def make_preview(data, max_side=1280, quality=85):
    """Байты картинки -> JPEG для Telegram (нужен Pillow; без него — как есть, если < 10 МБ)."""
    raw_ok = len(data) <= 10 * 1024 * 1024 and (
        data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return data if raw_ok else None
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        # Pillow не смог (необычный формат) — отдаём исходное фото как есть, если Telegram его примет
        return data if raw_ok else None


# --------------------------------------------------------------------------- архивы
def resolve_archive(session, url):
    """Ссылка на хостинг -> список (прямая_ссылка, имя_или_None)."""
    sp = urlsplit(url)
    host, path = sp.netloc.lower(), sp.path
    if "ouo." in host:
        raise ValueError("ouo.io закрыт капчей: откройте ссылку в браузере и возьмите итоговую ссылку")
    if "pixeldrain" in host:
        m = re.match(r"^/(u|l)/([\w-]+)", path)
        if not m:
            raise ValueError("непонятная ссылка pixeldrain")
        if m.group(1) == "u":
            return [(f"https://pixeldrain.com/api/file/{m.group(2)}?download", None)]
        r = session.get(f"https://pixeldrain.com/api/list/{m.group(2)}", timeout=30)
        r.raise_for_status()
        return [(f"https://pixeldrain.com/api/file/{f['id']}?download", f["name"])
                for f in r.json()["files"]]
    if "mediafire" in host:
        if "/folder/" in path:
            raise ValueError("папки mediafire не поддерживаются, нужна ссылка на файл")
        r = session.get(url, timeout=30)
        r.raise_for_status()
        m = re.search(r"https?://download\d*\.mediafire\.com/[^\"'<>\s]+", r.text)
        if m:
            return [(htmllib.unescape(m.group(0)), None)]
        scrambled = re.search(r'data-scrambled-url="([^"]+)"', r.text)
        if scrambled:  # новая разметка: ссылка закодирована в base64
            try:
                import base64
                return [(base64.b64decode(scrambled.group(1)).decode(), None)]
            except Exception:  # noqa: BLE001
                pass
        raise ValueError("mediafire: не нашёл прямую ссылку (файл удалён?)")
    return [(url, None)]  # прямая ссылка на файл


def download_file(session, url, out_dir, name=None, referer=None, control=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Referer": referer} if referer else {}
    with session.get(url, headers=headers, stream=True, timeout=(10, 60)) as r:
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            raise ValueError("пришла HTML-страница вместо файла")
        if not name:
            cd = r.headers.get("Content-Disposition", "")
            m = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", cd)
            name = unquote(m.group(1) or m.group(2)) if m else \
                unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        dest = out_dir / safe_name(name)
        if dest.exists():
            print(f"   [skip] {dest.name} уже есть")
            return dest
        tmp = dest.with_name(dest.name + ".part")
        total = int(r.headers.get("Content-Length") or 0)
        done, t0 = 0, time.time()
        try:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if control:
                        control.checkpoint()
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - t0 > 5:
                        t0 = time.time()
                        pct = f" ({done * 100 // total}%)" if total else ""
                        print(f"   {dest.name}: {done >> 20} MB{pct}")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)
        print(f"   -> {dest}")
        return dest


def download_archives(session, links_file, out_dir, control=None):
    """Читает файл со ссылками (по одной в строке) и качает архивы."""
    lines = Path(links_file).read_text(encoding="utf-8").splitlines()
    links = [x.strip() for x in lines if x.strip().startswith("http")]
    print(f"Ссылок на архивы: {len(links)}")
    for i, link in enumerate(links, 1):
        print(f"({i}/{len(links)}) {link}")
        try:
            for direct, name in resolve_archive(session, link):
                download_file(session, direct, out_dir, name, referer=link, control=control)
        except Stopped:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"   ! пропуск: {e}")
        time.sleep(1)


def export_links(lib, query="", field=None):
    """Текст со ссылками на архивы постов из библиотеки (для ручного прохождения капчи)."""
    rows, _ = lib.search(query, field, limit=10 ** 6)
    out = []
    for r in rows:
        meta = json.loads(r["meta_json"] or "{}")
        links = meta.get("archives") or []
        if links:
            out.append(f"# {r['title']}" + (f"  (пароль: {meta['password']})" if meta.get("password") else ""))
            out += links + [""]
    return "\n".join(out)


def summary_text(p):
    return (f"новых: {p.get('new', 0)}, обновлённых: {p.get('updated', 0)}, "
            f"пропущено: {p.get('skipped', 0)}, неполных: {p.get('partial', 0)}, "
            f"ошибок: {p.get('errors', 0)}; файлов: {p.get('files', 0)} "
            f"({human_size(p.get('bytes', 0))})")


def format_row(r):
    bits = [x for x in (r.get("cosplayer"), r.get("source"), r.get("character")) if x]
    return f"{r['title']}" + (f" — {' / '.join(bits)}" if bits else "")


# --------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Парсер косплей-сайтов (mitaku, cosplaytele, galleryepic, "
                                 "xasiat, haneame): метаданные, фото/видео, поиск")
    ap.add_argument("urls", nargs="*", help="ссылка на пост, категорию, тег или поиск")
    ap.add_argument("-o", "--out", default="mitaku_data", help="папка библиотеки")
    ap.add_argument("--storage", choices=["meta", "full"], default="meta",
                    help="meta — только метаданные, файлы не сохраняются (по умолчанию); "
                         "full — качать фото/видео на диск")
    ap.add_argument("--site", help="сайт: mitaku|cosplaytele|galleryepic|xasiat|haneame. "
                    "Без ссылок — обойти раздел этого сайта; для --search/--site-search — фильтр")
    ap.add_argument("--section", default="all", help="раздел сайта (см. README), по умолчанию all")
    ap.add_argument("--dump-html", metavar="URL", help="сохранить сырой HTML страницы в файл "
                    "(чтобы прислать для настройки адаптера)")
    ap.add_argument("--zip", action="store_true", help="(storage=full) упаковывать каждый пост в .zip")
    ap.add_argument("--pages", default="1", help="страницы листинга: 5 (=1-5) или 3-8")
    ap.add_argument("--limit", type=int, default=0, help="максимум постов с листинга")
    ap.add_argument("--images-only", action="store_true")
    ap.add_argument("--videos-only", action="store_true")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--keep-cover", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="только показать найденное")
    ap.add_argument("--force", action="store_true", help="перекачать всё заново")
    ap.add_argument("--recheck", action="store_true", help="открывать каждый пост и сверять")
    ap.add_argument("--until-known", action="store_true",
                    help="остановиться, когда страница листинга целиком уже в библиотеке")
    ap.add_argument("--search", metavar="TEXT", help="поиск по локальной библиотеке")
    ap.add_argument("--field", choices=["all", "cosplayer", "game", "character", "tag"],
                    help="поле для --search / --export-links")
    ap.add_argument("--site-search", metavar="TEXT", help="поиск на сайте mitaku.net")
    ap.add_argument("--stats", action="store_true", help="статистика библиотеки")
    ap.add_argument("--export-links", metavar="FILE", help="выгрузить ссылки на архивы в txt "
                    "(можно вместе с --search)")
    ap.add_argument("--archives", metavar="FILE", help="txt со ссылками на архивы "
                    "(mediafire/pixeldrain/прямые), по одной в строке")
    a = ap.parse_args()

    opt = Options(zip=a.zip, images=not a.videos_only, videos=not a.images_only,
                  workers=a.workers, delay=a.delay, keep_cover=a.keep_cover,
                  dry_run=a.dry_run, force=a.force, recheck=a.recheck,
                  until_known=a.until_known, storage=a.storage)
    sc = Scraper(a.out, opt)
    import mitaku_sites
    if a.site and a.site != "all" and a.site not in mitaku_sites.SITES:
        sys.exit(f"Неизвестный сайт {a.site}. Доступно: {', '.join(mitaku_sites.SITES)}")
    if a.storage == "meta" and (a.urls or a.site):
        print("Режим storage=meta: файлы не сохраняются, только метаданные (--storage full — качать).")

    if a.stats:
        s = sc.lib.stats()
        print(f"Постов: {s['posts']}, косплеерш: {s['cosplayers']}, размер: {human_size(s['bytes'])}")
        print("Сайты:", s["sites"], "| Статусы:", s["status"], "| Разделы:", s["sections"])
    if a.search:
        rows, total = sc.lib.search(a.search, a.field, limit=50,
                                    site=a.site if a.site != "all" else None)
        print(f"Найдено: {total}")
        for r in rows:
            print(f" - [{r['site']}] {format_row(r)}  [{r['status']}]  {r['url']}")
    if a.site_search:
        sites = [a.site] if a.site and a.site != "all" else None
        for it in sc.site_search(a.site_search, sites=sites):
            print(f" - [{it['site']}] {it['title']}  {it['url']}")
        for key, err in sc.search_errors:
            print(f" ! {key}: {err}")
    if a.dump_html:
        site = sc.site_for(a.dump_html)
        r = site._get(sc.session, a.dump_html)
        name = f"dump_{site.key}_{safe_name(slug_of(a.dump_html))}.html"
        Path(name).write_text(r.text, encoding="utf-8")
        print(f"HTML сохранён: {name} ({len(r.text) // 1024} КБ)")
    if a.export_links:
        text = export_links(sc.lib, a.search or "", a.field)
        Path(a.export_links).write_text(text, encoding="utf-8")
        print(f"Ссылки записаны в {a.export_links}")
    if a.archives:
        download_archives(sc.session, a.archives, Path(a.out) / "archives")
    urls = list(a.urls)
    if not urls and a.site and a.site != "all":
        secs = mitaku_sites.SITES[a.site].sections()
        if a.section not in secs:
            sys.exit(f"Нет раздела {a.section}. Доступно: {', '.join(secs)}")
        urls = [secs[a.section]]
    if urls:
        s, _, e = a.pages.partition("-")
        first, last = (int(s), int(e)) if e else (1, int(s))
        try:
            res = sc.sync(urls, first, last, a.limit)
        except KeyboardInterrupt:
            print("\nПрервано. Запустите снова — продолжу с того же места.")
            return 130
        print("\nГотово:", summary_text(res))
    return 0


if __name__ == "__main__":
    sys.modules.setdefault("mitaku_parser", sys.modules[__name__])  # mitaku_sites импортирует его
    sys.exit(main())
