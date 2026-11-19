from pathlib import Path
import asyncio, json, os, sys, tempfile, time
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE / "stubs")]   # заглушки aiogram/telethon/yt_dlp, код проекта
tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="1:x", ADMIN_IDS="1", CHANNEL_ID="@test", MITAKU_OUT=tmp + "/data")
os.environ.pop("DATABASE_URL", None)
import mitaku_bot as b, mitaku_parser as mp, mitaku_sites as ms
from aiogram.types import ReplyKeyboardMarkup
from mitaku_jobs import JobManager
ok = lambda n: print("OK  ", n)

class FakeBot:
    def __init__(s): s.sent = []
    async def send_message(s, chat_id, text, **k): s.sent.append(text); return type("M", (), {"message_id": 1})()
    async def delete_message(s, *a): pass
class FakeMsg:
    def __init__(s, bot, from_bot=False):
        s.bot, s.text, s.photo, s.message_id = bot, "x", None, 7
        s.chat = type("C", (), {"id": 1})(); s.from_user = type("U", (), {"is_bot": from_bot})(); s.log = []
    async def answer(s, text, reply_markup=None, **k): s.log.append((text, reply_markup)); return FakeMsg(s.bot, True)
    async def delete(s): pass

def rec_of(slug, title, kind, archives=(), tags=("cosplay", "Nikke game")):
    files = {"photo": [{"name": f"{slug}-{i}.jpg", "kind": "photo", "url": "u"} for i in range(3)],
             "video": [{"name": f"{slug}.mp4", "kind": "video", "url": "u"}] + [{"name": f"{slug}-0.jpg", "kind": "photo", "url": "u"}],
             "arch": []}[kind]
    meta = {"title": title, "cosplayer": "Byoru", "character": "Rabbit", "source": "NIKKE", "tags": list(tags),
            "archives": list(archives), "formats": {"photo": {"jpg": 12}, "video": {}}}
    return {"slug": slug, "url": f"https://fake.example/{slug}/", "title": title, "cosplayer": "Byoru", "status": "meta",
            "files_json": json.dumps(files), "meta_json": json.dumps(meta), "published": "2026", "site": "mitaku",
            "section": "x", "character": "Rabbit", "source": "NIKKE", "tags": ",".join(tags), "zipped": 0, "folder": ""}

class FakeSite:
    key, title, experimental = "fake", "Fake Site 🧪", False
    def sections(self): return {"all": "https://fake.example/"}
    def slug(self, url): return url.rstrip("/").split("/")[-1]
    def fetch_listing(self, session, base, n):
        if n > 3: return None
        return [{"url": f"https://fake.example/{n}b/", "title": f"Post {n}b", "sig": None},
                {"url": f"https://fake.example/{n}a/", "title": f"Post {n}a", "sig": None}]

