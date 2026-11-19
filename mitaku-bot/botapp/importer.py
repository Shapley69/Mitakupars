"""Импорт всего сайта в канал: состояние в БД, остановка, продолжение, защита от блокировки."""
import asyncio
import json
import logging
import mitaku_parser as mp
import mitaku_sites as ms
import time
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from . import core, delivery, publishing, ui, util

# --------------------------------------------------------------------------- импорт всего сайта
# Идёт от самой старой страницы листинга к самой новой: каждый пост разбирается, и если подходит
# (есть фото, нет видео, не «только архивы») — публикуется в канал с паузой publish_delay.
# Состояние (страница, номер поста, счётчики) хранится в БД: после остановки или перезапуска
# импорт продолжается с той же страницы. Посты, по которым решение уже принято, пропускаются.
IMPORT = {"task": None, "stop": False, "msg": None, "last_edit": 0.0, "state": None}
IMPORT_KEY = "import_state"
KNOWN_STATUSES = ("full", "published", "partial", "post_only",
                  "skip_video", "skip_archives", "skip_empty", "skip_duplicate")
IMPORT_STATUS = {"running": "▶️ идёт", "stopped": "⏹ остановлен", "finished": "✅ завершён",
                 "interrupted": "⚠️ прерван перезапуском бота",
                 "failed": "⛔ остановлен из-за ошибки"}
IMPORT_COUNTERS = ("full", "partial", "skip_video", "skip_archives", "skip_empty", "skip_duplicate",
                   "known", "errors")


class ImportStopped(Exception):
    pass


def import_active():
    task = IMPORT["task"]
    return bool(task and not task.done())


def import_load():
    raw = core.jm.lib.kv_get(IMPORT_KEY)
    return json.loads(raw) if raw else None


def import_save(st):
    st["updated_at"] = mp.now_iso()
    core.jm.lib.kv_set(IMPORT_KEY, json.dumps(st, ensure_ascii=False))


def import_new(site_key):
    return {"site": site_key, "base": ms.SITES[site_key].sections()["all"], "status": "running",
            "phase": "", "last_page": 0, "page": 0, "index": 0, "page_total": 0, "current": "",
            "current_url": "", "streak": 0, "error": "", "started_at": mp.now_iso(),
            "updated_at": mp.now_iso(), **{k: 0 for k in IMPORT_COUNTERS}}


def import_text(st, settings):
    lines = [f"📥 <b>Импорт сайта {core.esc(publishing.site_title(st['site']))} → канал</b>",
             f"Статус: {IMPORT_STATUS.get(st['status'], st['status'])}"]
    if st.get("phase") and st["status"] == "running":
        lines.append(f"⏳ {core.esc(st['phase'])}")
    if st["last_page"]:
        lines.append(f"Страница: <b>{st['page']}</b> из {st['last_page']} "
                     f"(идём от самой старой к новой), готово страниц: {st['last_page'] - st['page']}")
        if st["page_total"]:
            lines.append(f"Пост на странице: {st['index']}/{st['page_total']}"
                         + (f" — {core.esc(util.button_title(st['current'], 60))}" if st["current"] else ""))
    lines += [f"✅ Полностью: {st['full']} · ⚠️ частично: {st['partial']}",
              f"⏭ Не опубликовано (записано в историю): видео {st['skip_video']} · "
              f"только архивы {st['skip_archives']} · без фото {st['skip_empty']} · "
              f"дубликаты {st.get('skip_duplicate', 0)}",
              f"Уже были: {st['known']} · ошибок: {st['errors']}"]
    if st.get("error"):
        lines.append(f"❌ {core.esc(st['error'])}")
    lines.append(f"Пауза между постами в канал: {settings['publish_delay']} с · между запросами к "
                 f"сайту ≈{settings['import_delay']} с, каждые {settings['pause_every']} постов — "
                 f"{settings['pause_seconds']} с")
    if st["status"] in ("stopped", "interrupted", "failed") and st["last_page"]:
        lines.append(f"📌 Остановились на странице {st['page']}, пост {st['index']}"
                     f"/{st['page_total']}. Позиция сохранена в базе — «▶️ Продолжить» начнёт "
                     "с этой страницы.")
    return util._fit(lines, 4000)


