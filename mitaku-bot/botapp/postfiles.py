"""Файлы постов: временные папки (staging), чтение, ZIP-части, превью."""
import asyncio
import json
import logging
import mitaku_parser as mp
import shutil
import tempfile
import zipfile
from aiogram.types import BufferedInputFile, FSInputFile
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from . import core, publishing

def post_files(r):
    return json.loads(r["files_json"] or "[]")


def posts_root():
    return (Path(core.OUT) / "posts").resolve()


def post_archive_path(r):
    root = posts_root()
    path = (root / f"{r['folder']}.zip").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Некорректный путь к архиву поста")
    return path


def local_copy_exists(r):
    """Лежат ли файлы поста на этом сервере (режим full)."""
    if not r.get("folder"):
        return False
    try:
        if r["zipped"]:
            return post_archive_path(r).is_file()
        return (posts_root() / r["folder"]).is_dir()
    except (OSError, ValueError):
        return False


def is_meta(r):
    """Файлов нет на этом сервере (режим meta или локальный архив, которого нет на Render):
    тогда они скачиваются с сайта во временную папку."""
    return r.get("status") == "meta" or not local_copy_exists(r)


def in_zip(r):
    return bool(r["zipped"]) and not is_meta(r)


def stage_dir(r):
    return core.STAGING / mp.safe_name(r["slug"])


def post_file_path(r, item):
    if is_meta(r):  # файлы не хранятся: берём из временной папки (после stage_post)
        root = core.STAGING.resolve()
        path = (stage_dir(r) / item["name"]).resolve()
    else:
        root = posts_root()
        path = (root / r["folder"] / item["name"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Некорректный путь к файлу поста")
    return path


STAGE_LOCKS = defaultdict(asyncio.Lock)
STAGE_REFS = defaultdict(int)   # сколько операций сейчас пользуются временными файлами поста


async def stage_post(r, names=None):
    """Режим meta: скачивает файлы поста во временную папку. В режиме full ничего не делает."""
    if is_meta(r):
        workers = core.jm.get_settings().get("workers", 4)
        await asyncio.to_thread(core.jm.scraper.stage_files, r, stage_dir(r), names, workers)


def unstage_post(r):
    if is_meta(r):
        shutil.rmtree(stage_dir(r), ignore_errors=True)


@asynccontextmanager
async def staged(r, names=None):
    """with staged(r): временно скачать файлы -> использовать -> удалить (режим meta)."""
    # Вложенные вызовы для одного поста допустимы (раньше они блокировали друг друга навсегда):
    # блокировка держится только на время скачивания, а папка удаляется, когда ушёл последний.
    slug = r["slug"]
    async with STAGE_LOCKS[slug]:
        try:
            await stage_post(r, names)
        except BaseException:
            if not STAGE_REFS[slug]:
                unstage_post(r)
            raise
        STAGE_REFS[slug] += 1
    try:
        yield
    finally:
        async with STAGE_LOCKS[slug]:
            STAGE_REFS[slug] -= 1
            if STAGE_REFS[slug] <= 0:
                STAGE_REFS.pop(slug, None)
                unstage_post(r)


@contextmanager
def open_post_file(r, item):
    if in_zip(r):
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
    if in_zip(r):
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
    if not in_zip(r):
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
    prefix = mp.safe_name(publishing.archive_label(r))   # «Ник – персонаж (игра)»

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
        if size > core.ZIP_PART_SIZE:
            flush()
            part_count = 0
            with open_post_file(r, item) as source:
                while True:
                    path = next_part()
                    next_number = part_count + 1
                    part_name = f"{item['name']}.{next_number:03d}"
                    written = 0
                    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
                        with archive.open(part_name, "w") as target:
                            while written < core.ZIP_PART_SIZE:
                                chunk = source.read(min(1024 * 1024, core.ZIP_PART_SIZE - written))
                                if not chunk:
                                    break
                                target.write(chunk)
                                written += len(chunk)
                        if written:
                            archive.writestr(
                                "README.txt",
                                "This file is split into numbered .001/.002/... files. "
                                "Extract all ZIP parts into one folder, then join them: "
                                f"copy /b \"{item['name']}.0*\" \"{item['name']}\" "
                                "(or open the .001 file in 7-Zip).\n")
                    if not written:
                        path.unlink()
                        break
                    part_count = next_number
                    parts.append(path)
                    if written < core.ZIP_PART_SIZE:
                        break
            split_files.append((item["name"], part_count))
            continue
        if current and current_size + size > core.ZIP_PART_SIZE:
            flush()
        current.append(item)
        current_size += size
    flush()
    return temp_dir, parts, split_files


def load_preview(r, item, max_side=1280):
    """JPEG-превью фото поста (Pillow, если установлен). None — если не получилось."""
    if post_file_size(r, item) > 60 * core.MB:
        return None
    with open_post_file(r, item) as source:
        return mp.make_preview(source.read(), max_side=max_side)


def jpg_name(name):
    return Path(name).stem + ".jpg"


def file_exists(r, item):
    try:
        return post_file_size(r, item) > 0
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def photo_urls(r):
    """Ссылки на фото поста на сайте (Telegram умеет скачивать картинку по ссылке сам)."""
    out = []
    for item in post_files(r):
        if item["kind"] == "photo":
            for url in (item.get("cands") or [item.get("url")]):
                if url:
                    out.append(url.split("#")[0])
    return out


async def get_preview(r, tries=6):
    """(jpeg_bytes, имя) — превью поста. Пробует фото по очереди (первое, которое удалось
    получить и открыть), затем обложку со страницы поста. None — если не вышло ничего."""
    photos = [f for f in post_files(r) if f["kind"] == "photo"][:tries]
    for item in photos:
        try:
            async with staged(r, {item["name"]}):
                data = (await asyncio.to_thread(load_preview, r, item)
                        if file_exists(r, item) else None)
        except Exception as e:  # noqa: BLE001
            logging.warning("Превью %s: фото %s не получилось: %s", r["slug"], item["name"], e)
            continue
        if data:
            return data, jpg_name(item["name"])
    try:
        cover = await asyncio.to_thread(core.jm.scraper.page_cover, r["url"])
        data = mp.make_preview(cover) if cover else None
        if data:
            return data, "cover.jpg"
    except Exception as e:  # noqa: BLE001
        logging.warning("Превью %s: обложка не получилась: %s", r["slug"], e)
    return None


def photo_bytes(rec, item):
    """Фото для альбома: JPEG, который Telegram примет (≤10 МБ, разумные размеры)."""
    for side, quality in ((2560, 90), (1920, 85), (1280, 80)):
        with open_post_file(rec, item) as source:
            out = mp.make_preview(source.read(), max_side=side, quality=quality)
        if out and len(out) <= 9.5 * core.MB:
            return out
    return None


def input_file(rec, item):
    """Файл поста для отправки: с диска/временной папки или из локального zip."""
    if in_zip(rec):
        data = read_post_file(rec, item, 50 * core.MB)
        if data is None:
            raise ValueError(f"файл {item['name']} слишком велик для отправки из архива")
        return BufferedInputFile(data, item["name"])
    return FSInputFile(post_file_path(rec, item), filename=item["name"])
