"""Поиск по библиотеке и сайтам: результаты, списки, команды /search /cosplayer /game /tag."""
import asyncio
import math
import mitaku_parser as mp
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from . import core, ui, util

def new_search(q, field):
    tok = next(core._tok)
    core.SEARCHES[tok] = {"q": q, "field": field, "items": []}
    for old in list(core.SEARCHES)[:-100]:
        core.SEARCHES.pop(old, None)
    return tok


@core.router.message(Command("list"))
async def cmd_list(m: Message, command: CommandObject):
    page = int(command.args) - 1 if (command.args or "").strip().isdigit() else 0
    tok = new_search("", None)
    await show_local(m, tok, max(page, 0), keep=True)


@core.router.message(Command("links"))
async def cmd_links(m: Message, command: CommandObject):
    text = mp.export_links(core.jm.lib, command.args or "")
    if not text.strip():
        return await m.answer("Ссылок на архивы не найдено.")
    await m.answer_document(BufferedInputFile(text.encode(), "links.txt"),
                            caption="Откройте ссылки сами (капча), итоговые Mediafire/Pixeldrain "
                                    "положите в links.txt и запустите: "
                                    "python mitaku_parser.py --archives links.txt")


# --------------------------------------------------------------------------- поиск
async def run_search(m: Message, q, field):
    q = (q or "").strip()
    if not q:
        return await m.answer("Что искать? Например: /cosplayer Byoru")
    tok = new_search(q, field)
    _, total = core.jm.lib.search(q, field, 1)
    await asyncio.to_thread(core.jm.lib.search_log, q, field, total)   # история поиска за всё время
    if total:
        return await show_local(m, tok, 0, keep=True)
    status = await m.answer("В библиотеке ничего нет, ищу на сайте…")
    await show_site(status, tok)


async def show_local(m, tok, page, keep=False):
    s = core.SEARCHES.get(tok)
    if not s:
        return await m.answer("Поиск устарел, повторите команду.")
    rows, total = core.jm.lib.search(s["q"], s["field"], core.PAGE, page * core.PAGE)
    label = s["q"] or "все посты"
    lines = [f"🔎 <b>{core.esc(label)}</b> — найдено {total}\n"]
    lines += [f"{i}. {util.fmt_row(r)}" for i, r in enumerate(rows, 1)]
    rows_kb = [[ui.btn(util.button_title(f"{i}. {r['title']}", 42), f"i:{r['id']}:s:{tok}:{page}")]
               for i, r in enumerate(rows, 1)]
    rows_kb += ui.nav_rows(page, max(1, math.ceil(total / core.PAGE)), lambda p: f"s:{tok}:{p}")
    if s["q"]:
        rows_kb.append([ui.btn("🌐 Искать на сайте", f"w:{tok}:{page}")])
        rows_kb.append([ui.btn("🔔 Подписаться на этот запрос", f"sn:q:{tok}")])
    if core.CHANNEL_ID and total:
        rows_kb.append([ui.btn("📣 Опубликовать всё в канал", f"pa:{tok}:l:{page}")])
    rows_kb.append([ui.btn("✖️ Закрыть", "x:close")])
    await ui.render(m, util._fit(lines, 4000), rows_kb, keep=keep)


async def show_site(m, tok, return_page=0, keep=False):
    s = core.SEARCHES[tok]
    back = [[ui.btn("↩️ К библиотеке", f"s:{tok}:{return_page}")]]
    try:
        items = await asyncio.to_thread(core.jm.scraper.site_search, s["q"], None,
                                        core.jm.get_active_sites())
    except Exception as e:  # noqa: BLE001
        return await ui.render(m, f"❌ Поиск на сайте не удался: {core.esc(str(e))}", back, keep=keep)
    s["items"] = items
    if not items:
        errs = "".join(f"\n⚠️ {core.esc(k)}: {core.esc(e[:150])}" for k, e in core.jm.scraper.search_errors)
        return await ui.render(m, "На выбранных сайтах тоже ничего не найдено (/sites)." + errs,
                            back, keep=keep)
    await render_site(m, tok, 0, return_page=return_page, keep=keep)


