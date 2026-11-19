"""История публикаций, история поиска, дубликаты (/history /searches /dups)."""
import asyncio
import math
import mitaku_parser as mp
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from . import core, publishing, search_ui, sync_ui, ui, util

# --------------------------------------------------------------------------- история публикаций и поиска
HIST_FILTERS = (("all", "📋 Все"), ("ok", "✅ Полные"), ("partial", "⚠️ Частичные"),
                ("skipped", "⏭ Пропущенные"), ("error", "❌ Ошибки"))
FIELD_LABELS = {"cosplayer": "👤 ник", "game": "🎮 игра/аниме", "character": "🎭 персонаж",
                "tag": "🏷 тег"}


async def show_history(m, group="all", page=0, keep=False):
    """Список постов, обработанных для канала: с метками (полностью / частично / пропущен…)."""
    group = group if group in mp.HIST_GROUPS else "all"
    counts = await asyncio.to_thread(core.jm.lib.hist_counts)
    total = counts[group]
    pages = max(1, math.ceil(total / core.PAGE))
    page = min(max(page, 0), pages - 1)
    rows, _ = await asyncio.to_thread(core.jm.lib.hist_list, group, core.PAGE, page * core.PAGE)
    lines = [f"📜 <b>История публикаций</b> — {dict(HIST_FILTERS)[group]}: {total}",
             f"✅ полные {counts['ok']} · ⚠️ частичные {counts['partial']} · "
             f"⏭ не опубликованы {counts['skipped']} · ❌ ошибки {counts['error']}", ""]
    rows_kb = []
    for i, r in enumerate(rows, 1):
        icon = mp.HIST_LABELS.get(r["status"], "•").split()[0]
        bits = [r.get("cosplayer") or "",
                f"фото {r['photos_sent']}/{r['photos_total']}" if r.get("photos_total") else "",
                (r.get("updated_at") or "")[:10]]
        lines.append(f"{i}. {icon} <b>{core.esc(r['title'])}</b>\n"
                     f"    <i>{core.esc(' · '.join(b for b in bits if b))}</i>\n"
                     f"    {core.esc(r.get('detail') or mp.HIST_LABELS.get(r['status'], ''))}")
        if r.get("post_id"):
            rows_kb.append([ui.btn(util.button_title(f"{i}. {icon} {r['title']}", 42),
                                f"i:{r['post_id']}:h:{group}:{page}")])
    if not rows:
        lines.append("Пока пусто.")
    rows_kb += ui.nav_rows(page, pages, lambda p: f"h:{group}:{p}")
    if total:
        rows_kb.append([ui.btn("📤 Экспорт CSV", f"ex:h:{group}")])
    if core.CHANNEL_ID and group == "partial" and total:
        rows_kb.append([ui.btn("🔁 Дослать недостающее под всеми", "hb:resume")])
    if core.CHANNEL_ID and group == "skipped" and total:
        rows_kb.append([ui.btn("🔄 Перепроверить пропущенные (по базе)", "hb:local")])
        rows_kb.append([ui.btn("🌐 Перепроверить с сайта", "hb:web")])
    filters = [ui.btn(f"{'• ' if g == group else ''}{label} ({counts[g]})", f"h:{g}:0")
               for g, label in HIST_FILTERS]
    rows_kb += [filters[i:i + 2] for i in range(0, len(filters), 2)]
    rows_kb.append([ui.btn("✖️ Закрыть", "x:close")])
    await ui.render(m, util._fit(lines, 4000), rows_kb, keep=keep)


@ui.cb("h")
async def cb_history(c: CallbackQuery):
    _, group, page = c.data.split(":")
    await c.answer()
    await show_history(c.message, group, int(page))


def resume_candidates():
    """(slug-и частичных постов, под которыми можно дослать комментарии — старые первыми,
    число частичных без данных о комментарии)."""
    rows, _ = core.jm.lib.hist_list("partial", 10 ** 6, 0)
    ok, orphans = [], 0
    for r in rows:
        if r["status"] == "partial" and core.jm.lib.kv_get(f"fwd:{r['slug']}"):
            ok.append(r["slug"])
        else:
            orphans += 1
    return ok[::-1], orphans


def recheck_skipped(apply=False):
    """Перепроверка пропущенных постов по данным библиотеки (после смены настроек).
    Возвращает (подошедшие slug-и от старых к новым, сколько осталось пропущенными).
    apply=True — убрать подошедшие из истории пропущенных."""
    settings = core.jm.get_settings()
    rows, _ = core.jm.lib.hist_list("skipped", 10 ** 6, 0)
    ok, still = [], 0
    recs = core.jm.lib.get_many([r["slug"] for r in rows])
    for r in rows:
        rec = recs.get(r["slug"])
        if rec and publishing.publish_check(rec, settings)[0] == "ok":
            ok.append(r["slug"])
        else:
            still += 1
    if apply:
        for slug in ok:
            core.jm.lib.hist_delete(slug)
    return ok[::-1], still


