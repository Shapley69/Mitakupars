"""Команды канала: /channel, /publish, массовая публикация из поиска."""
import asyncio
import json
import mitaku_parser as mp
import mitaku_sites as ms
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from . import core, delivery, importer, publishing, sync_ui, ui, util

async def channel_status():
    """(текст, строки кнопок). Кнопок больше трёх -> render покажет их на нижней клавиатуре."""
    if not core.CHANNEL_ID:
        return "CHANNEL_ID не задан в .env — публикация в канал отключена.", []
    s = core.jm.get_settings()
    try:
        chat = await core.BOT.get_chat(core.CHANNEL_ID)
    except Exception as e:  # noqa: BLE001
        return (f"❌ Не могу открыть канал {core.esc(core.CHANNEL_ID)}: {core.esc(str(e))}\n"
                "Бот должен быть администратором канала."), []
    linked = getattr(chat, "linked_chat_id", None)
    sites = ", ".join(ms.SITES[k].title for k in core.jm.get_active_sites()) or "—"
    text = (f"📣 <b>Канал:</b> {core.esc(chat.title or str(chat.id))}\n"
            f"Комментарии: {'✅ группа обсуждения подключена' if linked else '❌ у канала нет группы обсуждения'}\n"
            f"Автопубликация: {'✅ вкл' if s['channel'] else '⬜ выкл'}, проверка каждые {s['channel_interval']} мин\n"
            f"Сайты: {core.esc(sites)}\n"
            f"Пауза между постами: {s['publish_delay']} с (/set publish_delay)"
            + (f" — ⚠️ автозамедление ×{delivery.flood_factor():g} после лимита Telegram" if delivery.flood_factor() > 1 else "")
            + f"\nZIP в комментариях: {'да' if s['channel_zip'] else 'нет'}, не больше {s['zip_max_mb']} МБ "
              f"(0 — без лимита); при импорте: {'да' if s['import_zip'] else 'нет'} (/set import_zip)\n"
            f"В очереди на публикацию: {len(publishing.PUBLISH_PENDING)}\n"
            f"Комментарии: фото — {s['channel_media']}, видео — {'да' if s['channel_videos'] else 'нет'}, "
            f"ZIP — {'да' if s['channel_zip'] else 'нет'}")
    for b in publishing.BATCHES.values():
        text += (f"\n📦 Массовая публикация «{core.esc(b['label'])}»: "
                 f"{b['total'] - b['left']}/{b['total']}")
    st = importer.IMPORT["state"] if importer.import_active() else core.jm.lib.kv_get(importer.IMPORT_KEY)
    st = json.loads(st) if isinstance(st, str) and st else (st or None)
    if st:
        text += (f"\n📥 Импорт {core.esc(publishing.site_title(st['site']))}: "
                 f"{importer.IMPORT_STATUS.get(st['status'], st['status'])}, страница {st['page']}/{st['last_page']}")
    rows = [[ui.btn("⏹ Выключить" if s["channel"] else "▶️ Включить",
                 "ch:off" if s["channel"] else "ch:on")],
            [ui.btn("🔍 Проверить сейчас", "ch:now"), ui.btn("🧪 Тест", "ch:test")],
            [ui.btn("📥 Импорт всего сайта", "im:menu")], [ui.btn("📜 История публикаций", "h:all:0")]]
    if publishing.PUBLISH_PENDING:
        rows.append([ui.btn("🧹 Очистить очередь публикации", "ch:clear")])
    rows.append([ui.btn("✖️ Закрыть", "x:close")])
    return text, rows


