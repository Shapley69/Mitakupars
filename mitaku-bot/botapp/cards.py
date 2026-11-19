"""Карточка поста, список файлов, кнопки карточки (архивы, видео, перепроверка, ZIP)."""
import asyncio
import json
import math
import mitaku_parser as mp
import mitaku_sites as ms
import shutil
import video_fetch
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from filesplit import join_hint
from ouo_downloader import CAPTCHA_HELP, extract_archive_urls
from urllib.parse import urlparse
from . import archive_links, core, delivery, postfiles, publishing, sync_ui, ui, util

def card(r, short=False, note=None):
    """short=True — подпись к фото (лимит Telegram 1024 символа). note — строка под заголовком."""
    m = json.loads(r["meta_json"] or "{}")
    d = m.get("declared", {})
    lines = [f"<b>{core.esc(r['title'])}</b>"]
    site = ms.SITES.get(r.get("site"))
    if site:
        lines.append(f"Сайт: {core.esc(site.title)}")
    for label, key in (("Косплеер", "cosplayer"), ("Персонаж", "character"), ("Игра/аниме", "source")):
        if m.get(key):
            lines.append(f"{label}: {core.esc(m[key])}")
    if m.get("tags"):
        lines.append("Теги: " + core.esc(", ".join(m["tags"])[:200]))
    if d.get("content"):
        lines.append(f"Заявлено: {core.esc(d['content'])}")
    for label, key in (("Разрешение", "resolution"), ("Длительность", "duration"),
                       ("Размер архива", "file_size")):
        if d.get(key):
            lines.append(f"{label}: {core.esc(d[key])}")
    fm = m.get("formats", {})
    fmt = [f"{k} {ext}×{n}" for k, v in fm.items() for ext, n in v.items()]
    if r["status"] == "meta":
        lines.append(f"Найдено на сайте: {len(m.get('files', []))} файлов"
                     + (f" ({', '.join(fmt)})" if fmt else "") + " — на ПК не хранятся")
    else:
        lines.append(f"Скачано: {len(m.get('files', []))} файлов, {mp.human_size(r['total_bytes'])}"
                     + (f" ({', '.join(fmt)})" if fmt else ""))
    for w in m.get("warnings", [])[:2]:
        lines.append(f"⚠️ {core.esc(w)}")
    if r["status"] == "partial":
        lines.append("⚠️ Скачано не полностью — /continue или кнопка «Перепроверить»")
    if m.get("password"):
        lines.append(f"Пароль архива: <code>{core.esc(m['password'])}</code>")
    archives, players = publishing.post_archives(r)
    vfiles = [f for f in postfiles.post_files(r) if f["kind"] == "video"]
    if vfiles or players or d.get("videos"):
        lines.append(f"🎬 Видео: файлов {len(vfiles)}, в плеерах {len(players)}"
                     + ("" if vfiles or players else " — заявлено на сайте, но не найдено"))
    if archives and not short:
        lines.append("Архивы:\n" + "\n".join(core.esc(a) for a in archives[:8]))
    if players and not short:
        lines.append("Плееры:\n" + "\n".join(core.esc(a) for a in players[:4]))
    lines.append(f"Опубликовано: {core.esc((m.get('published') or '')[:10])}  ·  "
                 f"<a href=\"{core.esc(r['url'])}\">пост на сайте</a>")
    if note:
        lines.insert(1, note)
    return util._fit(lines, 1000 if short else 4000)


def media_caption(r, filename=None):
    lines = [f"<b>{core.esc(r['title'])}</b>"]
    for label, key in (("Косплеер", "cosplayer"), ("Персонаж", "character"),
                       ("Игра/аниме", "source")):
        if r.get(key):
            lines.append(f"{label}: {core.esc(r[key])}")
    if filename:
        lines.append(f"Файл: <code>{core.esc(filename)}</code>")
    return util._fit(lines, 1000)


