"""Публикация в канал: текст поста, комментарии, правила, очередь с паузой, автопубликация."""
import asyncio
import itertools
import json
import logging
import mitaku_parser as mp
import mitaku_sites as ms
import re
import shutil
import time
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaDocument, InputMediaPhoto, Message, ReplyParameters
from contextlib import suppress
from . import core, delivery, importer, postfiles, util

AUTO_FWD: dict = {}         # id поста в канале -> (id группы обсуждения, id сообщения-копии)


@core.channel_router.message(F.is_automatic_forward)
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


def site_title(key):
    """Название сайта для людей — без адреса и пометок вроде 🧪."""
    site = ms.SITES.get(key)
    return (site.title.replace("🧪", "").strip() if site else (key or "Источник"))


def tags_line(tags, budget):
    out, size = [], 3
    for tag in tags[:12]:
        tag = re.sub(r"\W+", "_", tag).strip("_")
        if tag and size + len(tag) + 2 <= budget:
            out.append("#" + tag)
            size += len(tag) + 2
    return ("🏷 " + " ".join(out)) if out else ""


def channel_text(rec):
    """Пост в канале: название, косплеер, персонаж, игра/аниме, число фото, теги, архивы, источник
    (название сайта без адреса). Фото, видео и архивы — в комментариях."""
    m = json.loads(rec["meta_json"] or "{}")
    files = postfiles.post_files(rec)
    n_ph = (sum(1 for f in files if f["kind"] == "photo")
            or sum(m.get("formats", {}).get("photo", {}).values()))
    archives, players = post_archives(rec)
    n_vid = (sum(1 for f in files if f["kind"] == "video")
             or sum(m.get("formats", {}).get("video", {}).values())) + len(players)
    head = [f"<b>{core.esc(rec['title'])}</b>", ""]
    for icon, label, key in (("👤", "Косплеер", "cosplayer"), ("🎭", "Персонаж", "character"),
                             ("🎮", "Игра / аниме", "source")):
        if m.get(key):
            head.append(f"{icon} <b>{label}:</b> {core.esc(m[key])}")
    counts = ([f"🖼 Фото: {n_ph}"] if n_ph else []) + ([f"🎬 Видео: {n_vid}"] if n_vid else [])
    if counts:
        head.append(" · ".join(counts))
    tail = []
    if archives:
        tail.append("🗂 <b>Архивы:</b> " + " · ".join(
            f'<a href="{core.esc(u)}">Архив {i}</a>' for i, u in enumerate(archives[:6], 1)))
    tail += ["", f"📍 <b>Источник:</b> {core.esc(site_title(rec.get('site')))}",
             "⬇️ Фото и архивы — в комментариях"]
    budget = 1000 - len("\n".join(head + tail)) - 1
    tags = tags_line(m.get("tags", []), budget)
    return util._fit(head + ([tags] if tags else []) + tail, 1000)


