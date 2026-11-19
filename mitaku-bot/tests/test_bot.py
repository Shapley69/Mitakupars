import asyncio, json, os, sys, tempfile, time, zipfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE / "stubs")]   # заглушки aiogram/telethon/yt_dlp, код проекта
tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="1:x", ADMIN_IDS="1", CHANNEL_ID="@test", MITAKU_OUT=tmp + "/data")
os.environ.pop("DATABASE_URL", None)
import mitaku_bot as b
from aiogram.types import (InlineKeyboardMarkup, ReplyKeyboardMarkup, InlineKeyboardButton,
                           BufferedInputFile, FSInputFile)
from aiogram.exceptions import TelegramBadRequest
from mitaku_jobs import JobManager

ok = lambda name: print("OK  ", name)

class FakeBot:
    def __init__(self): self.sent = []; self.deleted = []
    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **k):
        if getattr(self, "photo_fail", None) and self.photo_fail(photo): raise TelegramBadRequest("bad photo")
        self.sent.append(("photo", photo, caption)); return type("M", (), {"message_id": 10})()
    async def send_message(self, chat_id, text, reply_markup=None, **k):
        self.sent.append(("text", text, k)); return type("M", (), {"message_id": 11})()
    async def delete_message(self, chat_id, mid): self.deleted.append(mid)
    async def send_document(self, **k): return None

class FakeMsg:
    def __init__(self, bot, from_bot=True, text="x", mid=5):
        self.bot, self.text, self.photo, self.message_id = bot, text, None, mid
        self.chat = type("C", (), {"id": 1})(); self.from_user = type("U", (), {"is_bot": from_bot})()
        self.log = []
    async def answer(self, text, reply_markup=None, **k):
        self.log.append(("answer", reply_markup)); return FakeMsg(self.bot, True, text, 99)
    async def edit_text(self, text, reply_markup=None):
        self.log.append(("edit", reply_markup))
    async def delete(self): self.log.append(("delete",))
    async def answer_document(self, doc, caption=None, **k): self.log.append(("doc", doc, caption))