async def render_site(m, tok, page, return_page=0, keep=False):
    s = core.SEARCHES.get(tok)
    if not s:
        return await m.answer("Поиск устарел, повторите команду.")
    items = s["items"]
    start = page * core.SITE_PAGE
    page_items = items[start:start + core.SITE_PAGE]
    lines = [f"🌐 На сайтах по запросу <b>{core.esc(s['q'])}</b> — "
             f"найдено {len(items)} (страница {page + 1}):\n"]
    rows_kb = []
    known = core.jm.lib.existing_slugs([mp.slug_of(it["url"]) for it in page_items])   # один запрос
    for i, it in enumerate(page_items, start):
        have = " ✅" if mp.slug_of(it["url"]) in known else ""
        lines.append(f"{i + 1}. [{core.short(it.get('site'))}] {core.esc(it['title'])}{have}")
        rows_kb.append([ui.btn(util.button_title(f"{i + 1}. {core.short(it.get('site'))} {it['title']}", 42),
                            f"d:{tok}:{i}:{page}:{return_page}")])
    rows_kb += ui.nav_rows(page, max(1, math.ceil(len(items) / core.SITE_PAGE)),
                        lambda p: f"sp:{tok}:{p}:{return_page}")
    rows_kb.append([ui.btn("⬇️ Скачать все найденные", f"da:{tok}:{page}:{return_page}")])
    if core.CHANNEL_ID:
        rows_kb.append([ui.btn("📣 Опубликовать всё в канал", f"pa:{tok}:w:{page}:{return_page}")])
    rows_kb.append([ui.btn("↩️ К библиотеке", f"s:{tok}:{return_page}")])
    rows_kb.append([ui.btn("✖️ Закрыть", "x:close")])
    await ui.render(m, util._fit(lines, 4000), rows_kb, keep=keep)


@core.router.message(Command("search"))
async def cmd_search(m: Message, command: CommandObject):
    await run_search(m, command.args, None)


@core.router.message(Command("cosplayer"))
async def cmd_cosplayer(m: Message, command: CommandObject):
    await run_search(m, command.args, "cosplayer")


@core.router.message(Command("game"))
async def cmd_game(m: Message, command: CommandObject):
    await run_search(m, command.args, "game")


@core.router.message(Command("character"))
async def cmd_character(m: Message, command: CommandObject):
    await run_search(m, command.args, "character")


@core.router.message(Command("tag"))
async def cmd_tag(m: Message, command: CommandObject):
    await run_search(m, command.args, "tag")


# --------------------------------------------------------------------------- кнопки
@ui.cb("s")
async def cb_page(c: CallbackQuery):
    _, tok, page = c.data.split(":")
    await show_local(c.message, int(tok), int(page))
    await c.answer()


@ui.cb("x")
async def cb_close(c: CallbackQuery):
    await ui.drop_reply_keyboard(c.message.bot, c.message.chat.id)
    await util.delete_message(c.message)
    await c.answer()


@ui.cb("w")
async def cb_web(c: CallbackQuery):
    parts = c.data.split(":")
    tok = int(parts[1])
    return_page = int(parts[2]) if len(parts) > 2 else 0
    if tok not in core.SEARCHES:
        return await c.answer("Поиск устарел", show_alert=True)
    await c.answer("Ищу на сайте…")
    await show_site(c.message, tok, return_page=return_page)


@ui.cb("sp")
async def cb_site_page(c: CallbackQuery):
    parts = c.data.split(":")
    tok, page = int(parts[1]), int(parts[2])
    return_page = int(parts[3]) if len(parts) > 3 else 0
    await render_site(c.message, tok, page, return_page=return_page)
    await c.answer()
