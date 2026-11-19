"""Подписки на косплееров и уведомления о новых постах."""
import asyncio
import math
import subs
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from . import core, delivery, publishing, ui, util

SUB_PAGE = 8
SUB_QUEUE: list = []   # (slug, [подписки]) — новые посты, под которые подходят подписки


def sub_check(slug):
    """Подписки, под которые подходит только что сохранённый пост (вызывается из потока парсера)."""
    rec = core.jm.lib.get(slug)
    hits = core.jm.lib.sub_match(rec) if rec else []
    if hits:
        core.jm.lib.sub_hit([h["id"] for h in hits])
    return rec, hits


async def sub_digest_loop():
    """Раз в 20 секунд собирает совпавшие посты в одно сообщение, чтобы не засыпать админов."""
    while True:
        await asyncio.sleep(20)
        if not SUB_QUEUE:
            continue
        items = SUB_QUEUE[:]
        SUB_QUEUE.clear()
        lines = [f"🔔 <b>Новые посты по подпискам: {len(items)}</b>"]
        buttons = []
        for rec, hits in items[:12]:
            names = ", ".join(core.esc(h["value"]) for h in hits[:3])
            lines.append(f"• <b>{core.esc(rec['title'])}</b> — {core.esc(rec.get('cosplayer') or '')} "
                         f"({core.esc(publishing.site_title(rec.get('site')))}) · по подписке: {names}")
            if len(buttons) < 3:
                buttons.append([ui.btn(util.button_title(rec["title"], 40), f"i:{rec['id']}")])
        if len(items) > 12:
            lines.append(f"…и ещё {len(items) - 12}")
        await delivery.notify_admins(util._fit(lines, 4000), InlineKeyboardMarkup(inline_keyboard=buttons))


async def show_subs(m, page=0, keep=False):
    rows, total = await asyncio.to_thread(core.jm.lib.sub_list, SUB_PAGE, page * SUB_PAGE)
    pages = max(1, math.ceil(total / SUB_PAGE))
    if page >= pages:
        page = pages - 1
        rows, total = await asyncio.to_thread(core.jm.lib.sub_list, SUB_PAGE, page * SUB_PAGE)
    lines = [f"🔔 <b>Подписки</b> — {total}",
             "Когда парсер находит новый пост, подходящий под подписку, бот пишет вам.",
             "Добавить: <code>/sub Byoru</code> · <code>/sub игра NIKKE</code> · "
             "<code>/sub тег cosplay</code> · <code>/sub персонаж Rabbit</code> · "
             "<code>/sub текст любое слово</code>", ""]
    rows_kb = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {subs.KINDS.get(r['kind'], r['kind'])}: <b>{core.esc(r['value'])}</b> — "
                     f"срабатываний {r['hits'] or 0}"
                     + (f", последний раз {r['last_hit_at'][:10]}" if r.get("last_hit_at") else ""))
        rows_kb.append([ui.btn(util.button_title(f"🗑 {i}. {r['value']}", 42), f"su:{r['id']}:{page}")])
    if not rows:
        lines.append("Пока пусто.")
    rows_kb += ui.nav_rows(page, pages, lambda p: f"sl:{p}")
    if total:
        rows_kb.append([ui.btn("📤 Экспорт CSV", "ex:u")])
    rows_kb.append([ui.btn("✖️ Закрыть", "x:close")])
    await ui.render(m, util._fit(lines, 4000), rows_kb, keep=keep)


@core.router.message(Command("subs"))
async def cmd_subs(m: Message):
    await show_subs(m, 0, keep=True)


@core.router.message(Command("sub"))
async def cmd_sub(m: Message, command: CommandObject):
    """/sub <значение> — по косплееру; /sub игра|персонаж|тег|текст <значение>."""
    words = (command.args or "").split(maxsplit=1)
    if not words:
        return await m.answer("Использование: <code>/sub Byoru</code> (косплеер), "
                              "<code>/sub игра NIKKE</code>, <code>/sub персонаж Rabbit</code>, "
                              "<code>/sub тег cosplay</code>, <code>/sub текст слово</code>. "
                              "Список и удаление: /subs")
    kind = subs.parse_kind(words[0]) if len(words) > 1 else None
    value = words[1] if kind else " ".join(words)
    kind = kind or "cosplayer"
    try:
        sub_id, created = await asyncio.to_thread(core.jm.lib.sub_add, kind, value)
    except ValueError as e:
        return await m.answer(f"❌ {core.esc(str(e))}")
    await m.answer(f"🔔 Подписка {'создана' if created else 'уже есть'}: "
                   f"{subs.KINDS[kind]} <b>{core.esc(value)}</b>. Все подписки — /subs")


@ui.cb("sl")
async def cb_subs_page(c: CallbackQuery):
    await c.answer()
    await show_subs(c.message, int(c.data.split(":")[1]))


@ui.cb("su")
async def cb_sub_delete(c: CallbackQuery):
    _, sub_id, page = c.data.split(":")
    await asyncio.to_thread(core.jm.lib.sub_delete, int(sub_id))
    await c.answer("Подписка удалена")
    await show_subs(c.message, int(page))


@ui.cb("sn")
async def cb_sub_new(c: CallbackQuery):
    """Подписка из карточки поста (на косплеера) или из результатов поиска (на запрос)."""
    parts = c.data.split(":")
    if parts[1] == "q":
        s = core.SEARCHES.get(int(parts[2]))
        if not s:
            return await c.answer("Поиск устарел", show_alert=True)
        kind = s["field"] if s["field"] in ("cosplayer", "game", "character", "tag") else "any"
        value = s["q"]
    else:
        r = core.jm.lib.get_by_id(int(parts[1]))
        if not r or not r.get("cosplayer"):
            return await c.answer("У поста нет ника косплеера", show_alert=True)
        kind, value = "cosplayer", r["cosplayer"]
    _, created = await asyncio.to_thread(core.jm.lib.sub_add, kind, value)
    await c.answer(f"🔔 Подписка создана: {value}" if created else "Такая подписка уже есть",
                   show_alert=True)
