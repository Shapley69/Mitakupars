"""Запуск: сборка диспетчера, фоновые задачи, события парсера, основной цикл."""
import asyncio
import logging
import shutil
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand
from contextlib import suppress
from mitaku_jobs import JobManager
from mtproto_sender import MTSender
from . import cards, core, delivery, importer, postfiles, publishing, subs_ui, system, ui

# --------------------------------------------------------------------------- запуск
async def main():
    if not core.TOKEN:
        sys.exit("Задайте BOT_TOKEN (переменные окружения или файл .env)")
    if "--logout" in sys.argv:  # разовый выход с облачного api.telegram.org (перед локальным сервером)
        cloud = Bot(core.TOKEN)
        print("logOut:", await cloud.log_out())
        await cloud.session.close()
        return
    if not core.ADMINS:
        sys.exit("Задайте ADMIN_IDS (Telegram ID через запятую)")
    logging.basicConfig(level=logging.INFO)
    if not core.DATABASE_URL:
        system.restore_database()
    api = (TelegramAPIServer.from_base(core.LOCAL_API_URL, is_local=True) if core.LOCAL_API_URL
           else None)
    session = (AiohttpSession(api=api, timeout=core.UPLOAD_TIMEOUT) if api
               else AiohttpSession(timeout=core.UPLOAD_TIMEOUT))
    bot = core.TrackingBot(core.TOKEN, session=session,
                      default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                   link_preview_is_disabled=True))
    core.BOT = bot
    core.PUBLISH_Q = asyncio.Queue()
    core.PUBLISH_LOCK = asyncio.Lock()
    shutil.rmtree(core.STAGING, ignore_errors=True)  # остатки прошлого запуска
    loop = asyncio.get_running_loop()

    async def update_job_message(job, text=None, delete=False, reply_markup=None):
        message_id = job.get("status_message_id")
        if not message_id:
            if text:
                await bot.send_message(
                    job["chat_id"], text, reply_markup=reply_markup)
            return
        try:
            if delete:
                await bot.delete_message(job["chat_id"], message_id)
            elif text:
                await bot.edit_message_text(
                    text, chat_id=job["chat_id"], message_id=message_id,
                    reply_markup=reply_markup)
        except TelegramBadRequest as e:
            logging.warning("Не удалось обновить сообщение задачи #%s: %s", job["id"], e)
            if delete:
                bot.forget_message(job["chat_id"], message_id)
            if text and not delete:
                status = await bot.send_message(
                    job["chat_id"], text, reply_markup=reply_markup)
                job["status_message_id"] = status.message_id

    async def send_job_event(job, kind, text):
        if kind == "started":
            await update_job_message(
                job, f"⏳ Задача #{job['id']} выполняется. Статус — /status")
            return
        if kind != "finished" or not job.get("result_slug"):
            markup = (ui.kb([ui.btn("↩️ Назад", job["back_data"])])
                      if job.get("back_data") else None)
            await update_job_message(job, text, reply_markup=markup)
            return
        if not job.get("result_ready"):
            markup = (ui.kb([ui.btn("↩️ Назад", job["back_data"])])
                      if job.get("back_data") else None)
            await update_job_message(job, text, reply_markup=markup)
            return
        rec = core.jm.lib.get(job["result_slug"])
        if not rec:
            await update_job_message(
                job, "Не удалось сохранить пост в библиотеку — ZIP и файлы недоступны.")
            return
        files = postfiles.post_files(rec)
        if not files and rec["status"] == "partial":
            await update_job_message(
                job, "Пост не удалось скачать: в библиотеке нет доступных медиафайлов.")
            return
        await cards.show_download_result(
            bot, job["chat_id"], rec, partial=rec["status"] == "partial",
            back_data=job.get("back_data"))
        await update_job_message(job, delete=True)

    def on_event(job, kind, text):  # вызывается из рабочего потока
        if kind == "post":  # пост только что сохранён в библиотеку
            slug = text.split(":", 1)[1]
            if job.get("flags", {}).get("publish") and text.startswith("new:"):
                loop.call_soon_threadsafe(publishing.enqueue_publish, slug, False)
            if text.startswith("new:") and not importer.import_active():
                try:
                    rec, hits = subs_ui.sub_check(slug)
                    if hits:
                        loop.call_soon_threadsafe(subs_ui.SUB_QUEUE.append, (rec, hits))
                except Exception:  # noqa: BLE001
                    logging.exception("Проверка подписок для %s", slug)
            return
        if kind == "finished" and job.get("publish_batch"):  # «опубликовать всё»: посты загружены
            asyncio.run_coroutine_threadsafe(
                publishing.start_batch_and_notify(job["chat_id"], job["publish_batch"],
                                       job.get("batch_label", "")), loop)
        if kind == "finished" and job.get("baseline_site"):  # первая проверка сайта завершена
            core.jm.lib.kv_set(f"baseline:{job['baseline_site']}", "1")
        if job.get("silent") and kind in ("started", "finished"):
            return
        if not job.get("chat_id"):
            return
        future = asyncio.run_coroutine_threadsafe(send_job_event(job, kind, text), loop)

        def report_error(done):
            try:
                done.result()
            except Exception:
                logging.exception("Не удалось отправить уведомление о задаче #%s", job["id"])

        future.add_done_callback(report_error)

    core.jm = JobManager(core.OUT, on_event)
    dp = Dispatcher()
    dp.include_router(core.channel_router)
    dp.include_router(core.router)
    asyncio.create_task(publishing.publisher())
    asyncio.create_task(publishing.watcher())
    asyncio.create_task(subs_ui.sub_digest_loop())
    await bot.set_my_commands([
        BotCommand(command="sites", description="Выбрать сайты"),
        BotCommand(command="sync", description="Парсить сайт (кнопками)"),
        BotCommand(command="update", description="Только новое"),
        BotCommand(command="channel", description="Публикация в канал"),
        BotCommand(command="import", description="Импорт всего сайта в канал"),
        BotCommand(command="history", description="История публикаций"),
        BotCommand(command="searches", description="История поиска"),
        BotCommand(command="mediafire", description="Скачать с Mediafire"),
        BotCommand(command="mtproto", description="MTProto: файлы до 2 ГБ"),
        BotCommand(command="videos", description="Видео в библиотеке"),
        BotCommand(command="subs", description="Подписки на косплееров"),
        BotCommand(command="dups", description="Дубликаты в библиотеке"),
        BotCommand(command="export", description="Выгрузка в CSV"),
        BotCommand(command="publish", description="Опубликовать пост в канал"),
        BotCommand(command="get", description="Добавить один пост"),
        BotCommand(command="pause", description="Пауза"),
        BotCommand(command="resume", description="Продолжить"),
        BotCommand(command="stop", description="Остановить"),
        BotCommand(command="continue", description="Продолжить незавершённую задачу"),
        BotCommand(command="status", description="Прогресс"),
        BotCommand(command="search", description="Поиск"),
        BotCommand(command="cosplayer", description="Поиск по нику"),
        BotCommand(command="game", description="Поиск по игре/аниме"),
        BotCommand(command="list", description="Последние посты"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="links", description="Ссылки на архивы"),
        BotCommand(command="set", description="Настройки"),
    ])
    if core.MT_ENABLED:
        try:
            core.MT = MTSender(core.MT_API_ID, core.MT_API_HASH, core.TOKEN, delivery.KvStore(core.jm.lib))
            await core.MT.start()
        except Exception as e:  # noqa: BLE001
            logging.exception("MTProto не запустился")
            core.MT = None
            await delivery.notify_admins(f"⚠️ MTProto не запустился: {core.esc(str(e))}\nФайлы больше 50 МБ "
                                "отправляться не будут. Проверьте MT_API_ID / MT_API_HASH.")
    delivery.apply_limits()
    interrupted = importer.import_load()
    if interrupted and interrupted.get("status") == "running":   # бот перезапустили посреди импорта
        interrupted["status"] = "interrupted"
        importer.import_save(interrupted)
        await delivery.notify_admins(
            f"⚠️ Импорт {core.esc(publishing.site_title(interrupted['site']))} прерван перезапуском бота на "
            f"странице {interrupted['page']} (пост {interrupted['index']}). Позиция сохранена — "
            "/import, чтобы продолжить.")
    if core.jm.pending_job():
        for admin in core.ADMINS:
            try:
                await bot.send_message(admin, "⚠️ Есть незавершённая задача — /continue")
            except Exception:  # noqa: BLE001
                pass
    health_server = await asyncio.start_server(
        system.health_check, "0.0.0.0", core.HEALTH_PORT)
    logging.info("Health endpoint слушает 0.0.0.0:%s/health", core.HEALTH_PORT)
    backup_task = (asyncio.create_task(system.daily_backup(bot, core.jm.lib))
                   if not core.jm.lib.postgres else None)
    try:
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        if backup_task:
            backup_task.cancel()
            try:
                await backup_task
            except asyncio.CancelledError:
                pass
            try:
                await system.backup_database(bot, core.jm.lib)
            except Exception:
                logging.exception("Не удалось создать резервную копию при завершении")
        if core.MT is not None:
            with suppress(Exception):
                await core.MT.stop()
        health_server.close()
        await health_server.wait_closed()
        if core.jm.lib.postgres:
            core.jm.lib.db.close()
        await bot.session.close()


# Модули с обработчиками подключаются здесь: сам импорт регистрирует их в роутере aiogram.
# fallbacks — последним: порядок регистрации «нажатие кнопки → ссылка на архив → текстовый поиск» важен.
from . import (sync_ui, system, search_ui, cards, archive_links, channel_ui, history_ui, subs_ui,  # noqa: E402,F401
               export_ui)
from . import fallbacks  # noqa: E402,F401