def import_rows(st):
    if st["status"] == "running":
        return [[ui.btn("⏹ Остановить импорт", "im:stop"), ui.btn("🔄 Обновить", "im:refresh")]]
    if st["status"] in ("stopped", "interrupted", "failed"):
        return [[ui.btn("▶️ Продолжить импорт", "im:resume")],
                [ui.btn("🗑 Сбросить позицию", "im:reset")], [ui.btn("✖️ Закрыть", "x:close")]]
    return [[ui.btn("📥 Новый импорт", "im:new")], [ui.btn("✖️ Закрыть", "x:close")]]


async def import_refresh(force=False):
    """Обновляет сообщение со статусом импорта (не чаще раза в 10 с)."""
    st, msg = IMPORT["state"], IMPORT["msg"]
    if not st or not msg or time.monotonic() - IMPORT["last_edit"] < (0 if force else 10):
        return
    IMPORT["last_edit"] = time.monotonic()
    settings = await asyncio.to_thread(core.jm.get_settings)
    text = import_text(st, settings)
    markup = InlineKeyboardMarkup(inline_keyboard=[list(r) for r in import_rows(st)])
    try:
        await core.BOT.edit_message_text(text, chat_id=msg[0], message_id=msg[1], reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        sent = await core.BOT.send_message(msg[0], text, reply_markup=markup)   # сообщение удалено
        IMPORT["msg"] = (msg[0], sent.message_id)


async def import_wait(st, seconds, phase):
    st["phase"] = phase
    await import_refresh(True)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if IMPORT["stop"]:
            raise ImportStopped()
        await asyncio.sleep(1)
    st["phase"] = ""


def is_block(e):
    return type(e).__name__ == "SiteBlocked" or isinstance(e, mp.requests.exceptions.RetryError)


def block_minutes(attempt):
    return min(5 * 2 ** (attempt - 1), 60)


async def import_call(st, fn, *args):
    """Вызов парсера в потоке. Если сайт ограничил доступ — ждёт (5, 10, 20… мин) и повторяет."""
    for attempt in range(1, 7):
        try:
            return await asyncio.to_thread(fn, *args)
        except mp.Stopped:
            raise ImportStopped()
        except Exception as e:  # noqa: BLE001
            if not is_block(e) or attempt == 6:
                raise
            minutes = block_minutes(attempt)
            await import_wait(st, minutes * 60,
                              f"сайт ограничил доступ — жду {minutes} мин (попытка {attempt}/5)")


async def import_parse(st, item):
    """Разбор поста в библиотеку (process_post глотает ошибки, поэтому блокировку ловим по флагу)."""
    scraper = core.jm.scraper
    for attempt in range(1, 7):
        try:
            res = await asyncio.to_thread(scraper.process_post, item)
        except mp.Stopped:
            raise ImportStopped()
        if res == "error" and scraper.blocked and attempt < 6:
            minutes = block_minutes(attempt)
            await import_wait(st, minutes * 60,
                              f"сайт ограничил доступ — жду {minutes} мин (попытка {attempt}/5)")
            continue
        return res


def import_known(slug):
    if core.jm.lib.kv_get(f"published:{slug}"):
        return True
    h = core.jm.lib.hist_get(slug)
    return bool(h and h["status"] in KNOWN_STATUSES)


async def import_one(st, site, item, settings):
    """Один пост: -> full | partial | skip_* | known | errors."""
    slug = site.slug(item["url"])
    if await asyncio.to_thread(import_known, slug):
        return "known"
    await import_parse(st, item)
    rec = await asyncio.to_thread(core.jm.lib.get, slug)
    if not rec:
        detail = str(core.jm.scraper.last_error or "пост не удалось разобрать")[:300]
        await asyncio.to_thread(core.jm.lib.hist_set, slug, "error", detail,
                                title=item.get("title") or slug, site=site.key)
        return "errors"
    status, why = publishing.publish_check(rec, settings)
    if status != "ok":   # видео / только архивы / нет фото / дубликат — в историю, в канал не идёт
        await asyncio.to_thread(publishing.record_verdict, rec, status, why)
        return status
    try:
        detail = await publishing.publish_paced(slug, False, should_stop=lambda: IMPORT["stop"],
                                     overrides={"channel_zip": bool(settings.get("import_zip"))})
    except publishing.NotPublishable as e:
        return e.status
    except Exception as e:  # noqa: BLE001
        logging.exception("Импорт: публикация %s", slug)
        h = await asyncio.to_thread(core.jm.lib.hist_get, slug)
        if not h or h["status"] != "partial":
            await asyncio.to_thread(core.jm.lib.hist_set, slug, "error", str(e)[:300],
                                    title=rec["title"], site=site.key)
        return "errors"
    if detail is None:
        raise ImportStopped()
    h = await asyncio.to_thread(core.jm.lib.hist_get, slug)
    return "full" if h and h["status"] == "full" else "partial"


async def import_run(st):
    site = ms.SITES[st["site"]]
    scraper = core.jm.scraper
    settings = await asyncio.to_thread(core.jm.get_settings)
    scraper.opt = mp.Options(storage="meta", images=True, videos=True,
                             delay=settings["import_delay"], pause_every=settings["pause_every"],
                             pause_seconds=settings["pause_seconds"])
    scraper.ctl.reset()
    scraper._since_pause = 0
    IMPORT.update(stop=False, state=st)
    st.update(status="running", error="", streak=0)
    try:
        if not st["last_page"]:
            st["phase"] = "ищу последнюю страницу листинга…"
            await asyncio.to_thread(import_save, st)
            await import_refresh(True)
            last = await import_call(st, scraper.find_last_page, site, st["base"])
            if not last:
                raise RuntimeError("не удалось открыть листинг сайта (пусто или нет ответа)")
            st.update(last_page=last, page=last, index=0, phase="")
        while st["page"] >= 1:
            if IMPORT["stop"]:
                raise ImportStopped()
            items = await import_call(st, site.fetch_listing, scraper.session, st["base"], st["page"])
            seen, unique = set(), []
            for it in items or []:
                if it["url"] not in seen:
                    seen.add(it["url"])
                    unique.append(it)
            unique.reverse()   # внутри страницы тоже от старых к новым
            st["page_total"] = len(unique)
            for i, item in enumerate(unique, 1):
                if IMPORT["stop"]:
                    raise ImportStopped()
                st.update(index=i, current=item.get("title") or "", current_url=item["url"])
                outcome = await import_one(st, site, item, settings)
                st[outcome] = st.get(outcome, 0) + 1
                st["streak"] = st["streak"] + 1 if outcome == "errors" else 0
                await asyncio.to_thread(import_save, st)
                await import_refresh()
                if st["streak"] >= 5:
                    raise RuntimeError("5 ошибок подряд — импорт остановлен, позиция сохранена. "
                                       "Проверьте канал, права бота и доступ к сайту")
            st.update(page=st["page"] - 1, index=0, page_total=0, current="")
            await asyncio.to_thread(import_save, st)
            await import_refresh()
            await import_call(st, scraper._nap, settings["import_delay"], False)
        st["status"] = "finished"
        await asyncio.to_thread(core.jm.lib.kv_set, f"baseline:{st['site']}", "1")
    except ImportStopped:
        st["status"] = "stopped"
    except asyncio.CancelledError:
        await asyncio.to_thread(import_save, st)   # статус остаётся running -> при старте «прерван»
        raise
    except Exception as e:  # noqa: BLE001
        logging.exception("Импорт сайта %s", st["site"])
        st.update(status="failed", error=str(e)[:300])
    finally:
        st["phase"] = ""
        scraper.ctl.reset()
        if st["status"] != "running":
            await asyncio.to_thread(import_save, st)
            await import_refresh(True)
    icon = {"finished": "✅ завершён", "stopped": "⏹ остановлен", "failed": "⛔ остановлен из-за ошибки"}
    await delivery.notify_admins(
        f"📥 Импорт {core.esc(publishing.site_title(st['site']))} {icon.get(st['status'], st['status'])}.\n"
        f"Полностью {st['full']} · частично {st['partial']} · не опубликовано "
        f"{st['skip_video'] + st['skip_archives'] + st['skip_empty'] + st.get('skip_duplicate', 0)}"
        f" · ошибок {st['errors']}"
        + (f"\nПозиция: страница {st['page']}, пост {st['index']}. Продолжить — /import."
           if st["status"] != "finished" else "")
        + (f"\n⚠️ Частично опубликованных: {st['partial']} — «/history → Частичные → Дослать»."
           if st["partial"] else "")
        + (f"\n❌ {core.esc(st['error'])}" if st.get("error") else ""))


def import_can_start():
    if not core.CHANNEL_ID:
        return "CHANNEL_ID не задан в .env — импорт идёт в канал"
    if import_active():
        return "Импорт уже идёт"
    if core.jm.status()["state"] != "idle" or core.jm.q.qsize():
        return "Парсер занят задачей — дождитесь её или остановите (/stop)"
    return None


async def import_launch(msg, st):
    """Запускает (или продолжает) импорт и показывает сообщение со статусом."""
    settings = await asyncio.to_thread(core.jm.get_settings)
    st.update(status="running", error="")
    await asyncio.to_thread(import_save, st)
    IMPORT["state"] = st
    IMPORT["task"] = asyncio.create_task(import_run(st))
    sent = await ui.render(msg, import_text(st, settings), import_rows(st))
    IMPORT["msg"] = (sent.chat.id, sent.message_id)
    IMPORT["last_edit"] = time.monotonic()


async def import_screen(msg, keep=False):
    st = IMPORT["state"] if import_active() else await asyncio.to_thread(import_load)
    if st:
        settings = await asyncio.to_thread(core.jm.get_settings)
        sent = await ui.render(msg, import_text(st, settings), import_rows(st), keep=keep)
        IMPORT["state"], IMPORT["msg"] = st, (sent.chat.id, sent.message_id)
        return
    await import_pick(msg, keep)


async def import_pick(msg, keep=False):
    rows = [[ui.btn(f"📥 {publishing.site_title(k)}" + (" 🧪" if s.experimental else ""), f"im:site:{k}")]
            for k, s in ms.SITES.items()]
    rows.append([ui.btn("✖️ Закрыть", "x:close")])
    await ui.render(msg, "📥 <b>Импорт всего сайта в канал</b>\n\nБот пройдёт все страницы сайта от "
                      "самой первой публикации до последней и опубликует каждый пост в подключённый "
                      "канал (фото — в комментариях, группами). Посты с видео и посты только со "
                      "ссылками на архивы в канал не идут — они записываются в /history.\n\n"
                      "Выберите сайт:", rows, keep=keep)


@core.router.message(Command("import"))
async def cmd_import(m: Message):
    await import_screen(m, keep=True)


@ui.cb("im")
async def cb_import(c: CallbackQuery):
    parts = c.data.split(":")
    action = parts[1]
    if action == "menu":
        await c.answer()
        return await import_screen(c.message)
    if action == "new":
        await c.answer()
        return await import_pick(c.message)
    if action == "refresh":
        await c.answer()
        return await import_refresh(True)
    if action == "stop":
        if not import_active():
            return await c.answer("Импорт не запущен", show_alert=True)
        IMPORT["stop"] = True
        core.jm.ctl.stop()   # прерывает текущий запрос/паузу парсера
        IMPORT["state"]["phase"] = "останавливаю… позиция будет сохранена"
        await c.answer("Останавливаю: позиция сохранится")
        return await import_refresh(True)
    if action == "reset":
        if import_active():
            return await c.answer("Сначала остановите импорт", show_alert=True)
        await asyncio.to_thread(core.jm.lib.kv_set, IMPORT_KEY, "")
        IMPORT["state"] = None
        await c.answer("Позиция сброшена")
        return await import_pick(c.message)
    if action == "site":
        key = parts[2]
        await c.answer()
        text = (f"📥 <b>Импорт сайта {core.esc(publishing.site_title(key))} → канал</b>\n\n"
                "• Идём от самой старой страницы к новой, каждый пост — в канал, фото группами "
                "в комментариях.\n• Видео и «только архивы» в канал не идут, а записываются в "
                "историю.\n• Паузы против блокировки IP: между запросами и длинная каждые N постов; "
                "при блокировке сайта бот ждёт и повторяет.\n• Остановить можно в любой момент — "
                "страница и пост запоминаются в базе, после перезапуска можно продолжить.\n\n"
                "Это может занять много часов или дней (/set publish_delay, import_delay).")
        if core.CHANNEL_ID:
            try:
                chat = await core.BOT.get_chat(core.CHANNEL_ID)
                text += f"\n\nКанал: {core.esc(chat.title or str(chat.id))}"
                if not getattr(chat, "linked_chat_id", None):
                    text += "\n⚠️ У канала нет группы обсуждения — комментарии (фото) не отправятся."
            except Exception as e:  # noqa: BLE001
                text += f"\n\n⚠️ Не могу открыть канал: {core.esc(str(e))}"
        return await ui.render(c.message, text, [[ui.btn("✅ Запустить импорт", f"im:go:{key}")],
                                              [ui.btn("✖️ Отмена", "im:new")]])
    if action in ("go", "resume"):
        err = import_can_start()
        if err:
            return await c.answer(err, show_alert=True)
        await c.answer()
        if action == "go":
            st = import_new(parts[2])
        else:
            st = await asyncio.to_thread(import_load)
            if not st:
                return await import_pick(c.message)
        return await import_launch(c.message, st)
