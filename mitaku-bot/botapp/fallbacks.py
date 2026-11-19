"""Обработчики «по умолчанию»: нажатия нижней клавиатуры, ссылки на архивы, текстовый поиск. Подключаются последними — порядок регистрации важен."""
from aiogram import F
from aiogram.types import Message
from ouo_downloader import extract_archive_urls
from . import archive_links, core, search_ui, ui, util

@core.router.message(F.text, lambda m: bool(ui.menu_action(m.chat.id, m.text)))
async def on_menu_tap(m: Message):
    """Нажатие кнопки нижней клавиатуры — выполняем то же, что и для inline-кнопки."""
    data = ui.menu_action(m.chat.id, m.text)
    handler = ui.CB_TABLE.get(data.split(":", 1)[0])
    if not handler:
        return
    try:
        await handler(ui.Tap(m, data))
    finally:
        await util.delete_message_quiet(m)


@core.router.message(lambda m: m.text and not m.text.startswith("/") and extract_archive_urls(m.text))
async def handle_archive_link(m: Message):
    for url in extract_archive_urls(m.text)[:5]:
        await archive_links.deliver_archive(m, url)


@core.router.message(F.text & ~F.text.startswith("/"))
async def plain_text(m: Message):
    await search_ui.run_search(m, m.text, None)