@core.router.message(Command("videos"))
async def cmd_videos(m: Message):
    """Видео в библиотеке: сколько постов с файлами, сколько только в плеерах, что нужно обновить."""
    rep_ = await asyncio.to_thread(core.jm.lib.video_report)
    st = core.jm.get_settings()
    text = (f"🎬 <b>Видео в библиотеке</b>\n"
            f"Постов с заявленным видео: {rep_['declared']}\n"
            f"• видео-файл найден: {rep_['files']}\n"
            f"• только в плеере (iframe): {rep_['embeds']}\n"
            f"• не найдено нигде: {rep_['missing']}\n"
            f"Разобрано до этой версии (без данных о плеерах): {len(rep_['refresh'])}\n\n"
            f"yt-dlp: {'✅' if video_fetch.available() else '❌ pip install yt-dlp'} · "
            f"ffmpeg: {'✅' if video_fetch.ffmpeg_available() else '❌'}\n"
            f"Видео из плееров в комментариях: {'вкл' if st['download_embedded_video'] else 'выкл'} "
            "(/set download_embedded_video on; нужны ещё publish_video_posts on и channel_videos on)")
    rows = []
    if rep_["refresh"]:
        rows.append([ui.btn(f"🔄 Обновить {len(rep_['refresh'])} постов с сайта", "vr:go")])
    await ui.render(m, text, rows, keep=True)


@ui.cb("vr")
async def cb_videos_refresh(c: CallbackQuery):
    rep_ = await asyncio.to_thread(core.jm.lib.video_report)
    if not rep_["refresh"]:
        return await c.answer("Обновлять нечего", show_alert=True)
    await c.answer()
    await sync_ui.queue_job(c.message, rep_["refresh"], source_message=c.message, recheck=True,
                    status_text=f"📥 Перечитываю {len(rep_['refresh'])} постов с видео, чтобы найти плееры.")


def card_rows(r, back_data=None):
    """Кнопки карточки поста (back_data — куда вернуться: результаты поиска и т. п.)."""
    id_ = r["id"]
    back = f":{back_data}" if back_data else ""
    files = postfiles.post_files(r)
    meta = json.loads(r["meta_json"] or "{}")
    rows = [[ui.btn("🔁 Перепроверить", f"r:{id_}{back}"), ui.btn("📄 meta.json", f"j:{id_}{back}")]]
    if files:
        rows.append([ui.btn("📦 Скачать ZIP", f"pz:{id_}"),
                     ui.btn("🖼 Выбрать файл", f"pf:{id_}:0:i:{id_}{back}")])
    if publishing.post_archives(r)[0]:
        rows.append([ui.btn("🔗 Архивы", f"a:{id_}"), ui.btn("📥 Архивы в чат", f"ar:{id_}")])
    if any(f["kind"] == "video" for f in files) or publishing.post_archives(r)[1]:
        rows.append([ui.btn("🎬 Скачать видео", f"vd:{id_}")])
    if core.CHANNEL_ID and files:
        rows.append([ui.btn("📣 В канал", f"pub:{id_}")])
    if r.get("cosplayer"):
        rows.append([ui.btn(f"🔔 Подписаться на {util.button_title(r['cosplayer'], 24)}", f"sn:{id_}:c")])
    h = core.jm.lib.hist_get(r["slug"]) if core.CHANNEL_ID else None
    if h and h["status"] == "partial" and core.jm.lib.kv_get(f"fwd:{r['slug']}"):
        rows.append([ui.btn("🔁 Дослать недостающее", f"rs:{id_}")])
    elif h and h["status"] in ("partial", "post_only"):
        rows.append([ui.btn("♻️ Переопубликовать", f"rp:{id_}")])
    if back_data:
        rows.append([ui.btn("↩️ Назад к результатам", back_data)])
    return rows


def dup_note(rec):
    """Строка «🔁 Дубликаты: …», если у поста есть копии в библиотеке."""
    copies = core.jm.lib.dup_candidates(rec)
    if not copies:
        return None
    published = core.jm.lib.published_among([c["slug"] for c in copies])
    return "🔁 Дубликаты: " + ", ".join(
        f"{core.esc(publishing.site_title(c['site']))}{' ✅' if c['slug'] in published else ''}" for c in copies[:5])


