"""Кнопки: inline и нижняя клавиатура (строка ввода), экраны render(), листание, регистрация обработчиков."""
import asyncio
import json
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from contextlib import suppress
from . import core, util

def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


# --- Кнопки: до INLINE_MAX штук — под сообщением, больше — на нижней клавиатуре (строка ввода).
# Нажатие кнопки нижней клавиатуры приходит текстом; MENUS[chat] помнит «подпись -> callback_data»
# (и в БД, чтобы переживать перезапуск), а on_menu_tap запускает тот же обработчик, что и inline.
MENUS: dict = {}
CB_TABLE: dict = {}   # префикс callback_data -> обработчик


def cb(prefix):
    """Регистрирует обработчик inline-кнопок `prefix:...` и нижней клавиатуры."""
    def deco(fn):
        CB_TABLE[prefix] = fn
        core.router.callback_query(F.data.startswith(prefix + ":"))(fn)
        return fn
    return deco


def _set_menu(chat_id, actions):
    MENUS[chat_id] = actions
    if core.jm is not None:
        with suppress(RuntimeError):
            asyncio.get_running_loop().run_in_executor(
                None, core.jm.lib.kv_set, f"menu:{chat_id}", json.dumps(actions, ensure_ascii=False))


def menu_action(chat_id, text):
    if chat_id not in MENUS:
        raw = core.jm.lib.kv_get(f"menu:{chat_id}") if core.jm is not None else None
        MENUS[chat_id] = json.loads(raw) if raw else {}
    return MENUS[chat_id].get(text)


def build_markup(chat_id, rows):
    rows = [list(r) for r in rows if r]
    if sum(len(r) for r in rows) <= core.INLINE_MAX:
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    actions, keyboard = {}, []
    for row in rows:
        line = []
        for b in row:
            label = b.text
            while label in actions:       # подписи на экране должны быть уникальны
                label += "\u2063"
            actions[label] = b.callback_data
            line.append(KeyboardButton(text=label))
        keyboard.append(line)
    _set_menu(chat_id, actions)
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True,
                               input_field_placeholder="Выберите кнопку…")


async def drop_reply_keyboard(bot, chat_id):
    """Убирает нижнюю клавиатуру (если она была): невидимое сообщение с ReplyKeyboardRemove."""
    menu_action(chat_id, None)   # после перезапуска подгружает сохранённое состояние из БД
    if not MENUS.get(chat_id):
        return
    _set_menu(chat_id, {})
    with suppress(Exception):
        note = await bot.send_message(chat_id, "⌨️", reply_markup=ReplyKeyboardRemove())
        await bot.delete_message(chat_id, note.message_id)


async def make_markup(bot, chat_id, rows):
    markup = build_markup(chat_id, rows)
    if not isinstance(markup, ReplyKeyboardMarkup):
        await drop_reply_keyboard(bot, chat_id)
    return markup


async def render(msg, text, rows, keep=False):
    """Показывает экран: текст + кнопки. Если можно — правит сообщение бота на месте, иначе
    присылает новое и удаляет старое (или нажатие пользователя). keep=True — старое не трогать."""
    markup = await make_markup(msg.bot, msg.chat.id, rows)
    from_bot = bool(msg.from_user and msg.from_user.is_bot)
    if (not keep and from_bot and msg.text is not None
            and not isinstance(markup, ReplyKeyboardMarkup)):
        try:
            await msg.edit_text(text, reply_markup=markup)
            return msg
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return msg
    sent = await msg.answer(text, reply_markup=markup)
    if not keep:
        await util.delete_message(msg)
    return sent


def nav_rows(page, pages, data_fn):
    """Строка листания: ⏮ в начало · ◀️ назад · 📄 стр/всего · вперёд ▶️ · в конец ⏭.
    data_fn(номер_страницы) -> callback_data. Для одной страницы кнопок нет."""
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row += [btn("⏮ В начало", data_fn(0)), btn("◀️ Назад", data_fn(page - 1))]
    row.append(btn(f"📄 {page + 1}/{pages}", "nop:0"))
    if page < pages - 1:
        row += [btn("Вперёд ▶️", data_fn(page + 1)), btn("В конец ⏭", data_fn(pages - 1))]
    return [row]


@cb("nop")
async def cb_nop(c: CallbackQuery):
    await c.answer()


class Tap:
    """Нажатие кнопки нижней клавиатуры в виде, совместимом с CallbackQuery."""

    def __init__(self, message, data):
        self.message, self.data, self.from_user = message, data, message.from_user

    async def answer(self, text=None, show_alert=False, **_):
        if text and show_alert:
            note = await self.message.answer(text)
            asyncio.get_running_loop().call_later(
                8, lambda: asyncio.ensure_future(util.delete_message_quiet(note)))