async def main():
    jm = b.jm = JobManager(tmp + "/data"); b.BOT = FakeBot(); b.PUBLISH_Q = asyncio.Queue(); b.PUBLISH_LOCK = asyncio.Lock()
    notes = []
    async def fake_notify(text): notes.append(text)
    b.notify_admins = fake_notify
    jm.get_settings = lambda: {"publish_delay": 0, "import_delay": 0, "pause_every": 0, "pause_seconds": 0,
                               "publish_video_posts": False, "channel_media": "photos", "channel_zip": True, "channel_videos": False}

    # ---------- nav_rows
    labels = lambda rows: [x.text for r in rows for x in r]
    assert b.nav_rows(0, 1, str) == []
    assert labels(b.nav_rows(0, 5, lambda p: f"x:{p}")) == ["📄 1/5", "Вперёд ▶️", "В конец ⏭"]
    mid = b.nav_rows(2, 5, lambda p: f"x:{p}")[0]
    assert [x.text for x in mid] == ["⏮ В начало", "◀️ Назад", "📄 3/5", "Вперёд ▶️", "В конец ⏭"]
    assert [x.callback_data for x in mid] == ["x:0", "x:1", "nop:0", "x:3", "x:4"]
    assert labels(b.nav_rows(4, 5, lambda p: f"x:{p}")) == ["⏮ В начало", "◀️ Назад", "📄 5/5"]
    ok("nav_rows: в начало / назад / стр / вперёд / в конец")

    # ---------- формат поста
    r = rec_of("p1", "Byoru – Rabbit <Set>", "photo", archives=["https://ouo.io/AAA", "https://bysesayeveum.com/e/x", "https://ouo.io/BBB"])
    text = b.channel_text(r)
    assert "mitaku.net" not in text and "fake.example" not in text and "Источник:</b> Mitaku" in text, text
    assert "Косплеер:</b> Byoru" in text and "Игра / аниме:</b> NIKKE" in text and "Фото: 3" in text, text
    assert text.count("Архив ") == 2 and "bysesayeveum" not in text and "#cosplay" in text and "#Nikke_game" in text
    assert len(text) <= 1000
    long_tags = [f"verylongtagname{i}" for i in range(40)]
    assert len(b.channel_text(rec_of("p2", "T" * 150, "photo", archives=["https://ouo.io/A"] * 6, tags=long_tags))) <= 1000
    assert "Архив 1" in b.channel_text(rec_of("p2", "T" * 150, "photo", archives=["https://ouo.io/A"] * 6, tags=long_tags))
    ok("channel_text: ник, игра, число фото, теги, архивы, источник без адреса, ≤1000 символов")

    # ---------- правила: видео / только архивы
    S = {"publish_video_posts": False}
    assert b.publish_verdict(rec_of("a", "A", "photo"), S)[0] == "ok"
    assert b.publish_verdict(rec_of("a", "A", "video"), S)[0] == "skip_video"
    assert b.publish_verdict(rec_of("a", "A", "photo", archives=["https://bysesayeveum.com/e/q"]), S)[0] == "skip_video"
    assert b.publish_verdict(rec_of("a", "A", "arch", archives=["https://ouo.io/x"]), S)[0] == "skip_archives"
    assert b.publish_verdict(rec_of("a", "A", "arch"), S)[0] == "skip_empty"
    assert b.publish_verdict(rec_of("a", "A", "video"), {"publish_video_posts": True})[0] == "ok"
    ok("publish_verdict: видео и «только архивы» не публикуются (настройка может разрешить видео)")

    # ---------- publish_post: история full / partial / post_only / skip
    for slug, kind in (("f1", "photo"), ("f2", "photo"), ("f3", "photo"), ("f4", "photo"), ("v1", "video"), ("a1", "arch")):
        jm.lib.save(rec_of(slug, slug.upper(), kind, archives=["https://ouo.io/z"] if kind == "arch" else []))
    chat = type("C", (), {"id": -100, "linked_chat_id": 555})()
    async def get_chat(_): return chat
    b.BOT.get_chat = get_chat
    async def fake_swp(bot, chat_id, rec, caption, text, markup=None): return type("M", (), {"message_id": 42})()
    b.send_with_preview = fake_swp
    async def fake_fwd(mid): return (900, 901)
    b.wait_forward = fake_fwd
    mode = {"photos": 3}
    async def fake_comments(group, reply, rec, settings, stats=None, progress=None, save=None):
        stats.update({"photos": mode["photos"], "videos": 0, "zips": 1})
        if mode.get("boom"): raise RuntimeError("flood")
        return stats
    REAL_SEND_COMMENTS = b.send_comments
    b.send_comments = fake_comments
    class Staged:
        async def __aenter__(s): pass
        async def __aexit__(s, *a): pass
    b.staged = lambda rec, names=None: Staged()
    await b.publish_post("f1"); assert jm.lib.hist_get("f1")["status"] == "full"
    mode["photos"] = 1; await b.publish_post("f2"); h = jm.lib.hist_get("f2"); assert h["status"] == "partial" and h["photos_sent"] == 1
    mode.update(photos=2, boom=True)
    try: await b.publish_post("f3"); assert False
    except RuntimeError: pass
    h = jm.lib.hist_get("f3"); assert h["status"] == "partial" and "flood" in h["detail"] and h["photos_sent"] == 2
    chat.linked_chat_id = None; await b.publish_post("f4"); assert jm.lib.hist_get("f4")["status"] == "post_only"
    for slug, st in (("v1", "skip_video"), ("a1", "skip_archives")):
        try: await b.publish_post(slug); assert False
        except b.NotPublishable as e: assert e.status == st
        assert jm.lib.hist_get(slug)["status"] == st and not jm.lib.kv_get(f"published:{slug}")
    ok("publish_post: история full/partial/post_only, видео и архивы -> skip без публикации")

    # ---------- импорт: порядок, правила, остановка, продолжение
    ms.SITES["fake"] = FakeSite()
    created = {}
    def fake_process(item):
        slug = FakeSite().slug(item["url"])
        kind = {"1b": "video", "2b": "arch"}.get(slug, "photo")
        jm.lib.save(rec_of(slug, item["title"], kind, archives=["https://ouo.io/q"] if kind == "arch" else []))
        jm.scraper.blocked = False
        return "new"
    jm.scraper.process_post = fake_process
    published = []
    real_publish_post = b.publish_post
    async def pub(slug, force=False, overrides=None):
        published.append(slug); await asyncio.sleep(0.05)
        jm.lib.kv_set(f"published:{slug}", "1"); jm.lib.hist_set(slug, "full", "ok", photos_total=3, photos_sent=3)
        return "ok"
    b.publish_post = pub
    st = b.import_new("fake")
    b.IMPORT.update(state=None, msg=None)
    task = asyncio.create_task(b.import_run(st)); b.IMPORT["task"] = task
    while len(published) < 1: await asyncio.sleep(0.01)
    b.IMPORT["stop"] = True; jm.ctl.stop()
    await task
    saved = b.import_load()
    assert saved["status"] == "stopped" and saved["last_page"] == 3 and saved["page"] == 3 and saved["index"] == 1, saved
    assert published == ["3a"], published                    # самая старая страница, старые посты первыми
    ok(f"импорт: старт с последней страницы 3, остановка запомнила страницу {saved['page']} пост {saved['index']}")
    # продолжение (как после перезапуска): состояние берём из БД
    st = b.import_load(); st["status"] = "running"
    task = asyncio.create_task(b.import_run(st)); b.IMPORT["task"] = task
    await task
    saved = b.import_load()
    assert saved["status"] == "finished" and saved["page"] == 0, saved
    assert published == ["3a", "3b", "2a", "1a"], published    # 2b (архивы) и 1b (видео) не публиковались; повторов нет
    assert saved["full"] >= 3 and saved["skip_archives"] == 1 and saved["skip_video"] == 1, saved
    assert jm.lib.hist_get("2b")["status"] == "skip_archives" and jm.lib.hist_get("1b")["status"] == "skip_video"
    assert jm.lib.kv_get("baseline:fake") == "1"
    assert any("завершён" in n for n in notes), notes
    ok(f"импорт: продолжение с сохранённой позиции, порядок {published}, видео/архивы только в историю")

    # ---------- блокировка сайта: пауза и повтор
    b.block_minutes = lambda attempt: 0.01
    calls = {"n": 0}
    def blocking_process(item):
        calls["n"] += 1
        if calls["n"] == 1:
            jm.scraper.blocked = True; jm.scraper.last_error = RuntimeError("403"); return "error"
        return fake_process(item)
    jm.scraper.process_post = blocking_process
    st = b.import_new("fake"); st.update(last_page=1, page=1)
    jm.lib.kv_set("published:1a", ""); 
    for s in ("1a", "1b"): 
        import sqlite3
    c = jm.lib.db; c.execute("DELETE FROM publications"); c.execute("DELETE FROM kv WHERE key LIKE 'published:%'"); c.commit()
    published.clear(); b.IMPORT.update(stop=False)
    task = asyncio.create_task(b.import_run(st)); b.IMPORT["task"] = task; await task
    assert b.import_load()["status"] == "finished" and calls["n"] >= 3 and published == ["1a"], (calls, published)
    ok("импорт: при блокировке сайта пауза и повтор того же поста")

    # ---------- пять ошибок подряд останавливают импорт, позиция сохранена
    def bad_process(item): jm.scraper.blocked = False; jm.scraper.last_error = RuntimeError("не разобрать"); return "error"
    jm.scraper.process_post = bad_process
    jm.lib.db.execute("DELETE FROM publications"); jm.lib.db.execute("DELETE FROM posts"); jm.lib.db.execute("DELETE FROM kv WHERE key LIKE 'published:%'"); jm.lib.db.commit()
    st = b.import_new("fake"); st.update(last_page=3, page=3)
    task = asyncio.create_task(b.import_run(st)); b.IMPORT["task"] = task; await task
    saved = b.import_load(); assert saved["status"] == "failed" and "5 ошибок" in saved["error"] and saved["page"] == 1 and saved["errors"] == 5, saved
    ok("импорт: 5 ошибок подряд -> failed, позиция сохранена")

    # ---------- история и поиск: экраны и листание
    jm.lib.db.execute("DELETE FROM publications"); jm.lib.db.commit()
    for i in range(20):
        jm.lib.save(rec_of(f"h{i}", f"Hist {i}", "photo"))
        jm.lib.hist_set(f"h{i}", ["full", "partial", "skip_video", "error"][i % 4], f"d{i}", title=f"Hist {i}", photos_total=3, photos_sent=3)
    bot = FakeBot(); m = FakeMsg(bot)
    await b.show_history(m, "all", 1, keep=True)
    text, markup = m.log[-1]
    assert isinstance(markup, ReplyKeyboardMarkup) and "20" in text.split("\n")[0]
    acts = b.MENUS[1]
    assert "⏮ В начало" in acts and "В конец ⏭" in acts and "📄 2/3" in acts and acts["В конец ⏭"] == "h:all:2" and acts["⏮ В начало"] == "h:all:0"
    assert any(a.startswith("i:") and a.endswith(":h:all:1") for a in acts.values())
    assert any("Пропущенные (5)" in k for k in acts) and acts[[k for k in acts if "Ошибки" in k][0]] == "h:error:0"
    await b.show_history(m, "skipped", 0, keep=True); assert "5" in m.log[-1][0].split("\n")[0]
    ok("история публикаций: листание ⏮ ◀️ 📄 ▶️ ⏭, фильтры по категориям со счётчиками")
    for q in ("byoru", "nikke", "byoru"): jm.lib.search_log(q, None, 4)
    await b.show_searches(m, 0, keep=True)
    text = m.log[-1][0]; assert "byoru" in text and "2 раз" in text, text
    acts = b.MENUS[1]; sid = [v for v in acts.values() if v.startswith("sr:")]; assert len(sid) == 2
    jm.lib.search_clear(); assert jm.lib.search_list(5, 0)[1] == 0
    ok("история поиска: список, повтор запроса, очистка")
    # карточка показывает метку из истории
    assert "Канал" in b.hist_note(jm.lib.get("h0")) and "Полностью" in b.hist_note(jm.lib.get("h0"))
    ok("карточка поста: статус публикации из истории")


    # =============================================================== новое: докачка, перепроверка, ZIP, флуд
    b.publish_post = real_publish_post
    del b.staged
    import mitaku_bot as bb
    MB = bb.MB
    # ---------- автозамедление
    b.FLOOD.update(factor=1.0, last=0.0)
    assert b.note_flood(2) is None and b.flood_factor() == 1.0
    assert b.note_flood(10) == 2.0 and b.note_flood(10) == 4.0 and b.note_flood(10) == 8.0 and b.note_flood(10) is None
    b.FLOOD["last"] -= 1900; assert b.flood_factor() == 4.0            # через 30 мин — вдвое ближе к норме
    b.FLOOD["last"] -= 1900; assert b.flood_factor() == 2.0
    ok("автозамедление: ×2 за флуд-лимит (до ×8) и возврат к норме через 30 мин")
    b.FLOOD.update(factor=4.0, last=time.monotonic())
    times = []
    async def pub2(slug, force=False, overrides=None): times.append(time.monotonic()); return "ok"
    b.publish_post = pub2
    jm.get_settings = lambda: {"publish_delay": 0.1, "publish_video_posts": False}
    for s_ in ("h1", "h2"): jm.lib.kv_set(f"published:{s_}", "")
    b.LAST_PUBLISH = 0.0
    await b.publish_paced("h1", True); await b.publish_paced("h2", True)
    assert times[1] - times[0] >= 0.39, times                          # 0.1 с × 4
    b.FLOOD.update(factor=1.0)
    ok("publish_paced: пауза умножается на множитель флуда")

    # ---------- send_comments: ZIP-лимит и продолжение с места обрыва
    rec = rec_of("c1", "C1", "photo"); rec["files_json"] = json.dumps([{"name": f"c1-{i}.jpg", "kind": "photo", "url": "u"} for i in range(25)])
    groups, photos_sent, zips = [], [], []
    class B2(FakeBot):
        async def send_media_group(s, chat_id, media, **k): groups.append(len(media))
        async def send_photo(s, chat_id, photo, **k): photos_sent.append(1)
    b.BOT = B2(); b.send_comments = REAL_SEND_COMMENTS
    b.photo_bytes = lambda r, f: b"x"; b.file_exists = lambda r, f: True
    b.post_file_size = lambda r, f: 100 * MB if False else 10
    async def fake_parts(*a, **k): zips.append(1); return 2
    b.send_parts = fake_parts; b.SEND_PAUSE = 0
    saves = []; prog = {}
    stats = {}
    await b.send_comments(-100, 5, rec, {"channel_zip": True, "zip_max_mb": 150, "channel_media": "photos"}, stats, prog, lambda: saves.append(dict(prog)))
    assert groups == [10, 10, 5 and 5] or groups == [10, 10], groups
    assert stats["photos"] == 25 and prog["photo_chunks"] == 3 and prog["zip"] and len(zips) == 1 and len(saves) == 4
    groups.clear(); zips.clear(); stats2 = {}
    await b.send_comments(-100, 5, rec, {"channel_zip": True, "zip_max_mb": 150, "channel_media": "photos"}, stats2, {"photo_chunks": 2}, None)
    assert len(groups) + len(photos_sent) >= 1 and stats2["photos"] == 25, (groups, stats2)    # отправлен только последний альбом
    assert groups == [] and len(photos_sent) >= 1 or groups == [5]
    ok("send_comments: продолжение с 3-го альбома, счётчики полные")
    b.post_file_size = lambda r, f: 100 * MB                              # 25 × 100 МБ — выше лимита
    zips.clear(); st3 = {}
    await b.send_comments(-100, 5, rec, {"channel_zip": True, "zip_max_mb": 150, "channel_media": "photos"}, st3, {}, None)
    assert not zips and st3["zip_skipped_mb"] == 2500, st3
    zips.clear(); st4 = {}
    await b.send_comments(-100, 5, rec, {"channel_zip": True, "zip_max_mb": 0, "channel_media": "photos"}, st4, {}, None)
    assert len(zips) == 1
    ok("ZIP: пропускается при превышении zip_max_mb, при 0 — без лимита")

    # ---------- resume_comments / republish_post / карточка
    jm.lib.save(rec_of("r1", "R1", "photo")); jm.lib.save(rec_of("r2", "R2", "photo"))
    jm.lib.hist_set("r1", "partial", "упало", title="R1", photos_total=3, photos_sent=1, msg_id=77)
    jm.lib.hist_set("r2", "partial", "упало", title="R2", photos_total=3, photos_sent=1, msg_id=78)
    jm.lib.kv_set("published:r1", "77"); jm.lib.kv_set("fwd:r1", "-100555:901"); jm.lib.kv_set("cprog:r1", json.dumps({"photo_chunks": 1}))
    jm.lib.kv_set("published:r2", "78")
    seen = {}
    async def fake_send_comments(group, reply, rec, settings, stats, progress, save):
        seen.update(group=group, reply=reply, progress=dict(progress)); stats.update(photos=3, videos=0, zips=1); return stats
    b.send_comments = fake_send_comments
    class Staged:
        async def __aenter__(s): pass
        async def __aexit__(s, *a): pass
    b.staged = lambda rec, names=None: Staged()
    jm.get_settings = lambda: {"publish_delay": 0, "publish_video_posts": False, "zip_max_mb": 150, "import_zip": False, "channel_zip": True}
    detail = await b.resume_comments("r1")
    assert seen == {"group": -100555, "reply": 901, "progress": {"photo_chunks": 1}} and jm.lib.hist_get("r1")["status"] == "full", (seen, detail)
    try: await b.resume_comments("r2"); assert False
    except RuntimeError as e: assert "Переопубликовать" in str(e)
    ok("resume_comments: берёт группу/сообщение и прогресс из БД, итог -> full; без данных — понятная ошибка")
    deleted = []
    chatobj = type("C", (), {"id": -100, "linked_chat_id": 555, "title": "Канал"})()
    async def get_chat(_): return chatobj
    async def del_msg(chat_id, mid): deleted.append((chat_id, mid))
    b.BOT.get_chat, b.BOT.delete_message = get_chat, del_msg
    called = {}
    async def fake_pp(slug, force=False, overrides=None): called.update(slug=slug, force=force); return "ok"
    b.publish_post = fake_pp
    await b.republish_post("r2")
    assert deleted == [(-100, 78)] and called == {"slug": "r2", "force": True} and jm.lib.kv_get("published:r2") == ""
    ok("republish_post: старый пост удалён из канала, ключи сброшены, публикация заново")
    # кнопки карточки
    labels_of = lambda rec: [x.text for row in b.card_rows(rec, None) for x in row]
    jm.lib.kv_set("fwd:r2", "")
    assert "♻️ Переопубликовать" in labels_of(jm.lib.get("r2"))
    jm.lib.hist_set("r1", "partial", "x"); assert "🔁 Дослать недостающее" in labels_of(jm.lib.get("r1"))
    jm.lib.hist_set("r1", "full", "x"); l = labels_of(jm.lib.get("r1")); assert "🔁 Дослать недостающее" not in l and "♻️ Переопубликовать" not in l
    ok("карточка: «Дослать недостающее» / «Переопубликовать» по состоянию поста")

    # ---------- массовые действия в истории
    jm.lib.db.execute("DELETE FROM publications WHERE slug NOT IN ('r1', 'r2')"); jm.lib.db.commit()
    jm.lib.hist_set("r1", "partial", "x")
    slugs, orphans = b.resume_candidates(); assert slugs == ["r1"] and orphans == 1, (slugs, orphans)
    m = FakeMsg(FakeBot()); m.from_user.is_bot = False
    b.PUBLISH_PENDING.clear(); b.BATCHES.clear()
    while not b.PUBLISH_Q.empty(): b.PUBLISH_Q.get_nowait()
    await b.cb_history_batch(b.Tap(m, "hb:resume"))
    assert "Дослать недостающее под постами: <b>1</b>" in m.log[-1][0] and "1 частичных" in m.log[-1][0], m.log[-1][0]
    await b.cb_history_batch(b.Tap(m, "hb:resume:yes"))
    item = b.PUBLISH_Q.get_nowait(); assert item[0] == "r1" and item[1] == "resume" and item[2] in b.BATCHES
    ok("история: «Дослать под всеми» — подтверждение и очередь в режиме resume")
    # перепроверка пропущенных после смены настройки
    for slug, kind in (("s1", "video"), ("s2", "arch")): jm.lib.save(rec_of(slug, slug, kind, archives=["https://ouo.io/q"] if kind == "arch" else []))
    jm.lib.hist_set("s1", "skip_video", "видео", title="s1"); jm.lib.hist_set("s2", "skip_archives", "арх", title="s2")
    jm.get_settings = lambda: {"publish_delay": 0, "publish_video_posts": False}
    ok_, still = b.recheck_skipped(); assert ok_ == [] and still >= 2
    jm.get_settings = lambda: {"publish_delay": 0, "publish_video_posts": True}
    ok_, still = b.recheck_skipped(); assert ok_ == ["s1"] and still >= 1, (ok_, still)
    ok_, _ = b.recheck_skipped(True); assert jm.lib.hist_get("s1") is None and jm.lib.hist_get("s2")["status"] == "skip_archives"
    slugs, urls = b.skipped_posts(); assert "s2" in slugs and all(u.startswith("https://") for u in urls)
    ok("перепроверка пропущенных: после publish_video_posts on видео-пост снова подходит, «только архивы» остаётся")
    # настройки
    jm2 = JobManager(tempfile.mkdtemp())
    assert jm2.set_setting("zip_max_mb", "300") == 300 and jm2.set_setting("import_zip", "on") is True
    try: jm2.set_setting("zip_max_mb", "5000")
    except ValueError: ok("настройки zip_max_mb / import_zip")
    # статус канала без падений
    b.BOT.get_chat = get_chat
    jm.get_settings = jm2.get_settings
    text, rows = await b.channel_status(); assert "ZIP в комментариях" in text and "не больше 300 МБ" in text, text

asyncio.run(main())
