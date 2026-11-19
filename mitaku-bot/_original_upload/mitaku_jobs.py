"""
mitaku_jobs.py — менеджер фоновых задач поверх mitaku_parser.

Не зависит от Telegram-библиотек: его можно подключить к любому боту
(aiogram, python-telegram-bot, telebot...) или к веб-интерфейсу.

    jm = JobManager("mitaku_data", on_event=lambda job, kind, text: print(kind, text))
    jm.submit(["https://mitaku.net/"], first=1, last=5, chat_id=123)
    jm.pause(); jm.resume(); jm.stop()
    jm.status()
"""
import json
import queue
import threading
import time
from dataclasses import fields

from mitaku_parser import Control, Options, Scraper, summary_text

DEFAULT_SETTINGS = {
    "storage": "meta",            # meta — только метаданные, full — файлы на диск
    "zip": False, "images": True, "videos": True, "workers": 4, "delay": 1.0,
    # публикация в Telegram-канал (использует mitaku_bot.py)
    "channel": False,             # автопроверка сайтов и публикация новых постов
    "channel_interval": 30,       # минут между проверками
    "channel_media": "photos",    # photos — альбомами фото (сжатые), documents — файлами без сжатия
    "channel_zip": True,          # слать zip-архивы в комментарии
    "channel_videos": True,       # слать видео в комментарии
}
SETTING_TYPES = {"storage": str, "zip": bool, "images": bool, "videos": bool, "workers": int,
                 "delay": float, "channel": bool, "channel_interval": int, "channel_media": str,
                 "channel_zip": bool, "channel_videos": bool}
CHOICES = {"storage": ("meta", "full"), "channel_media": ("photos", "documents")}
FLAG_KEYS = ("force", "recheck", "until_known", "publish")


def _to_bool(v):
    s = str(v).strip().lower()
    if s in ("1", "on", "yes", "true", "да", "вкл"):
        return True
    if s in ("0", "off", "no", "false", "нет", "выкл"):
        return False
    raise ValueError("ожидается on/off")