@core.router.message(Command("channel"))
async def cmd_channel(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg in ("on", "off"):
        core.jm.set_setting("channel", arg)
    text, rows = await channel_status()
    await ui.render(m, text, rows, keep=True)


@ui.cb("ch")
async def cb_channel(c: CallbackQuery):
    action = c.data.split(":")[1]
    if action in ("on", "off"):
        core.jm.set_setting("channel", action)
        await c.answer("Включено: первая проверка только запомнит текущие посты" if action == "on"
                       else "Выключено", show_alert=action == "on")
    elif action == "now":
        started = await publishing.watch_tick()
        await c.answer("Проверка запущена" if started else "Бот занят задачей, попробуйте позже",
                       show_alert=not started)
    elif action == "clear":
        removed = publishing.clear_publish_queue()
        await c.answer(f"Очередь очищена: снято {removed} постов", show_alert=True)
    elif action == "test":
        try:
            msg = await core.BOT.send_message(core.CHANNEL_ID, "🧪 Тест: бот подключён к каналу")
            await asyncio.sleep(2)
            await msg.delete()
            await c.answer("Сообщение отправлено и удалено ✅", show_alert=True)
        except Exception as e:  # noqa: BLE001
            await c.answer(f"Не вышло: {e}"[:190], show_alert=True)
    text, rows = await channel_status()
    await ui.render(c.message, text, rows)


@core.router.message(Command("publish"))
async def cmd_publish(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not core.CHANNEL_ID:
        return await m.answer("CHANNEL_ID не задан в .env")
    if not arg:
        return await m.answer("Использование: /publish &lt;slug или ссылка на пост&gt;")
    is_url = mp.is_post_url(arg)
    slug = mp.slug_of(arg) if is_url else arg
    if core.jm.lib.get(slug):
        queued = publishing.enqueue_publish(slug, True)
        return await m.answer("📣 Пост поставлен в очередь публикации." if queued
                              else "Этот пост уже стоит в очереди публикации.")
    if is_url:
        return await sync_ui.queue_job(m, [arg], publish=True,
                               status_text="📥 Поста нет в библиотеке — добавляю и публикую.")
    await m.answer("Такого slug нет в библиотеке. Пришлите полную ссылку на пост.")


@ui.cb("rs")
async def cb_resume(c: CallbackQuery):
    r = core.jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Пост не найден", show_alert=True)
    queued = publishing.enqueue_publish(r["slug"], "resume")
    await c.answer("🔁 Недостающее будет отправлено в порядке очереди" if queued
                   else "Уже в очереди", show_alert=not queued)


@ui.cb("rp")
async def cb_republish(c: CallbackQuery):
    parts = c.data.split(":")
    r = core.jm.lib.get_by_id(int(parts[1]))
    if not r:
        return await c.answer("Пост не найден", show_alert=True)
    await c.answer()
    if len(parts) > 2 and parts[2] == "yes":
        queued = publishing.enqueue_publish(r["slug"], "republish")
        return await ui.render(c.message, "♻️ Пост поставлен в очередь: старый будет удалён из канала "
                                       "и опубликован заново." if queued else "Уже в очереди.", [])
    await ui.render(c.message,
                 f"♻️ <b>Переопубликовать</b> «{core.esc(r['title'])}»?\n\nСтарый пост удалится из канала "
                 "и опубликуется заново вместе с комментариями. Комментарии прежней версии останутся "
                 "в группе обсуждения.", [[ui.btn("✅ Да", f"rp:{r['id']}:yes")],
                                          [ui.btn("✖️ Отмена", f"i:{r['id']}")]])


@ui.cb("pub")
async def cb_publish(c: CallbackQuery):
    r = core.jm.lib.get_by_id(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("Пост не найден", show_alert=True)
    queued = publishing.enqueue_publish(r["slug"], True)
    await c.answer("📣 В очереди публикации" if queued else "Уже в очереди", show_alert=not queued)


# --------------------------------------------------------------------------- «опубликовать всё» из поиска
def plan_publish(s, mode):
    """Для результатов поиска: slug-и от старых к новым, ссылки постов, которых нет в библиотеке,
    и счётчики: done — уже опубликованы, ready — можно публиковать, blocked — по правилам нельзя
    (видео / только архивы / нет фото). mode: l — результаты библиотеки, w — найденное на сайтах."""
    urls = []
    if mode == "l":
        rows, _ = core.jm.lib.search(s["q"], s["field"], limit=10 ** 6)
        slugs = [r["slug"] for r in rows]
    else:
        slugs = [mp.slug_of(it["url"]) for it in s["items"]]
        have = core.jm.lib.existing_slugs(slugs)
        urls = [it["url"] for it, slug in zip(s["items"], slugs) if slug not in have]
    slugs = list(dict.fromkeys(slugs))[::-1]   # поиск отдаёт новые первыми, публикуем старые первыми
    published = {k.split(":", 1)[1] for k in core.jm.lib.kv_list("published:")}
    settings = core.jm.get_settings()
    plan = {"slugs": slugs, "urls": urls, "done": 0, "ready": 0, "blocked": 0}
    recs = core.jm.lib.get_many([s for s in slugs if s not in published])
    for slug in slugs:
        if slug in published:
            plan["done"] += 1
            continue
        rec = recs.get(slug)
        if rec:
            plan["ready" if publishing.publish_check(rec, settings)[0] == "ok" else "blocked"] += 1
    return plan


@ui.cb("pa")
async def cb_publish_all(c: CallbackQuery):
    parts = c.data.split(":")
    tok, mode, page = int(parts[1]), parts[2], int(parts[3])
    return_page = int(parts[4]) if len(parts) > 4 else 0
    s = core.SEARCHES.get(tok)
    if not core.CHANNEL_ID or not s:
        return await c.answer("Поиск устарел или CHANNEL_ID не задан", show_alert=True)
    await c.answer()
    plan = await asyncio.to_thread(plan_publish, s, mode)
    back = f"s:{tok}:{page}" if mode == "l" else f"sp:{tok}:{page}:{return_page}"
    todo = plan["ready"] + len(plan["urls"])
    if todo <= 0:
        text = "Нечего публиковать: все найденные посты уже опубликованы."
        if plan["blocked"]:
            text += (f"\nПостов с видео / только с архивами: {plan['blocked']} — они в канал не идут, "
                     "причины записаны в /history.")
        return await ui.render(c.message, text, [[ui.btn("↩️ Назад", back)]])
    delay = core.jm.get_settings()["publish_delay"]
    text = (f"📣 <b>Опубликовать в канал</b> «{core.esc(s['q'] or 'все посты')}»\n\n"
            f"Постов в очередь: <b>{todo}</b>"
            + (f" (из них {len(plan['urls'])} сначала загрузятся с сайта)" if plan["urls"] else "")
            + f"\nУже опубликованы, будут пропущены: {plan['done']}"
            + (f"\nНе пойдут в канал (видео / только архивы / нет фото): {plan['blocked']} — "
               "попадут в историю с причиной" if plan["blocked"] else "")
            + f"\nПауза между постами: {delay} с — всё займёт примерно "
              f"{util.human_duration(todo * delay)}.\n"
              "Порядок: от старых к новым. Остановить можно в /channel («🧹 Очистить очередь»).\n\n"
              "Поставить в очередь?")
    await ui.render(c.message, text, [[ui.btn("✅ Да, в очередь", f"pg:{tok}:{mode}")],
                                   [ui.btn("✖️ Отмена", back)]])


@ui.cb("pg")
async def cb_publish_go(c: CallbackQuery):
    _, tok, mode = c.data.split(":")
    s = core.SEARCHES.get(int(tok))
    if not core.CHANNEL_ID or not s:
        return await c.answer("Поиск устарел или CHANNEL_ID не задан", show_alert=True)
    await c.answer()
    plan = await asyncio.to_thread(plan_publish, s, mode)
    label = s["q"] or "все посты"
    if plan["urls"]:   # часть постов ещё не в библиотеке: сначала загрузка, затем очередь публикации
        return await sync_ui.queue_job(
            c.message, plan["urls"], source_message=c.message,
            status_text=f"📥 Сначала загружаю {len(plan['urls'])} постов с сайта, затем поставлю их "
                        "в очередь публикации.",
            extra={"publish_batch": plan["slugs"], "batch_label": label})
    n = await publishing.start_batch(c.message.chat.id, plan["slugs"], label)
    await ui.render(c.message,
                 f"📣 В очередь публикации поставлено постов: {n}. Ход — в /channel." if n
                 else "Нечего публиковать: все найденные посты уже опубликованы или не подходят.",
                 [])