async def send_comments(group_id, reply_to, rec, settings, stats=None, progress=None, save=None):
    """Фото (альбомами), видео и ZIP под постом. progress — уже отправленное ({photo_chunks, videos,
    zip}); save() вызывается после каждого шага, поэтому прерванную отправку можно продолжить."""
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
    files = [f for f in postfiles.post_files(rec) if postfiles.file_exists(rec, f)]
    photos = [f for f in files if f["kind"] == "photo"]
    videos = [f for f in files if f["kind"] == "video"]
    if stats is None:
        stats = {}
    stats.update({"photos": 0, "videos": 0, "zips": 0})   # заполняется по ходу: видно и при сбое
    progress = {} if progress is None else progress

    def checkpoint():
        if save:
            save()

    async def send_documents(chunk):
        docs = [InputMediaDocument(media=postfiles.input_file(rec, f)) for f in chunk]
        if len(docs) == 1:
            await delivery.tg_call(core.BOT.send_document, chat_id=group_id, document=docs[0].media,
                          reply_parameters=rp)
        else:
            await delivery.tg_call(core.BOT.send_media_group, chat_id=group_id, media=docs, reply_parameters=rp)

    chunks = [photos[i:i + 10] for i in range(0, len(photos), 10)]
    done_chunks = progress.get("photo_chunks", 0)
    for ci, chunk in enumerate(chunks):
        if ci < done_chunks:                  # этот альбом уже отправлен раньше
            stats["photos"] += len(chunk)
            continue
        if settings.get("channel_media") == "documents":
            await send_documents(chunk)
            stats["photos"] += len(chunk)
        else:
            media, rest = [], []
            for f in chunk:
                data = await asyncio.to_thread(postfiles.photo_bytes, rec, f)
                if data:
                    media.append(InputMediaPhoto(media=BufferedInputFile(data, postfiles.jpg_name(f["name"]))))
                else:
                    rest.append(f)   # не удалось сжать до лимита — отправим файлом
            sent = 0
            try:
                if len(media) == 1:
                    await delivery.tg_call(core.BOT.send_photo, chat_id=group_id, photo=media[0].media,
                                  reply_parameters=rp)
                    sent = 1
                elif media:
                    await delivery.tg_call(core.BOT.send_media_group, chat_id=group_id, media=media,
                                  reply_parameters=rp)
                    sent = len(media)
            except TelegramBadRequest as e:   # например, недопустимые размеры фото
                logging.warning("Альбом %s отклонён (%s) — отправляю файлами", rec["slug"], e)
                rest = chunk
            stats["photos"] += sent
            for f in rest:
                await delivery.tg_call(core.BOT.send_document, chat_id=group_id, reply_parameters=rp,
                              document=postfiles.input_file(rec, f))
                stats["photos"] += 1
        progress["photo_chunks"] = ci + 1
        checkpoint()
        await asyncio.sleep(core.SEND_PAUSE)

    if settings.get("channel_videos"):
        for vi, f in enumerate(videos):
            if vi < progress.get("videos", 0):
                stats["videos"] += 1
                continue
            size = postfiles.post_file_size(rec, f)
            if size <= core.MAX_UPLOAD:
                if size <= core.BOTAPI_UPLOAD:
                    await delivery.tg_call(core.BOT.send_video, chat_id=group_id, reply_parameters=rp,
                                  video=postfiles.input_file(rec, f), caption=core.esc(f["name"]),
                                  supports_streaming=True)
                else:   # видео больше 50 МБ — через MTProto
                    with postfiles.materialize_post_file(rec, f) as video_path:
                        await delivery.send_file_any(group_id, video_path, caption=core.esc(f["name"]), rp=rp,
                                            video=True, name=f["name"])
                stats["videos"] += 1
                await asyncio.sleep(core.SEND_PAUSE)
            else:  # видео больше лимита: ZIP-частями
                stats["zips"] += await delivery.send_parts(group_id, rp, rec, [f], f["name"])
                stats["videos"] += 1
            progress["videos"] = vi + 1
            checkpoint()

    if settings.get("channel_videos") and settings.get("download_embedded_video"):
        for ei, url in enumerate(post_archives(rec)[1]):   # видео из плееров (yt-dlp)
            if ei < progress.get("embeds", 0):
                stats["videos"] += 1
                continue
            try:
                path = await delivery.download_video(url, rec)
            except Exception as e:  # noqa: BLE001
                logging.warning("Видео из плеера %s: %s", url, e)
                stats["video_errors"] = stats.get("video_errors", 0) + 1
            else:
                try:
                    await delivery.send_file_any(group_id, path, caption=core.esc(archive_label(rec)), rp=rp,
                                        video=True, name=f"{mp.safe_name(archive_label(rec))}{path.suffix}")
                    stats["videos"] += 1
                finally:
                    shutil.rmtree(path.parent, ignore_errors=True)
            progress["embeds"] = ei + 1
            checkpoint()

    if settings.get("channel_zip") and files and not progress.get("zip"):
        total = sum(postfiles.post_file_size(rec, f) for f in files)
        limit = int(settings.get("zip_max_mb") or 0) * core.MB
        if limit and total > limit:           # ZIP слишком тяжёлый для канала — пропускаем
            stats["zip_skipped_mb"] = round(total / core.MB)
        else:
            stats["zips"] += await delivery.send_parts(group_id, rp, rec, None, "архив")
        progress["zip"] = True
        checkpoint()
    return stats


