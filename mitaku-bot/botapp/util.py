"""Мелкие функции форматирования: обрезка HTML-текста, подписи кнопок, длительности."""
import logging
import re
from aiogram.exceptions import TelegramBadRequest
from contextlib import suppress
from . import core

# --------------------------------------------------------------------------- вспомогательное
def fmt_row(r):
    bits = [x for x in (r.get("cosplayer"), r.get("source"), r.get("character")) if x]
    mark = " ⚠️" if r["status"] == "partial" else ""
    return f"[{core.short(r.get('site'))}] {core.esc(r['title'])}{mark}" + (f"\n    <i>{core.esc(' / '.join(bits))}</i>" if bits else "")


def _fit(lines, limit):
    """Отбрасывает строки с конца, пока текст не влезет (не режем посреди HTML-тега)."""
    lines = list(lines)
    while len("\n".join(lines)) > limit and len(lines) > 1:
        lines.pop()
    return "\n".join(lines)


async def delete_message_quiet(message):
    with suppress(TelegramBadRequest):
        await message.delete()


def button_title(title, limit=48):
    title = re.sub(r"\s+", " ", title or "").strip() or "Без названия"
    return title if len(title) <= limit else title[:limit - 1] + "…"


async def delete_message(message):
    try:
        await message.delete()
    except TelegramBadRequest as e:
        logging.warning("Не удалось удалить сообщение %s: %s", message.message_id, e)
        if isinstance(message.bot, core.TrackingBot):
            message.bot.forget_message(message.chat.id, message.message_id)


def human_duration(seconds):
    minutes = max(int(seconds // 60), 1)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
