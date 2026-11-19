import asyncio, json, os, shutil, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE / "stubs")]   # заглушки aiogram/telethon/yt_dlp, код проекта
tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="1:x", ADMIN_IDS="1", CHANNEL_ID="@test", MITAKU_OUT=tmp + "/data", MT_API_ID="123", MT_API_HASH="abc")
os.environ.pop("DATABASE_URL", None); os.environ.pop("LOCAL_API_URL", None)
import mitaku_bot as b
upload_cb = lambda s, t: None
import mtproto_sender as mts
import telethon
from telethon.tl.types import InputPeerUser
from mitaku_jobs import JobManager
ok = lambda n: print("OK  ", n)

class Store(dict):
    def get(self, k, d=None): return dict.get(self, k, d)
    def set(self, k, v): self[k] = v

class FakeMsg:
    def __init__(s, from_bot=True, text="x"):
        s.text, s.photo, s.message_id, s.from_user = text, None, 9, SimpleNamespace(is_bot=from_bot)
        s.chat = SimpleNamespace(id=1); s.log = []; s.bot = None
    async def answer(s, text, reply_markup=None, **k): m = FakeMsg(); m.text = text; s.log.append(text); m.log = s.log; return m
    async def edit_text(s, text, **k): s.log.append(text)

async def main():
    # ---------------- MTSender
    store = Store()
    mt = mts.MTSender("123", "abc", "1:x", store)
    await mt.start()
    cl = telethon.TelegramClient.last
    assert store["mt_session"] == "SESSION-STRING" and mt.ready and len(cl.handlers) == 2
    handler = cl.handlers[0][1]
    class Ev:
        async def get_input_sender(self): return InputPeerUser(777, 99887766)
    await handler(Ev())
    assert store["mt_hash:777"] == "99887766"
    p = await mt.peer(777); assert (p.user_id, p.access_hash) == (777, 99887766)      # после «перезапуска»: кэш пуст, хеш из БД
    cl.cache[5] = "cached"; assert await mt.peer(5) == "cached"
    try: await mt.peer(404); assert False
    except mts.MTError as e: assert "отправьте боту" in str(e)
    ok("MTSender: сессия в БД, access_hash из сообщений админа, пир из кэша или из БД")
    real_sleep = asyncio.sleep
    async def fast(s): await real_sleep(0)
    mts.asyncio.sleep = fast
    cl.flood = 1; prog = []
    mid = await mt.send(777, "/tmp/x.mp4", caption="<b>cap</b>", video=True, name="x.mp4", progress=lambda a, t: prog.append((a, t)), silent=True)
    peer, path, kw = cl.sent[-1]
    assert mid == 555 and kw["supports_streaming"] and not kw["force_document"] and kw["parse_mode"] == "html" and kw["silent"] and kw["allow_cache"] is False
    assert kw["attributes"][0].file_name == "x.mp4" and prog == [(5, 10), (10, 10)]
    await mt.send(777, "/tmp/a.zip"); assert cl.sent[-1][2]["force_document"] is True
    mts.asyncio.sleep = real_sleep
    ok("MTSender.send: видео со streaming, архив документом, повтор после FloodWait, прогресс")

    # ---------------- маршрутизация send_file_any
    jm = b.jm = JobManager(tmp + "/data")
    class FakeBot:
        def __init__(s): s.calls = []
        async def send_document(s, **k): s.calls.append(("doc", k)); return SimpleNamespace(message_id=11)
        async def send_video(s, **k): s.calls.append(("video", k)); return SimpleNamespace(message_id=12)
        async def copy_message(s, **k): s.calls.append(("copy", k)); return SimpleNamespace(message_id=13)
        async def delete_message(s, chat_id, mid): s.calls.append(("delete", chat_id, mid))
        async def send_message(s, *a, **k): return SimpleNamespace(message_id=1)
    b.BOT = FakeBot()
    class FakeMT:
        def __init__(s): s.calls = []
        async def send(s, chat, path, **k):
            s.calls.append((chat, str(path), k)); 
            if k.get("progress"): k["progress"](1, 2)
            return 500
    b.MT = FakeMT(); b.BOTAPI_UPLOAD, b.MAX_UPLOAD = 1000, 100000
    small, big = Path(tmp) / "small.bin", Path(tmp) / "big.bin"
    small.write_bytes(b"a" * 500); big.write_bytes(b"b" * 5000)
    assert await b.send_file_any(1, small, caption="c") == 11 and b.BOT.calls[-1][0] == "doc" and not b.MT.calls
    assert await b.send_file_any(1, small, video=True) == 12 and b.BOT.calls[-1][0] == "video"
    ok("send_file_any: малый файл — обычный Bot API, без MTProto")
    b.BOT.calls.clear()
    assert await b.send_file_any(1, big, caption="cap", name="Big.zip") == 500             # личный чат админа: напрямую
    chat, path, kw = b.MT.calls[-1]
    assert chat == 1 and kw["name"] == "Big.zip" and kw["silent"] is False and not b.BOT.calls
    ok("send_file_any: большой файл в чат админа — напрямую через MTProto")
    rp = SimpleNamespace(message_id=42)
    got = await b.send_file_any(-1001234, big, caption="cap", rp=rp, video=True)           # группа: загрузка в личный чат + copy
    chat, path, kw = b.MT.calls[-1]
    assert chat == 1 and kw["silent"] is True and kw["video"] is True and got == 13
    kinds = [c[0] for c in b.BOT.calls]; assert kinds == ["copy", "delete"], kinds
    assert b.BOT.calls[0][1] == {"chat_id": -1001234, "from_chat_id": 1, "message_id": 500, "reply_parameters": rp}
    assert b.BOT.calls[1][1:] == (1, 500)
    ok("send_file_any: в группу — загрузка в личный чат, copyMessage с reply, удаление временного сообщения")
    b.MT = None
    try: await b.send_file_any(1, big); assert False
    except RuntimeError as e: assert "MT_API_ID" in str(e)
    b.MT = FakeMT(); b.MAX_UPLOAD = 4000
    try: await b.send_file_any(1, big); assert False
    except RuntimeError as e: assert "2 ГБ" in str(e)
    ok("send_file_any: без MTProto и сверх лимита — понятные ошибки")

    # ---------------- нарезка + MTProto
    b.MAX_UPLOAD, b.ZIP_PART_SIZE, b.BOTAPI_UPLOAD = 1000, 800, 300
    work = Path(tmp) / "bw"; work.mkdir(); src = work / "Pack.zip"; src.write_bytes(os.urandom(3000))
    b.MT = FakeMT(); progress_names = []
    status = FakeMsg()
    await b.send_big_file(status, src, "Pack.zip", "cap",
                          progress_for=lambda n: (progress_names.append(n), upload_cb)[1])
    sent_parts = [c for c in b.MT.calls]
    assert len(sent_parts) == 4 and all(Path(c[1]).suffix == ".zip" for c in sent_parts), sent_parts
    assert progress_names == [f"Pack_{i:03d}.zip" for i in range(1, 5)]
    assert not list((work / "parts").glob("*.zip")) and "copy /b" in status.log[-1]
    ok("send_big_file: файл больше лимита режется на ZIP-части, каждая уходит через MTProto, части удаляются")

    # ---------------- лимиты, диск
    b.MT = None; b.apply_limits(); assert (b.MAX_UPLOAD, b.ZIP_PART_SIZE) == (49 * b.MB, 44 * b.MB)
    b.MT = FakeMT(); b.apply_limits(); assert (b.MAX_UPLOAD, b.ZIP_PART_SIZE) == (1990 * b.MB, 1900 * b.MB)
    real_du = shutil.disk_usage
    shutil.disk_usage = lambda p: SimpleNamespace(free=1000 * b.MB)
    assert b.download_limit() == 0                                  # меньше запаса под ZIP-часть
    shutil.disk_usage = lambda p: SimpleNamespace(free=20 * 1024 * b.MB)
    assert b.download_limit() == 8 * 1024 * b.MB                    # потолок 8 ГБ
    shutil.disk_usage = lambda p: SimpleNamespace(free=4000 * b.MB)
    assert b.download_limit() == 4000 * b.MB - 2100 * b.MB
    shutil.disk_usage = real_du
    ok("apply_limits: 50 МБ без MTProto, 1990 МБ с ним; download_limit учитывает диск и запас")

    # ---------------- /mttest
    b.BOTAPI_UPLOAD, b.MAX_UPLOAD = int(0.5 * b.MB), 1990 * b.MB
    b.MT = FakeMT(); m = FakeMsg(); b.STAGING = Path(tmp) / "stg"
    await b.cmd_mttest(m, SimpleNamespace(args="2"))
    assert any("✅ 2 МБ отправлено" in t for t in m.log), m.log
    assert b.MT.calls[-1][0] == 1 and not list((b.STAGING / "mttest").glob("*"))
    m2 = FakeMsg(); chat = SimpleNamespace(id=-100, linked_chat_id=-1005)
    async def get_chat(_): return chat
    b.BOT.get_chat = get_chat
    await b.cmd_mttest(m2, SimpleNamespace(args="2 group"))
    assert any("удалено" in t for t in m2.log) and b.MT.calls[-1][0] == 1 and [c[0] for c in b.BOT.calls[-3:]] == ["copy", "delete", "delete"]
    m3 = FakeMsg(); b.MT = None; await b.cmd_mttest(m3, SimpleNamespace(args="5")); assert "не запущен" in m3.log[-1]
    ok("/mttest: тест в чат и в группу (с копированием), файл удаляется; без MTProto — подсказка")
    await mt.stop(); assert not mt.ready

asyncio.run(main())