class NotPublishable(Exception):
    """Пост нельзя публиковать в канал по правилам (видео, только архивы…): он уже записан в историю."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def post_archives(rec):
    """(ссылки на архивы, ссылки на видео-плееры и потоки). Плееры берутся из video_embeds (новый
    разбор) и по хосту из «архивов» (посты, разобранные до появления video_embeds)."""
    meta = json.loads(rec["meta_json"] or "{}")
    links = meta.get("archives", [])
    players = list(meta.get("video_embeds", []))
    players += [u for u in links if mp.is_video_host(u) and u not in players]
    return [u for u in links if not mp.is_video_host(u)], players


def archive_label(rec):
    """Подпись и имя архива: «Ник косплеера – на что косплей (игра/аниме)».
    «На что» — персонаж; если его нет, берётся название поста без ника и скобки с игрой."""
    m = json.loads(rec["meta_json"] or "{}")
    cos = (m.get("cosplayer") or rec.get("cosplayer") or "").strip()
    what = (m.get("character") or "").strip()
    if not what:
        title = rec["title"] or ""
        if cos:
            title = re.sub(rf"^\s*{re.escape(cos)}\s*[-–—:|]\s*", "", title, flags=re.I)
        what = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
    game = (m.get("source") or "").strip()
    label = cos
    if what and what.lower() != cos.lower():
        label = f"{label} – {what}" if label else what
    if game and game.lower() not in label.lower():
        label += f" ({game})"
    return (label or rec["title"] or rec["slug"])[:150]


def count_photos(rec):
    return sum(1 for f in postfiles.post_files(rec) if f["kind"] == "photo")


def publish_verdict(rec, settings):
    """('ok', '') или (статус_истории, причина): можно ли публиковать пост в канал.
    Посты с видео и посты, где есть только ссылки на архивы, в канал и в комментарии не идут."""
    files = postfiles.post_files(rec)
    videos = [f for f in files if f["kind"] == "video"]
    archives, players = post_archives(rec)
    if (videos or players) and not settings.get("publish_video_posts"):
        return "skip_video", f"в посте видео ({len(videos) + len(players)}) — в канал не публикуется"
    if not count_photos(rec):
        if archives:
            return ("skip_archives",
                    f"в посте только ссылки на архивы ({len(archives)}) — бот их не публикует")
        return "skip_empty", "в посте не нашлось фото"
    return "ok", ""


def publish_check(rec, settings, force=False):
    """publish_verdict + проверка дубликатов (тот же сет уже опубликован в канале — с этого или
    другого сайта). force пропускает только проверку дубликатов (явная команда админа)."""
    status, why = publish_verdict(rec, settings)
    if status != "ok" or force or not settings.get("skip_duplicates", True):
        return status, why
    dup = core.jm.lib.find_duplicate_of(rec)
    if dup:
        return "skip_duplicate", (f"дубликат: уже опубликован «{dup['title']}» "
                                  f"({site_title(dup['site'])}) — /dups")
    return "ok", ""


def record_verdict(rec, status, detail):
    core.jm.lib.hist_set(rec["slug"], status, detail, title=rec["title"],
                    cosplayer=rec.get("cosplayer") or "", site=rec.get("site") or "",
                    photos_total=count_photos(rec), photos_sent=0)


def comments_saver(slug, progress):
    def save():
        core.jm.lib.kv_set(f"cprog:{slug}", json.dumps(progress))
    return save


async def deliver_comments(rec, group_id, reply_to, settings, info, progress):
    """Комментарии под постом с записью результата в историю (full / partial)."""
    slug = rec["slug"]
    stats = {}
    try:
        async with postfiles.staged(rec):  # режим meta: на время отправки скачиваем файлы, потом удаляем
            await send_comments(group_id, reply_to, rec, settings, stats, progress,
                                comments_saver(slug, progress))
    except Exception as e:
        core.jm.lib.hist_set(slug, "partial", f"комментарии прерваны: {e}"[:300],
                        photos_sent=stats.get("photos", 0), **info)
        raise
    detail = (f"фото {stats['photos']}/{info['photos_total']}, видео {stats['videos']}, "
              f"ZIP {stats['zips']}")
    if stats.get("video_errors"):
        detail += f", видео из плеера не скачалось: {stats['video_errors']}"
    if stats.get("zip_skipped_mb"):
        detail += f" (ZIP пропущен: {stats['zip_skipped_mb']} МБ больше лимита {settings.get('zip_max_mb')} МБ)"
    core.jm.lib.hist_set(slug, "full" if stats["photos"] >= info["photos_total"] else "partial",
                    detail, photos_sent=stats["photos"], **info)
    return detail


async def publish_post(slug, force=False, overrides=None):
    """Пост в канал + фото/ZIP (и видео, если разрешено) в комментарии. Итог пишется в историю.
    overrides — разовые настройки поверх /set (например, без ZIP при импорте).
    Возвращает текст-итог; NotPublishable — пост по правилам не публикуется."""
    rec = core.jm.lib.get(slug)
    if not rec:
        raise RuntimeError("пост не найден в библиотеке")
    if not core.CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID не задан")
    if not force and core.jm.lib.kv_get(f"published:{slug}"):
        return "уже опубликован"
    settings = {**core.jm.get_settings(), **(overrides or {})}
    status, why = publish_check(rec, settings, force is True)
    if status != "ok":
        record_verdict(rec, status, why)
        raise NotPublishable(status, why)
    info = {"title": rec["title"], "cosplayer": rec.get("cosplayer") or "",
            "site": rec.get("site") or "", "photos_total": count_photos(rec)}
    chat = await core.BOT.get_chat(core.CHANNEL_ID)
    group_id = getattr(chat, "linked_chat_id", None)

    # Пост всегда с превью: фото -> фото по ссылке -> обложка страницы -> ссылочное превью
    text = channel_text(rec)
    try:
        msg = await delivery.send_with_preview(core.BOT, chat.id, rec, text, text)
    except Exception as e:
        core.jm.lib.hist_set(slug, "error", f"пост не отправлен: {e}"[:300], photos_sent=0, **info)
        raise
    core.jm.lib.kv_set(f"published:{slug}", str(msg.message_id))
    core.jm.lib.kv_set(f"cprog:{slug}", "{}")
    core.jm.lib.kv_set(f"fwd:{slug}", "")
    info["msg_id"] = msg.message_id
    core.jm.lib.hist_set(slug, "partial", "пост в канале, комментарии отправляются…", photos_sent=0, **info)
    if not group_id:
        why = "пост в канале, но у канала нет группы обсуждения — комментарии пропущены"
        core.jm.lib.hist_set(slug, "post_only", why, photos_sent=0, **info)
        return why
    fwd = await wait_forward(msg.message_id)
    if not fwd:
        why = ("пост в канале, но копия в группе обсуждения не появилась — бот должен быть "
               "администратором группы (или отключите Privacy mode у @BotFather)")
        core.jm.lib.hist_set(slug, "post_only", why, photos_sent=0, **info)
        return why
    core.jm.lib.kv_set(f"fwd:{slug}", f"{fwd[0]}:{fwd[1]}")   # нужно, чтобы позже дослать комментарии
    return await deliver_comments(rec, fwd[0], fwd[1], settings, info, {})


async def resume_comments(slug, overrides=None):
    """Досылает недостающее под уже опубликованным постом: с того шага, где отправка прервалась."""
    rec = core.jm.lib.get(slug)
    if not rec:
        raise RuntimeError("пост не найден в библиотеке")
    fwd = core.jm.lib.kv_get(f"fwd:{slug}")
    if not fwd:
        raise RuntimeError("нет данных о комментариях под постом — используйте «Переопубликовать»")
    group_id, reply_to = (int(x) for x in fwd.split(":"))
    settings = {**core.jm.get_settings(), **(overrides or {})}
    h = core.jm.lib.hist_get(slug) or {}
    info = {"title": rec["title"], "cosplayer": rec.get("cosplayer") or "",
            "site": rec.get("site") or "", "photos_total": count_photos(rec),
            "msg_id": h.get("msg_id") or ""}
    progress = json.loads(core.jm.lib.kv_get(f"cprog:{slug}") or "{}")
    return await deliver_comments(rec, group_id, reply_to, settings, info, progress)


async def republish_post(slug, overrides=None):
    """Удаляет старый пост из канала и публикует заново (если дослать комментарии нельзя).
    Комментарии прежней версии в группе обсуждения остаются."""
    h = core.jm.lib.hist_get(slug) or {}
    old = h.get("msg_id") or core.jm.lib.kv_get(f"published:{slug}")
    if old and str(old).isdigit():
        chat = await core.BOT.get_chat(core.CHANNEL_ID)
        with suppress(Exception):
            await core.BOT.delete_message(chat.id, int(old))
    for key in ("published", "fwd", "cprog"):
        core.jm.lib.kv_set(f"{key}:{slug}", "")
    return await publish_post(slug, True, overrides)


# --------------------------------------------------------------------------- очередь публикации
# Элементы очереди: (slug, force, batch_id). Между публикациями выдерживается пауза
# publish_delay (настройка), чтобы Telegram не ограничил бота за флуд.
PUBLISH_PENDING: set = set()    # slug-и, стоящие в очереди или публикуемые сейчас
BATCHES: dict = {}              # массовые публикации: id -> {chat, label, total, left, ok, skipped, failed}
_batch_ids = itertools.count(1)


def enqueue_publish(slug, force=False, batch=None):
    """Ставит пост в очередь публикации. False — он уже в очереди."""
    if slug in PUBLISH_PENDING:
        return False
    PUBLISH_PENDING.add(slug)
    core.PUBLISH_Q.put_nowait((slug, force, batch))
    return True


def clear_publish_queue():
    removed = 0
    while True:
        try:
            core.PUBLISH_Q.get_nowait()
            removed += 1
        except asyncio.QueueEmpty:
            break
    PUBLISH_PENDING.clear()   # публикатор после ожидания увидит, что слаг снят, и пропустит его
    BATCHES.clear()
    return removed


def publish_candidates(slugs):
    """Из slug-ов оставляет те, что можно публиковать (есть в библиотеке, не опубликованы, без видео
    и не «только архивы»). Остальные записываются в историю с причиной."""
    done = {k.split(":", 1)[1] for k in core.jm.lib.kv_list("published:")}
    settings = core.jm.get_settings()
    out = []
    recs = core.jm.lib.get_many([s for s in slugs if s not in done])   # один запрос вместо запроса на пост
    for slug in slugs:
        if slug in done:
            continue
        rec = recs.get(slug)
        if not rec:
            continue
        status, why = publish_check(rec, settings)
        if status == "ok":
            out.append(slug)
        else:
            record_verdict(rec, status, why)
    return out


async def start_batch(chat_id, slugs, label, mode=False):
    """Массовая публикация: все подходящие посты — в очередь (от старых к новым). Возвращает число.
    mode="resume" — дослать комментарии (список slug-ов уже отобран)."""
    if mode is False:
        slugs = await asyncio.to_thread(publish_candidates, slugs)
    if not slugs:
        return 0
    bid = next(_batch_ids)
    batch = {"chat": chat_id, "label": label, "total": 0, "left": 0, "ok": 0, "skipped": 0,
             "failed": []}
    BATCHES[bid] = batch
    for slug in slugs:
        if enqueue_publish(slug, mode, bid):
            batch["total"] += 1
            batch["left"] += 1
    if not batch["total"]:
        BATCHES.pop(bid, None)
    return batch["total"]


async def start_batch_and_notify(chat_id, slugs, label):
    n = await start_batch(chat_id, slugs, label)
    await core.BOT.send_message(
        chat_id, f"📣 В очередь публикации поставлено постов: {n}. Ход — в /channel." if n
        else "Нечего публиковать: все найденные посты уже опубликованы или без файлов.")


async def finish_batch_item(bid, status, detail=""):
    batch = BATCHES.get(bid)
    if not batch:
        return
    if status == "ok":
        batch["ok"] += 1
    elif status == "skipped":
        batch["skipped"] += 1
    else:
        batch["failed"].append(detail)
    batch["left"] -= 1
    if batch["left"] <= 0:
        BATCHES.pop(bid, None)
        text = (f"📣 Массовая публикация «{core.esc(batch['label'])}» завершена: опубликовано "
                f"{batch['ok']}, пропущено {batch['skipped']}, ошибок {len(batch['failed'])}.")
        if batch["failed"]:
            text += "\n" + "\n".join(f"❌ {core.esc(x)}" for x in batch["failed"][:8])
        await delivery.notify_admins(text)


async def publish_paced(slug, force=False, should_stop=None, overrides=None):
    """Публикация с паузой publish_delay (×множитель при флуд-лимитах Telegram) после предыдущей;
    общая для очереди и импорта. force: False | True | "resume" (дослать комментарии) |
    "republish" (удалить и опубликовать заново). None — отменено (should_stop() вернул True);
    NotPublishable — пост по правилам не публикуется."""
    special = force in ("resume", "republish")
    if not special and not force and await asyncio.to_thread(core.jm.lib.kv_get, f"published:{slug}"):
        return "уже опубликован"
    rec = await asyncio.to_thread(core.jm.lib.get, slug)
    settings = await asyncio.to_thread(core.jm.get_settings)
    if rec and force != "resume":   # нельзя публиковать — решаем сразу, не дожидаясь паузы
        status, why = publish_check(rec, {**settings, **(overrides or {})}, force is True)
        if status != "ok":
            await asyncio.to_thread(record_verdict, rec, status, why)
            raise NotPublishable(status, why)
    async with core.PUBLISH_LOCK:
        delay = settings["publish_delay"] * delivery.flood_factor()
        while core.LAST_PUBLISH:
            wait = core.LAST_PUBLISH + delay - time.monotonic()
            if wait <= 0:
                break
            if should_stop and should_stop():
                return None
            await asyncio.sleep(min(wait, 1.0))
        if should_stop and should_stop():
            return None
        try:
            if force == "resume":
                return await resume_comments(slug, overrides)
            if force == "republish":
                return await republish_post(slug, overrides)
            return await publish_post(slug, force, overrides)
        finally:
            core.LAST_PUBLISH = time.monotonic()


async def publisher():
    while True:
        slug, force, bid = await core.PUBLISH_Q.get()
        title, status, detail = slug, "ok", ""
        try:
            rec = await asyncio.to_thread(core.jm.lib.get, slug)
            title = rec["title"] if rec else slug
            detail = await publish_paced(slug, force, should_stop=lambda s=slug: s not in PUBLISH_PENDING)
            if detail is None:   # очередь очистили, пока ждали паузу
                status, detail = "cancelled", "отменено"
            elif detail == "уже опубликован":
                status = "skipped"
        except NotPublishable as e:
            status, detail = "skipped", e.detail
        except Exception as e:  # noqa: BLE001
            logging.exception("Публикация %s", slug)
            status, detail = "error", f"{title}: {e}"
        finally:
            PUBLISH_PENDING.discard(slug)
        if bid:
            await finish_batch_item(bid, status if status != "cancelled" else "skipped", detail)
        elif status == "error":
            await delivery.notify_admins(f"❌ Публикация {core.esc(slug)} не удалась: {core.esc(detail)}")
        elif status != "cancelled":
            await delivery.notify_admins(f"📣 {core.esc(title)}: {core.esc(detail)}")
        if status == "error":
            await asyncio.sleep(2)   # пауза между публикациями уже задаёт publish_delay


async def watch_tick():
    """Один проход: для каждого активного сайта — проверка первых страниц на новые посты."""
    if importer.import_active() or core.jm.status()["state"] != "idle" or core.jm.q.qsize():
        return False
    admin = sorted(core.ADMINS)[0]
    for key in core.jm.get_active_sites():
        baseline = core.jm.lib.kv_get(f"baseline:{key}") == "1"
        # Первая проверка сайта только запоминает посты (baseline), ничего не публикуя.
        core.jm.submit([ms.SITES[key].sections()["all"]], first=1, last=3, chat_id=admin,
                  silent=True, until_known=True, publish=baseline, extra={"baseline_site": key})
    return True


async def watcher():
    last = 0.0
    while True:
        await asyncio.sleep(30)
        s = core.jm.get_settings()
        if core.CHANNEL_ID and s["channel"] and time.time() - last >= s["channel_interval"] * 60:
            if await watch_tick():
                last = time.time()