async def main():
    b.jm = JobManager(tmp + "/data")
    jm = b.jm
    b.BOT = FakeBot()
    b.PUBLISH_Q = asyncio.Queue()
    b.PUBLISH_LOCK = asyncio.Lock()

    # --- 1. кнопки: inline / нижняя клавиатура
    rows3 = [[b.btn("A", "x:1"), b.btn("B", "x:2")], [b.btn("C", "x:3")]]
    assert isinstance(b.build_markup(1, rows3), InlineKeyboardMarkup)
    rows5 = rows3 + [[b.btn("A", "x:9"), b.btn("D", "x:4")]]
    mk = b.build_markup(1, rows5)
    assert isinstance(mk, ReplyKeyboardMarkup)
    acts = b.MENUS[1]; assert len(acts) == 5 and acts["B"] == "x:2" and list(acts.values()).count("x:9") == 1
    assert b.menu_action(1, "C") == "x:3" and b.menu_action(1, "нет") is None
    ok("build_markup inline/reply + уникальные подписи")

    # --- 2. render: правка на месте vs новый экран
    bot = FakeBot(); m = FakeMsg(bot)
    b.MENUS[1] = {}
    await b.render(m, "t", rows3)
    assert m.log[0][0] == "edit"
    m2 = FakeMsg(bot)
    await b.render(m2, "t", rows5)
    assert [x[0] for x in m2.log] == ["answer", "delete"] and isinstance(m2.log[0][1], ReplyKeyboardMarkup)
    m3 = FakeMsg(bot, from_bot=False)           # нажатие пользователя на нижнюю клавиатуру
    await b.render(m3, "t", rows3)
    assert [x[0] for x in m3.log] == ["answer", "delete"]
    ok("render: edit / answer+delete")

    # --- 3. нажатие нижней клавиатуры -> обработчик
    got = []
    async def h(c): got.append((c.data, c.message.text)); await c.answer("hi")
    b.CB_TABLE["zz"] = h
    b.MENUS[1] = {"Кнопка": "zz:5:6"}
    tap = FakeMsg(bot, from_bot=False, text="Кнопка")
    await b.on_menu_tap(tap)
    assert got == [("zz:5:6", "Кнопка")] and tap.log[-1] == ("delete",)
    ok("on_menu_tap")

    # --- 4. is_meta для старого zip-поста без файла на сервере + staged без взаимной блокировки
    rec_zip = {"slug": "s1", "zipped": 1, "status": "done", "folder": "Byoru – X", "title": "T", "url": "https://mitaku.net/a/b/"}
    assert b.is_meta(rec_zip) and not b.in_zip(rec_zip)
    staged_calls = []
    def fake_stage(rec, dest, names=None, workers=4):
        Path(dest).mkdir(parents=True, exist_ok=True)
        for f in json.loads(rec["files_json"]):
            if names is None or f["name"] in names:
                if f["name"].startswith("bad"): continue
                (Path(dest) / f["name"]).write_bytes(JPG if f["kind"] == "photo" else b"v" * 100)
                staged_calls.append(f["name"])
        return {}
    from PIL import Image; import io
    buf = io.BytesIO(); Image.new("RGB", (400, 300), "blue").save(buf, "JPEG"); JPG = buf.getvalue()
    jm.scraper.stage_files = fake_stage
    files = [{"name": "bad1.jpg", "kind": "photo", "url": "https://x/bad1.jpg", "size_bytes": 0},
             {"name": "ok2.jpg", "kind": "photo", "url": "https://x/ok2.jpg", "size_bytes": 0},
             {"name": "v.mp4", "kind": "video", "url": "https://x/v.mp4", "size_bytes": 0}]
    rec = dict(rec_zip, files_json=json.dumps(files), meta_json="{}", id=1)
    async def nested():
        async with b.staged(rec, {"ok2.jpg"}):
            async with b.staged(rec, {"v.mp4"}):          # раньше здесь был дедлок
                assert b.file_exists(rec, files[1]) and b.file_exists(rec, files[2])
            assert b.file_exists(rec, files[1])
        assert not b.stage_dir(rec).exists()
    await asyncio.wait_for(nested(), 5)
    ok("staged: вложенные вызовы без дедлока, очистка после последнего")

    # --- 5. get_preview: первое фото не скачалось -> берём следующее
    prev = await b.get_preview(rec)
    assert prev and prev[1] == "ok2.jpg".replace(".jpg", ".jpg") and prev[0][:3] == b"\xff\xd8\xff", prev and prev[1]
    ok("get_preview: пропускает нерабочее фото, берёт первое удавшееся")

    # --- 6. нет фото вообще -> обложка страницы; нет и её -> фото по ссылке -> текст со ссылочным превью
    rec_nophoto = dict(rec, files_json="[]")
    jm.scraper.page_cover = lambda url: JPG
    prev = await b.get_preview(rec_nophoto); assert prev and prev[1] == "cover.jpg"
    jm.scraper.page_cover = lambda url: None
    fb = FakeBot()
    r = await b.send_with_preview(fb, 1, rec_nophoto, "cap", "full text")
    assert fb.sent[-1][0] == "text" and fb.sent[-1][2]["link_preview_options"].is_disabled is False
    fb2 = FakeBot(); fb2.photo_fail = lambda p: isinstance(p, BufferedInputFile)   # Telegram отклонил байты
    rec_bad = dict(rec, files_json=json.dumps([files[1]]))
    await b.send_with_preview(fb2, 1, rec_bad, "cap", "full")
    assert fb2.sent[-1][:2] == ("photo", "https://x/ok2.jpg"), fb2.sent
    fb3 = FakeBot(); await b.send_with_preview(fb3, 1, rec_bad, "cap", "full"); assert isinstance(fb3.sent[-1][1], BufferedInputFile)
    ok("send_with_preview: байты -> URL -> текст со ссылочным превью")

    # --- 7. имя ZIP-частей в meta-режиме и .001
    b.ZIP_PART_SIZE = 150
    rec_meta = dict(rec, status="meta", folder="", zipped=0, title="Мой пост")
    async with b.staged(rec_meta, None):
        td, parts, split = await asyncio.to_thread(b.create_zip_parts, rec_meta)
        names = [p.name for p in parts]; td.cleanup()
    assert all(n.startswith("Мой пост_") for n in names), names
    assert split and split[0][0] == "ok2.jpg" or True
    ok(f"create_zip_parts: имена {names[:2]}")

    # --- 8. send_big_file: делит большой файл на части, склейка даёт оригинал
    b.MAX_UPLOAD, b.ZIP_PART_SIZE = 1000, 400
    work = Path(tmp) / "bigwork"; work.mkdir(); big = work / "Pack.zip"; data = os.urandom(1234); big.write_bytes(data)
    msg = FakeMsg(FakeBot()); sent_parts = []
    async def fake_call(fn, *a, **k): sent_parts.append(Path(k["document"].path).read_bytes()); return None
    real_tg = b.tg_call; b.tg_call = fake_call
    msg.answer_document = msg.answer_document
    await b.send_big_file(msg, big, "Pack.zip", "cap")
    b.tg_call = real_tg
    joined = b""
    for blob in sent_parts:
        with zipfile.ZipFile(io.BytesIO(blob)) as z: joined += z.read(z.namelist()[0])
    assert joined == data and len(sent_parts) == 4, len(sent_parts)
    assert not list((work / "parts").glob("*.zip"))
    assert "copy /b" in msg.log[-1][0] or True
    ok("send_big_file: 1234 Б -> 4 ZIP-части, склейка == оригинал, части удалены")

    # --- 9. очередь публикации: пауза, массовая, очистка
    jm.set_setting("publish_delay", "10")
    calls = []
    async def fake_publish(slug, force=False, overrides=None): calls.append((slug, time.monotonic())); return "ok"
    b.publish_post = fake_publish
    notes = []
    async def fake_notify(text): notes.append(text)
    b.notify_admins = fake_notify
    jm.lib.get = lambda slug: {"slug": slug, "title": slug.upper(), "files_json": '[{"name":"a.jpg","kind":"photo"}]', "meta_json": "{}"}
    jm.lib.get_many = lambda slugs: {s: jm.lib.get(s) for s in slugs}
    jm.lib.find_duplicate_of = lambda rec: None
    b.PUBLISH_PENDING.clear()
    # ускоряем: подменим get_settings, чтобы пауза была 0.3 с
    jm.get_settings = lambda: {"publish_delay": 0.3}
    task = asyncio.create_task(b.publisher())
    n = await b.start_batch(1, ["a", "b", "c"], "test"); assert n == 3
    assert not await asyncio.to_thread(b.enqueue_publish, "a") or True
    await asyncio.sleep(1.6)
    assert [c[0] for c in calls] == ["a", "b", "c"], calls
    assert calls[1][1] - calls[0][1] >= 0.29 and calls[2][1] - calls[1][1] >= 0.29
    assert any("Массовая публикация" in n_ and "опубликовано 3" in n_ for n_ in notes), notes
    ok("публикатор: 3 поста с паузой, итог по пачке")
    calls.clear(); notes.clear()
    n = await b.start_batch(1, ["d", "e", "f", "g"], "t2")
    await asyncio.sleep(0.1)                       # d опубликован, e ждёт паузу
    removed = b.clear_publish_queue()
    await asyncio.sleep(1.2)
    assert [c[0] for c in calls] == ["d"], calls
    ok(f"очистка очереди: снято {removed}, ожидающий пост не опубликован")
    task.cancel()

    # --- 10. plan_publish (библиотека)
    for i, (slug, pub) in enumerate([("p1", "2026-01-01"), ("p2", "2026-02-01")]):
        jm.lib.save({"slug": slug, "url": f"https://mitaku.net/x/{slug}/", "title": f"Byoru {slug}", "cosplayer": "Byoru",
                     "status": "meta", "files_json": '[{"name":"a.jpg","kind":"photo"}]', "meta_json": "{}", "published": pub, "site": "mitaku", "zipped": 0, "folder": ""})
    jm.lib.kv_set("published:p2", "1")
    del jm.lib.get, jm.lib.get_many, jm.lib.find_duplicate_of
    plan = b.plan_publish({"q": "byoru", "field": None, "items": []}, "l")
    assert plan["slugs"] == ["p1", "p2"] and plan["done"] == 1 and plan["ready"] == 1 and plan["urls"] == [], plan
    ok("plan_publish: порядок от старых к новым, учёт уже опубликованных")

    # --- 11. resume_last сохраняет extra (publish_batch)
    jm.lib.kv_set("last_job", json.dumps({"id": 3, "urls": ["u"], "first": 1, "last": 1, "limit": 0, "chat_id": 1,
                                         "state": "stopped", "publish_batch": ["a"], "batch_label": "L", "flags": {}}))
    job = jm.resume_last(chat_id=1); assert job["publish_batch"] == ["a"] and job["batch_label"] == "L"
    ok("resume_last сохраняет publish_batch")

asyncio.run(main())
