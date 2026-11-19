"""Команды парсера: /sync, /update, /get, /status, /set, выбор сайтов и страниц, очередь задач."""
import mitaku_parser as mp
import mitaku_sites as ms
import re
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from . import core, importer, ui, util

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
    for key in (sites or core.jm.get_active_sites()):
        secs = ms.SITES[key].sections()
        name = section or "all"
        if name not in secs:
            warns.append(f"{ms.SITES[key].title}: нет раздела «{name}» (есть: {', '.join(secs)})")
            continue
        urls.append((key, secs[name]))
    return urls, warns


async def queue_job(message, urls, *, source_message=None, status_text=None, **kwargs):
    if importer.import_active():   # импорт сам использует парсер
        await message.answer("⛔ Идёт импорт сайта — парсер занят. Остановите его: /import.")
        return None
    await ui.drop_reply_keyboard(message.bot, message.chat.id)
    status = await message.answer(status_text or "📥 Задача добавлена в очередь.")
    job = core.jm.submit(urls, chat_id=message.chat.id,
                    status_message_id=status.message_id, **kwargs)
    if source_message:
        await util.delete_message(source_message)
    return job


async def choose_site(m, mode, edit=False):
    act = core.jm.get_active_sites()
    rows = [[ui.btn(("✅ " if k in act else "") + site.title, f"sy:{mode}:{k}")]
            for k, site in ms.SITES.items()]
    rows.append([ui.btn("🌐 Все выбранные (/sites)", f"sy:{mode}:act")])
    text = "С какого сайта " + ("парсить?" if mode == "s" else "обновить?")
    await ui.render(m, text, rows, keep=not edit)


async def start_sync(m, args, mode):
    if not (args or "").strip():
        return await choose_site(m, mode)
    sites, section, first, last, flags = parse_sync_args(args, 5 if mode == "s" else 50)
    targets, warns = section_targets(sites, section)
    if not targets:
        return await m.answer("❌ " + ("\n".join(core.esc(w) for w in warns) or
                                       "Не выбран ни один сайт — /sites"))
    names = ", ".join(ms.SITES[k].title for k, _ in targets)
    prefix = "Обновление" if mode == "u" else "Загрузка"
    await queue_job(m, [u for _, u in targets], first=first, last=last,
                    status_text=f"📥 {prefix}: {core.esc(names)}, страницы {first}–{last}."
                                + ("\n" + "\n".join(core.esc(w) for w in warns) if warns else ""),
                    until_known=(mode == "u"), **flags)


@core.router.message(Command("sync"))
async def cmd_sync(m: Message, command: CommandObject):
    await start_sync(m, command.args, "s")


@core.router.message(Command("update"))
async def cmd_update(m: Message, command: CommandObject):
    await start_sync(m, command.args, "u")


@core.router.message(Command("sites"))
async def cmd_sites(m: Message):
    await show_sites(m)


async def show_sites(m, edit=False):
    act = core.jm.get_active_sites()
    rows = [[ui.btn(("✅ " if k in act else "⬜ ") + site.title, f"st:{k}")]
            for k, site in ms.SITES.items()]
    rows.append([ui.btn("Готово", "st:done")])
    text = ("Активные сайты — их используют /sync, /update, поиск на сайте и автопубликация "
            "в канал.\n🧪 — экспериментальные адаптеры, не проверены на живом сайте.")
    await ui.render(m, text, rows, keep=not edit)