def skipped_posts():
    rows, _ = core.jm.lib.hist_list("skipped", 10 ** 6, 0)
    found = core.jm.lib.get_many([r["slug"] for r in rows])
    recs = [found[r["slug"]] for r in rows if r["slug"] in found]
    return [x["slug"] for x in recs][::-1], [x["url"] for x in recs][::-1]


@ui.cb("hb")
async def cb_history_batch(c: CallbackQuery):
    """Массовые действия над историей: дослать комментарии, перепроверить пропущенные."""
    parts = c.data.split(":")
    action, confirmed = parts[1], len(parts) > 2 and parts[2] == "yes"
    if not core.CHANNEL_ID:
        return await c.answer("CHANNEL_ID не задан", show_alert=True)
    await c.answer()
    delay = core.jm.get_settings()["publish_delay"]
    if action == "resume":
        back = [ui.btn("↩️ Назад", "h:partial:0")]
        slugs, orphans = await asyncio.to_thread(resume_candidates)
        if not slugs:
            text = "Нет частичных постов, под которыми можно дослать комментарии."
            if orphans:
                text += (f"\nЧастичных без данных о комментарии: {orphans} — для них в карточке "
                         "кнопка «♻️ Переопубликовать».")
            return await ui.render(c.message, text, [back])
        if not confirmed:
            return await ui.render(
                c.message, f"🔁 Дослать недостающее под постами: <b>{len(slugs)}</b>\n"
                           f"Отправка продолжится с того места, где прервалась. Пауза между постами "
                           f"{delay} с (≈{util.human_duration(len(slugs) * delay)})."
                           + (f"\nЕщё {orphans} частичных без данных о комментарии пропущу — для них "
                              "«♻️ Переопубликовать» в карточке." if orphans else ""),
                [[ui.btn("✅ Да, в очередь", "hb:resume:yes")], [ui.btn("✖️ Отмена", "h:partial:0")]])
        n = await publishing.start_batch(c.message.chat.id, slugs, "дослать комментарии", mode="resume")
        return await ui.render(c.message, f"🔁 В очередь поставлено: {n}. Ход — в /channel.", [])
    if action == "local":
        ok, still = await asyncio.to_thread(recheck_skipped)
        back = [ui.btn("↩️ Назад", "h:skipped:0")]
        if not ok:
            return await ui.render(c.message, f"По данным библиотеки пропущенные посты по-прежнему не "
                                           f"подходят ({still}). Если вы меняли настройки (например, "
                                           "publish_video_posts) — проверьте их; либо «🌐 Перепроверить "
                                           "с сайта».", [back])
        if not confirmed:
            return await ui.render(
                c.message, f"🔄 Теперь подходят для публикации: <b>{len(ok)}</b> из {len(ok) + still} "
                           f"пропущенных. Остальные ({still}) останутся в истории.\nПауза между "
                           f"постами {delay} с (≈{util.human_duration(len(ok) * delay)}).",
                [[ui.btn("✅ Опубликовать подошедшие", "hb:local:yes")], [ui.btn("✖️ Отмена", "h:skipped:0")]])
        ok, _ = await asyncio.to_thread(recheck_skipped, True)
        n = await publishing.start_batch(c.message.chat.id, ok, "перепроверка пропущенных")
        return await ui.render(c.message, f"📣 В очередь публикации поставлено: {n}. Ход — в /channel.", [])
    if action == "web":
        slugs, urls = await asyncio.to_thread(skipped_posts)
        if not urls:
            return await ui.render(c.message, "Пропущенных постов нет.", [[ui.btn("↩️ Назад", "h:skipped:0")]])
        if not confirmed:
            return await ui.render(
                c.message, f"🌐 Заново открою на сайте пропущенные посты: <b>{len(urls)}</b> (с паузами "
                           "против блокировки), обновлю данные и опубликую те, что теперь подходят. "
                           "Это может занять много времени.",
                [[ui.btn("✅ Запустить", "hb:web:yes")], [ui.btn("✖️ Отмена", "h:skipped:0")]])
        return await sync_ui.queue_job(
            c.message, urls, source_message=c.message, recheck=True,
            status_text=f"📥 Перепроверяю {len(urls)} пропущенных постов на сайте, затем опубликую "
                        "подходящие.",
            extra={"publish_batch": slugs, "batch_label": "перепроверка с сайта"})


DUP_PAGE = 5