class JobManager:
    def __init__(self, out_dir, on_event=None):
        """on_event(job: dict, kind: str, text: str) вызывается из рабочего потока.
        kind: started | finished | stopped | failed | post.
        post: text = "new:<slug>" / "updated:<slug>" — пост только что сохранён в библиотеку."""
        self.ctl = Control()
        self.scraper = Scraper(out_dir, Options(), self.ctl, log=self._log)
        self.lib = self.scraper.lib
        self.on_event = on_event or (lambda job, kind, text: None)
        self.scraper.on_post = self._on_post
        self.q = queue.Queue()
        self.current = None
        self._id = 0
        self._lock = threading.Lock()
        self.log_tail = []
        threading.Thread(target=self._worker, name="mitaku-worker", daemon=True).start()

    # ---- настройки (хранятся в БД, переживают перезапуск)
    def get_settings(self):
        d = dict(DEFAULT_SETTINGS)
        d.update(json.loads(self.lib.kv_get("settings") or "{}"))
        return d

    def set_setting(self, key, value):
        if key not in SETTING_TYPES:
            raise ValueError(f"неизвестная настройка: {key} (доступно: {', '.join(SETTING_TYPES)})")
        typ = SETTING_TYPES[key]
        v = _to_bool(value) if typ is bool else typ(value)
        if key in CHOICES and v not in CHOICES[key]:
            raise ValueError(f"{key}: одно из {', '.join(CHOICES[key])}")
        if key == "channel_interval" and not 5 <= v <= 1440:
            raise ValueError("channel_interval: от 5 до 1440 минут")
        if key == "workers" and not 1 <= v <= 16:
            raise ValueError("workers: от 1 до 16")
        if key == "delay" and not 0 <= v <= 60:
            raise ValueError("delay: от 0 до 60 секунд")
        cur = json.loads(self.lib.kv_get("settings") or "{}")
        cur[key] = v
        self.lib.kv_set("settings", json.dumps(cur))
        return v

    # ---- выбранные сайты (хранятся в БД)
    def get_active_sites(self):
        import mitaku_sites
        raw = self.lib.kv_get("active_sites")
        if raw:
            return [k for k in json.loads(raw) if k in mitaku_sites.SITES]
        return [k for k, s in mitaku_sites.SITES.items() if not s.experimental]

    def set_active_sites(self, keys):
        self.lib.kv_set("active_sites", json.dumps(list(keys)))

    def toggle_site(self, key):
        keys = self.get_active_sites()
        keys = [k for k in keys if k != key] if key in keys else keys + [key]
        self.set_active_sites(keys)
        return keys

    # ---- задачи
    def submit(self, urls, first=1, last=1, limit=0, chat_id=None, result_slug=None,
               status_message_id=None, back_data=None, silent=False, extra=None, **flags):
        """silent — не слать «запущена/завершена» (для фоновой проверки канала);
        extra — любые свои поля задачи (например baseline_site)."""
        with self._lock:
            self._id += 1
            job = {"id": self._id, "urls": list(urls), "first": first, "last": last,
                   "limit": limit, "chat_id": chat_id, "result_slug": result_slug,
                   "status_message_id": status_message_id, "back_data": back_data,
                   "silent": silent, **(extra or {}),
                   "flags": {k: bool(v) for k, v in flags.items() if k in FLAG_KEYS}}
        self.q.put(job)
        job["position"] = self.q.qsize() + (1 if self.current else 0)
        return job

    def pause(self):
        self.ctl.pause()

    def resume(self):
        self.ctl.resume()

    def stop(self, clear_queue=True):
        if clear_queue:
            try:
                while True:
                    self.q.get_nowait()
            except queue.Empty:
                pass
        self.ctl.stop()

    def pending_job(self):
        """Последняя задача, которая не завершилась (остановлена / упала / программу закрыли)."""
        raw = self.lib.kv_get("last_job")
        if not raw:
            return None
        job = json.loads(raw)
        return job if job.get("state") in ("running", "stopped", "failed") else None

    def resume_last(self, chat_id=None, status_message_id=None):
        job = self.pending_job()
        if not job:
            return None
        return self.submit(job["urls"], job["first"], job["last"], job["limit"],
                           chat_id or job.get("chat_id"), result_slug=job.get("result_slug"),
                           status_message_id=status_message_id, back_data=job.get("back_data"),
                           silent=job.get("silent", False), **job.get("flags", {}))

    def status(self):
        p = self.scraper.status()
        state = p.get("state", "idle")
        if self.current is None:
            state = "idle"
        elif self.ctl.paused:
            state = "paused"
        return {"state": state, "job": self.current, "queued": self.q.qsize(), "progress": p}

    # ---- служебное
    def _log(self, line):
        self.log_tail = (self.log_tail + [line])[-30:]
        print(line)

    def _on_post(self, slug, result):
        job = self.current
        if job:
            self._emit(job, "post", f"{result}:{slug}")

    def _emit(self, job, kind, text):
        try:
            self.on_event(job, kind, text)
        except Exception as e:  # noqa: BLE001
            print("on_event error:", e)

    def _worker(self):
        while True:
            job = self.q.get()
            self.ctl.reset()
            self.current = job
            opts = {**self.get_settings(), **job["flags"]}
            known = {f.name for f in fields(Options)}
            self.scraper.opt = Options(**{k: v for k, v in opts.items() if k in known})
            self.lib.kv_set("last_job", json.dumps({**job, "state": "running"}))
            self._emit(job, "started", f"▶️ Задача #{job['id']} запущена")
            try:
                res = self.scraper.sync(job["urls"], job["first"], job["last"], job["limit"])
                state = "stopped" if res.get("state") == "stopped" else "finished"
            except Exception as e:  # noqa: BLE001
                state, res = "failed", {**self.scraper.status(), "error": str(e)}
            job["result_ready"] = state == "finished"
            self.lib.kv_set("last_job", json.dumps({**job, "state": state}))
            self.current = None
            icon = {"finished": "✅", "stopped": "⏹", "failed": "❌"}[state]
            text = f"{icon} Задача #{job['id']}: {summary_text(res)}"
            if res.get("error"):
                text += f"\nОшибка: {res['error']}"
            self._emit(job, state, text)
            time.sleep(0.1)