@core.router.message(Command("get"))
async def cmd_get(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        return await m.answer("Использование: /get &lt;ссылка на пост или slug&gt;")
    if mp.is_post_url(arg):
        url, flags = arg, {}
    else:
        rec = core.jm.lib.get(arg)
        if not rec:
            return await m.answer("Такого slug нет в библиотеке. Пришлите полную ссылку.")
        url, flags = rec["url"], {"recheck": True}
    await queue_job(m, [url], result_slug=mp.slug_of(url), **flags)


@core.router.message(Command("pause"))
async def cmd_pause(m: Message):
    core.jm.pause()
    await m.answer("⏸ Пауза (текущие загрузки замрут на ближайшем куске). /resume — продолжить.")


@core.router.message(Command("resume"))
async def cmd_resume(m: Message):
    core.jm.resume()
    await m.answer("▶️ Продолжаю.")


@core.router.message(Command("stop"))
async def cmd_stop(m: Message):
    core.jm.stop()
    text = "⏹ Останавливаю, очередь очищена. Прогресс сохранён — /continue продолжит."
    if importer.import_active():
        importer.IMPORT["stop"] = True
        text += "\n📥 Импорт сайта тоже остановлен: страница и пост сохранены, продолжить — /import."
    await m.answer(text)


@core.router.message(Command("continue"))
async def cmd_continue(m: Message):
    status = await m.answer("📥 Продолжение добавлено в очередь.")
    job = core.jm.resume_last(chat_id=m.chat.id, status_message_id=status.message_id)
    if not job:
        await util.delete_message(status)
        return await m.answer("Незавершённых задач нет.")


@core.router.message(Command("status"))
async def cmd_status(m: Message):
    st = core.jm.status()
    p = st["progress"]
    icon = {"idle": "💤", "running": "⏳", "paused": "⏸"}.get(st["state"], "•")
    if st["state"] == "idle":
        text = f"{icon} Простаиваю. В очереди: {st['queued']}.\nПоследняя задача: {mp.summary_text(p)}"
        pend = core.jm.pending_job()
        if pend:
            text += f"\n⚠️ Есть незавершённая задача #{pend['id']} ({pend['state']}) — /continue"
        return await m.answer(text)
    job = st["job"]
    await m.answer(
        f"{icon} Задача #{job['id']} — {st['state']}\n"
        f"Страница: {p['page']}\nПост: {core.esc(p['post'])}\n"
        f"Файлы поста: {p['file_i']}/{p['file_n']}\n"
        f"Итого: {mp.summary_text(p)}\nВ очереди: {st['queued']}")


@core.router.message(Command("stats"))
async def cmd_stats(m: Message):
    s = core.jm.lib.stats()
    secs = ", ".join(f"{k}: {v}" for k, v in s["sections"].items())
    sites = ", ".join(f"{core.short(k)}: {v}" for k, v in s["sites"].items())
    await m.answer(f"📚 Постов: {s['posts']} ({sites})\nКосплееров: {s['cosplayers']}\n"
                   f"Хранится на диске: {mp.human_size(s['bytes'])}\nСтатусы: {s['status']}\n"
                   f"Разделы: {secs}")


@core.router.message(Command("set"))
async def cmd_set(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) == 2:
        try:
            v = core.jm.set_setting(parts[0].lower(), parts[1])
            await m.answer(f"✅ {parts[0]} = {v} (применится к следующей задаче)")
        except ValueError as e:
            await m.answer(f"❌ {core.esc(str(e))}")
        return
    s = core.jm.get_settings()
    await m.answer("⚙️ Настройки:\n" + "\n".join(f"{k} = {v}" for k, v in s.items())
                   + "\n\nИзменить: /set zip on")


# --------------------------------------------------------------------------- выбор сайта кнопками
async def ask_pages(m, mode, site, section):
    if mode == "u":
        return await run_sync_choice(m, mode, site, section, 50)
    title = "выбранные сайты" if site == "act" else ms.SITES[site].title
    rows = [[ui.btn(f"{n} стр.", f"sg:{mode}:{site}:{section}:{n}") for n in core.PAGES_CHOICES],
            [ui.btn("↩️ Назад", f"sb:{mode}")]]
    await ui.render(m, f"{core.esc(title)}: сколько страниц брать?", rows)


async def run_sync_choice(m, mode, site, section, n):
    sites = [] if site == "act" else [site]
    targets, warns = section_targets(sites, None if section == "all" else section)
    if not targets:
        return await ui.render(m, "Не выбран ни один сайт — /sites", [])
    names = ", ".join(ms.SITES[k].title for k, _ in targets)
    prefix = "Обновление" if mode == "u" else "Загрузка"
    await queue_job(m, [u for _, u in targets], source_message=m, first=1, last=n,
                    until_known=(mode == "u"),
                    status_text=f"📥 {prefix}: {core.esc(names)}, до {n} стр.")


@ui.cb("sy")
async def cb_sync_site(c: CallbackQuery):
    _, mode, site = c.data.split(":")
    await c.answer()
    if site != "act":
        secs = list(ms.SITES[site].sections())
        if len(secs) > 1:
            rows = [[ui.btn(f"📂 {n}", f"sc:{mode}:{site}:{n}") for n in secs[i:i + 2]]
                    for i in range(0, len(secs), 2)]
            rows.append([ui.btn("↩️ Назад", f"sb:{mode}")])
            return await ui.render(c.message, f"{core.esc(ms.SITES[site].title)}: какой раздел?", rows)
    await ask_pages(c.message, mode, site, "all")


@ui.cb("sc")
async def cb_sync_section(c: CallbackQuery):
    _, mode, site, section = c.data.split(":", 3)
    await c.answer()
    await ask_pages(c.message, mode, site, section)


@ui.cb("sb")
async def cb_sync_back(c: CallbackQuery):
    await c.answer()
    await choose_site(c.message, c.data.split(":")[1], edit=True)


@ui.cb("sg")
async def cb_sync_go(c: CallbackQuery):
    _, mode, site, section, n = c.data.split(":")
    await c.answer()
    await run_sync_choice(c.message, mode, site, section, int(n))


@ui.cb("st")
async def cb_site_toggle(c: CallbackQuery):
    key = c.data.split(":")[1]
    if key == "done":
        await ui.drop_reply_keyboard(c.message.bot, c.message.chat.id)
        await util.delete_message(c.message)
        return await c.answer()
    core.jm.toggle_site(key)
    await c.answer()
    await show_sites(c.message, edit=True)