async def show_dups(m, page=0, keep=False):
    """Группы дубликатов в библиотеке (один сет на разных сайтах или повторная запись на одном)."""
    groups = await asyncio.to_thread(core.jm.lib.dup_groups)
    pages = max(1, math.ceil(len(groups) / DUP_PAGE))
    page = min(max(page, 0), pages - 1)
    lines = [f"🔁 <b>Дубликаты в библиотеке</b> — групп: {len(groups)}",
             "✅ — копия уже опубликована в канале; она стоит первой.", ""]
    rows_kb = []
    for i, g in enumerate(groups[page * DUP_PAGE:(page + 1) * DUP_PAGE], 1):
        lines.append(f"{i}. <b>{core.esc(g[0]['title'])}</b> — копий: {len(g)}")
        lines += [f"    {'✅' if r['is_published'] else '▫️'} {core.esc(publishing.site_title(r['site']))}, "
                  f"фото {r['nphotos'] or '?'}" for r in g[:6]]
        rows_kb.append([ui.btn(util.button_title(f"{i}. {g[0]['title']}", 42), f"i:{g[0]['post_id']}:dp:{page}")])
    if not groups:
        lines.append("Дубликатов не найдено.")
    rows_kb += ui.nav_rows(page, pages, lambda p: f"dp:{p}")
    rows_kb.append([ui.btn("✖️ Закрыть", "x:close")])
    await ui.render(m, util._fit(lines, 4000), rows_kb, keep=keep)


@ui.cb("dp")
async def cb_dups(c: CallbackQuery):
    await c.answer()
    await show_dups(c.message, int(c.data.split(":")[1]))


@core.router.message(Command("dups"))
async def cmd_dups(m: Message):
    await show_dups(m, 0, keep=True)


@core.router.message(Command("history"))
async def cmd_history(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    await show_history(m, arg if arg in mp.HIST_GROUPS else "all", 0, keep=True)


async def show_searches(m, page=0, keep=False):
    """История поиска за всё время (запросы косплеерш, игр, тегов…): нажмите, чтобы повторить."""
    rows, total = await asyncio.to_thread(core.jm.lib.search_list, core.SEARCH_PAGE, page * core.SEARCH_PAGE)
    pages = max(1, math.ceil(total / core.SEARCH_PAGE))
    if page >= pages:
        page = pages - 1
        rows, total = await asyncio.to_thread(core.jm.lib.search_list, core.SEARCH_PAGE, page * core.SEARCH_PAGE)
    lines = [f"🕘 <b>История поиска</b> — запросов: {total}", ""]
    rows_kb = []
    for i, r in enumerate(rows, 1):
        field = FIELD_LABELS.get(r["field"], "")
        lines.append(f"{i}. 🔎 <b>{core.esc(r['q'])}</b>" + (f" ({field})" if field else "")
                     + f" — в библиотеке {r['found']} · искали {r['hits']} раз · "
                       f"{(r.get('last_at') or '')[:10]}")
        rows_kb.append([ui.btn(util.button_title(f"{i}. {r['q']}", 42), f"sr:{r['id']}")])
    if not rows:
        lines.append("Пока пусто: запросы появятся после первого поиска.")
    rows_kb += ui.nav_rows(page, pages, lambda p: f"se:{p}")
    if total:
        rows_kb.append([ui.btn("📤 Экспорт CSV", "ex:s")])
        rows_kb.append([ui.btn("🗑 Очистить историю поиска", "sx:ask")])
    rows_kb.append([ui.btn("✖️ Закрыть", "x:close")])
    await ui.render(m, util._fit(lines, 4000), rows_kb, keep=keep)


@core.router.message(Command("searches"))
async def cmd_searches(m: Message):
    await show_searches(m, 0, keep=True)


@ui.cb("se")
async def cb_searches_page(c: CallbackQuery):
    await c.answer()
    await show_searches(c.message, int(c.data.split(":")[1]))


@ui.cb("sr")
async def cb_search_repeat(c: CallbackQuery):
    s = core.jm.lib.search_get(int(c.data.split(":")[1]))
    if not s:
        return await c.answer("Запись не найдена", show_alert=True)
    await c.answer()
    await search_ui.run_search(c.message, s["q"], s["field"] or None)


@ui.cb("sx")
async def cb_searches_clear(c: CallbackQuery):
    await c.answer()
    if c.data.endswith(":yes"):
        await asyncio.to_thread(core.jm.lib.search_clear)
        return await show_searches(c.message, 0)
    await ui.render(c.message, "Очистить всю историю поиска? Библиотека и посты не затрагиваются.",
                 [[ui.btn("✅ Да, очистить", "sx:yes")], [ui.btn("✖️ Отмена", "se:0")]])