def hist_note(rec):
    """Строка «📣 Канал: …» для карточки, если пост есть в истории публикаций."""
    h = core.jm.lib.hist_get(rec["slug"])
    if not h:
        return None
    note = f"📣 Канал: {core.esc(mp.HIST_LABELS.get(h['status'], h['status']))}"
    return note + (f" — {core.esc(h['detail'])[:120]}" if h.get("detail") else "")


async def show_card(msg, r, back_data=None, note=None):
    note = note or "\n".join(x for x in (hist_note(r), dup_note(r)) if x) or None
    markup = await ui.make_markup(msg.bot, msg.chat.id, card_rows(r, back_data))
    sent = await delivery.send_with_preview(msg.bot, msg.chat.id, r, card(r, short=True, note=note),
                                   card(r, note=note), markup)
    await util.delete_message(msg)
    return sent


@ui.cb("i")
async def cb_info(c: CallbackQuery):
    parts = c.data.split(":", 3)
    r = core.jm.lib.get_by_id(int(parts[1]))
    back_data = parts[2] + ":" + parts[3] if len(parts) > 3 else (
        parts[2] if len(parts) > 2 else None)
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    await c.answer()
    await show_card(c.message, r, back_data)


@ui.cb("vd")
async def cb_videos(c: CallbackQuery):
    """Скачать видео поста в чат: прямые файлы и видео из плееров (yt-dlp)."""
    r = core.jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    vfiles = [f for f in postfiles.post_files(r) if f["kind"] == "video"]
    _archives, players = publishing.post_archives(r)
    if not vfiles and not players:
        return await c.answer("В посте нет видео", show_alert=True)
    if not vfiles and not video_fetch.available():
        return await c.answer("Видео лежит во внешнем плеере. Для скачивания установите yt-dlp "
                              "(pip install yt-dlp) и ffmpeg.", show_alert=True)
    await c.answer("Качаю видео…")
    status = await c.message.answer("🎬 Готовлю видео…")
    label, sent, errors = publishing.archive_label(r), 0, []
    for item in vfiles:
        try:
            async with postfiles.staged(r, {item["name"]}):
                with postfiles.materialize_post_file(r, item) as path:
                    await delivery.send_file_any(c.message.chat.id, path, caption=core.esc(label), video=True,
                                        name=item["name"])
            sent += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{item['name']}: {e}")
    for i, url in enumerate(players, 1):
        await delivery.edit_quiet(status, f"🎬 Плеер {i}/{len(players)}: {core.esc(urlparse(url).netloc)}")
        try:
            path = await delivery.download_video(url, r, delivery.thread_progress(
                status, f"🎬 Плеер {i}/{len(players)}: {core.esc(urlparse(url).netloc)}"))
            try:
                await delivery.send_file_any(c.message.chat.id, path, caption=core.esc(label), video=True,
                                    name=f"{mp.safe_name(label)}{path.suffix}",
                                    progress=delivery.upload_progress(status, path.name))
                sent += 1
            finally:
                shutil.rmtree(path.parent, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{urlparse(url).netloc}: {e}")
    text = f"🎬 Отправлено видео: {sent}"
    if errors:
        text += "\n" + "\n".join(f"❌ {core.esc(x)[:300]}" for x in errors[:5])
    await delivery.edit_quiet(status, text)


@ui.cb("a")
async def cb_archives(c: CallbackQuery):
    r = core.jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    m = json.loads(r["meta_json"] or "{}")
    links, players = publishing.post_archives(r)
    if not links and not players:
        return await c.answer("Ссылок на архивы нет", show_alert=True)
    text = ("🔗 Архивы:\n" + "\n".join(core.esc(x) for x in links[:20])) if links else ""
    if players:
        text += ("\n\n" if text else "") + "🎬 Видео в плеерах:\n" + "\n".join(
            core.esc(x) for x in players[:10])
    if m.get("password"):
        text += f"\nПароль: <code>{core.esc(m['password'])}</code>"
    await c.message.answer(text)
    await c.answer()


@ui.cb("ar")
async def cb_archives_download(c: CallbackQuery):
    r = core.jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    meta = json.loads(r["meta_json"] or "{}")
    links = [u for u in publishing.post_archives(r)[0] if extract_archive_urls(u)]
    if not links:
        return await c.answer("Среди ссылок нет поддерживаемых (ouo.io, Mediafire, Pixeldrain)",
                              show_alert=True)
    await c.answer("Качаю архивы…")
    captcha, label = [], publishing.archive_label(r)
    for i, url in enumerate(links[:8], 1):
        if await archive_links.deliver_archive(c.message, url, label, meta.get("password"), explain=False,
                                 rename_to=label if len(links) == 1 else f"{label} (архив {i})"
                                 ) == "captcha":
            captcha.append(url)
    if captcha:
        await c.message.answer("🔒 Эти ссылки закрыты капчей ouo.io:\n"
                               + "\n".join(core.esc(u) for u in captcha) + "\n\n" + core.esc(CAPTCHA_HELP))


@ui.cb("j")
async def cb_json(c: CallbackQuery):
    parts = c.data.split(":", 2)
    r = core.jm.lib.get_by_id(int(parts[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    back_data = parts[2] if len(parts) > 2 else None
    data = json.dumps(json.loads(r["meta_json"]), ensure_ascii=False, indent=2).encode()
    markup = ui.kb([ui.btn("↩️ К карточке", f"i:{r['id']}:{back_data}"
                     if back_data else f"i:{r['id']}")])
    await c.message.answer_document(
        BufferedInputFile(data, f"{r['slug']}.json"), reply_markup=markup)
    await util.delete_message(c.message)
    await c.answer()


@ui.cb("r")
async def cb_recheck(c: CallbackQuery):
    parts = c.data.split(":", 2)
    r = core.jm.lib.get_by_id(int(parts[1]))
    if not r:
        return await c.answer("Не найдено", show_alert=True)
    await c.answer()
    await sync_ui.queue_job(c.message, [r["url"]], source_message=c.message,
                    result_slug=r["slug"], back_data=parts[2] if len(parts) > 2 else None,
                    recheck=True)


@ui.cb("da")
async def cb_download_all(c: CallbackQuery):
    parts = c.data.split(":")
    tok = int(parts[1])
    site_page = int(parts[2]) if len(parts) > 2 else 0
    return_page = int(parts[3]) if len(parts) > 3 else 0
    s = core.SEARCHES.get(tok)
    if not s or not s["items"]:
        return await c.answer("Поиск устарел", show_alert=True)
    await c.answer()
    await sync_ui.queue_job(
        c.message, [it["url"] for it in s["items"]], source_message=c.message,
        status_text=f"📥 В очереди постов: {len(s['items'])}.",
        back_data=f"sp:{tok}:{site_page}:{return_page}")


@ui.cb("d")
async def cb_download(c: CallbackQuery):
    parts = c.data.split(":")
    tok, idx = int(parts[1]), int(parts[2])
    site_page = int(parts[3]) if len(parts) > 3 else idx // core.SITE_PAGE
    return_page = int(parts[4]) if len(parts) > 4 else 0
    s = core.SEARCHES.get(tok)
    if not s or idx >= len(s["items"]):
        return await c.answer("Поиск устарел", show_alert=True)
    url = s["items"][idx]["url"]
    await c.answer()
    await sync_ui.queue_job(c.message, [url], source_message=c.message,
                    result_slug=mp.slug_of(url),
                    back_data=f"sp:{tok}:{site_page}:{return_page}")


async def show_download_result(bot, chat_id, rec, partial=False, back_data=None):
    files = postfiles.post_files(rec)
    expected = sum(sum(formats.values()) for formats in
                   json.loads(rec["meta_json"] or "{}").get("formats", {}).values())
    note = (f"⚠️ Скачано частично: {len(files)} из {expected} файлов. "
            "Можно забрать уже загруженные файлы.") if partial else None
    markup = await ui.make_markup(bot, chat_id, card_rows(rec, back_data))
    await delivery.send_with_preview(bot, chat_id, rec, card(rec, short=True, note=note),
                            card(rec, note=note), markup)


async def show_post_files(message, rec, page, return_card=None):
    files = postfiles.post_files(rec)
    return_card = return_card or f"i:{rec['id']}"
    start = page * core.FILE_PAGE
    page_files = files[start:start + core.FILE_PAGE]
    lines = [f"<b>{core.esc(rec['title'])}</b> — файлы {start + 1}–"
             f"{min(start + len(page_files), len(files))} из {len(files)}:"]
    buttons = []
    for index, item in enumerate(page_files, start):
        label = f"{index + 1}. {item['name']}"
        buttons.append(ui.btn(label[:60], f"mf:{rec['id']}:{index}"))
        lines.append(f"{index + 1}. {core.esc(item['name'])}"
                     + (f" ({mp.human_size(item['size_bytes'])})" if item.get("size_bytes") else ""))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows += ui.nav_rows(page, max(1, math.ceil(len(files) / core.FILE_PAGE)),
                     lambda p: f"pf:{rec['id']}:{p}:{return_card}")
    rows.append([ui.btn("📦 Скачать ZIP", f"pz:{rec['id']}")])
    rows.append([ui.btn("↩️ К карточке", return_card)])
    await ui.render(message, util._fit(lines, 4000), rows)


async def send_zip_parts(message, rec, selected_files=None):
    names = {f["name"] for f in selected_files} if selected_files is not None else None
    status = None
    if postfiles.is_meta(rec):
        status = await message.answer("⏳ Скачиваю файлы с сайта…")
    async with postfiles.staged(rec, names):
        if status:
            await util.delete_message(status)
        temp_dir, parts, split_files = await asyncio.to_thread(
            postfiles.create_zip_parts, rec, selected_files)
        try:
            for index, path in enumerate(parts, 1):
                await delivery.send_file_any(message.chat.id, path,
                                    caption=f"{core.esc(publishing.archive_label(rec))} — архив {index}/{len(parts)}")
            for name, _count in split_files:
                await message.answer(join_hint(core.esc(name)))
            if not parts:
                await message.answer("Не удалось получить файлы поста (сайт недоступен?).")
        finally:
            temp_dir.cleanup()


@ui.cb("pz")
async def cb_post_zip(c: CallbackQuery):
    rec = core.jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not rec:
        return await c.answer("Пост не найден", show_alert=True)
    await c.answer("Готовлю ZIP-архивы…")
    await send_zip_parts(c.message, rec)


@ui.cb("pf")
async def cb_post_files(c: CallbackQuery):
    parts = c.data.split(":", 3)
    record_id, page = int(parts[1]), int(parts[2])
    return_card = parts[3] if len(parts) > 3 else None
    rec = core.jm.lib.get_by_id(int(record_id))
    if not rec:
        return await c.answer("Пост не найден", show_alert=True)
    await c.answer()
    await show_post_files(c.message, rec, page, return_card=return_card)


@ui.cb("mf")
async def cb_post_file(c: CallbackQuery):
    _, record_id, index = c.data.split(":")
    rec = core.jm.lib.get_by_id(int(record_id))
    if not rec:
        return await c.answer("Пост не найден", show_alert=True)
    files = postfiles.post_files(rec)
    index = int(index)
    if index >= len(files):
        return await c.answer("Файл не найден", show_alert=True)
    item = files[index]
    await c.answer("Готовлю файл…" if postfiles.is_meta(rec) else "Отправляю файл…")
    async with postfiles.staged(rec, {item["name"]}):
        try:
            size = await asyncio.to_thread(postfiles.post_file_size, rec, item)
        except OSError:
            return await c.message.answer("Не удалось скачать файл с сайта.")
        if size > core.ZIP_PART_SIZE:
            await send_zip_parts(c.message, rec, [item])
            return
        with postfiles.materialize_post_file(rec, item) as path:
            upload = FSInputFile(path, filename=item["name"])
            caption = media_caption(rec, item["name"])
            if item["kind"] == "photo" and size <= 9 * core.MB:
                await delivery.tg_call(c.message.answer_photo, upload, caption=caption)
            else:   # видео и документы; больше 50 МБ — через MTProto
                await delivery.send_file_any(c.message.chat.id, path, caption=caption,
                                    video=item["kind"] == "video", name=item["name"])
