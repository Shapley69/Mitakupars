"""Выгрузка в CSV (/export)."""
import asyncio
import exports
import mitaku_parser as mp
import subs
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from . import core, delivery, history_ui, ui

async def send_csv(m, kind, group="all"):
    """Собирает CSV (UTF-8 с BOM, разделитель «;» — открывается в Excel) и присылает файлом."""
    today = mp.now_iso()[:10]
    if kind == "history":
        group = group if group in mp.HIST_GROUPS else "all"
        rows, _ = await asyncio.to_thread(core.jm.lib.hist_list, group, 10 ** 6, 0)
        for r in rows:
            r["status_label"] = mp.HIST_LABELS.get(r["status"], r["status"])
        data, name = exports.to_csv(rows, exports.HISTORY_COLUMNS), f"history_{group}_{today}.csv"
    elif kind == "searches":
        rows, _ = await asyncio.to_thread(core.jm.lib.search_list, 10 ** 6, 0)
        for r in rows:
            r["field_label"] = history_ui.FIELD_LABELS.get(r["field"], "всё")
        data, name = exports.to_csv(rows, exports.SEARCH_COLUMNS), f"searches_{today}.csv"
    elif kind == "subs":
        rows, _ = await asyncio.to_thread(core.jm.lib.sub_list, 10 ** 6, 0)
        for r in rows:
            r["kind_label"] = subs.KINDS.get(r["kind"], r["kind"])
        data, name = exports.to_csv(rows, exports.SUBS_COLUMNS), f"subscriptions_{today}.csv"
    elif kind == "library":
        rows = await asyncio.to_thread(core.jm.lib.export_rows)
        data, name = exports.to_csv(rows, exports.LIBRARY_COLUMNS), f"library_{today}.csv"
    else:
        return await m.answer("Использование: /export history [ok|partial|skipped|error] · searches · "
                              "subs · library")
    if len(data) > 49 * core.MB:
        return await m.answer(f"CSV получился {len(data) // core.MB} МБ — больше лимита Telegram. "
                              "Для истории выберите категорию (/export history skipped).")
    await delivery.tg_call(m.answer_document, BufferedInputFile(data, name),
                  caption=f"📤 {name}: строк {len(rows)}")


@core.router.message(Command("export"))
async def cmd_export(m: Message, command: CommandObject):
    words = (command.args or "").lower().split()
    await send_csv(m, words[0] if words else "", words[1] if len(words) > 1 else "all")


@ui.cb("ex")
async def cb_export(c: CallbackQuery):
    parts = c.data.split(":")
    await c.answer("Готовлю CSV…")
    kind = {"h": "history", "s": "searches", "u": "subs", "l": "library"}.get(parts[1], "")
    await send_csv(c.message, kind, parts[2] if len(parts) > 2 else "all")
