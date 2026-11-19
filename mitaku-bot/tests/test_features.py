import asyncio, json, os, sys, tempfile, time, csv, io
from pathlib import Path
from types import SimpleNamespace
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE / "stubs")]   # заглушки aiogram/telethon/yt_dlp, код проекта
tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="1:x", ADMIN_IDS="1", CHANNEL_ID="@test", MITAKU_OUT=tmp + "/data")
os.environ.pop("DATABASE_URL", None); os.environ.pop("MT_API_ID", None); os.environ.pop("MT_API_HASH", None)
import mitaku_bot as b, mitaku_parser as mp, exports, subs
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup
from mitaku_jobs import JobManager
ok = lambda n: print("OK  ", n)

def rec_of(slug, title, cos="Byoru", site="mitaku", photos=52, source="NIKKE", tags="cosplay, nikke", character="", published="2026-01-01"):
    files = [{"name": f"{slug}-{i}.jpg", "kind": "photo", "url": "u"} for i in range(photos)]
    meta = {"cosplayer": cos, "source": source, "character": character, "tags": tags.split(", "), "archives": [], "declared": {}}
    return {"slug": slug, "url": f"https://{site}.example/{slug}/", "title": title, "cosplayer": cos, "status": "meta", "site": site,
            "files_json": json.dumps(files), "meta_json": json.dumps(meta), "published": published, "section": "x", "character": character,
            "source": source, "tags": tags, "zipped": 0, "folder": ""}

class FakeMsg:
    def __init__(s, from_bot=False):
        s.log, s.docs, s.chat, s.text, s.photo = [], [], SimpleNamespace(id=1), "x", None
        s.from_user = SimpleNamespace(is_bot=from_bot)
        async def _noop(*a, **k): return SimpleNamespace(message_id=1)
        s.bot = SimpleNamespace(send_message=_noop, delete_message=_noop)
    async def answer(s, text, reply_markup=None, **k): s.log.append((text, reply_markup)); return s
    async def answer_document(s, doc, caption=None, **k): s.docs.append((doc, caption))
    async def edit_text(s, text, **k): s.log.append((text, None))
    async def delete(s): pass

async def main():
    jm = b.jm = JobManager(tmp + "/data"); lib = jm.lib
    b.BOT = SimpleNamespace(); b.PUBLISH_Q = asyncio.Queue(); b.PUBLISH_LOCK = asyncio.Lock()
    notes = []
    async def fake_notify(text, markup=None): notes.append((text, markup))
    b.notify_admins = fake_notify

    # ---------------- поиск
    for i, (t, c, g, tg) in enumerate([("Byoru – Rabbit (NIKKE)", "Byoru", "NIKKE", "cosplay, nikke"), ("Аска – Школа (Evangelion)", "Аска", "Evangelion", "школа, косплей"),
                                       ("Nerissa Ravencroft – 100%_Set", "Nerissa Ravencroft", "Original", "gold")]):
        lib.save(rec_of(f"p{i}", t, c, source=g, tags=tg, published=f"2026-0{i+1}-01"))
    r, n = lib.search("BYORU nikke"); assert n == 1 and r[0]["slug"] == "p0"
    r, n = lib.search("аска"); assert n == 1 and r[0]["slug"] == "p1"                       # кириллица без учёта регистра
    r, n = lib.search("АСКА"); assert n == 1
    r, n = lib.search("100%_"); assert n == 1 and r[0]["slug"] == "p2"                      # % и _ — обычные символы
    r, n = lib.search("", None, 2, 0); assert [x["slug"] for x in r] == ["p2", "p1"] and n == 3
    r, n = lib.search("", None, 2, 4); assert r == [] and n == 3                            # за пределами: total посчитан
    r, n = lib.search("byoru", "cosplayer"); assert n == 1
    r, n = lib.search("evangelion", "game"); assert n == 1 and lib.search("evangelion", "tag")[1] == 0
    ver = lib._ver; lib.search("byoru"); assert ("byoru", None, 20, 0, None) in lib._cache
    lib.save(rec_of("p9", "Byoru – Maid", "Byoru", source="Original")); assert lib._ver == ver + 1 and not lib._cache   # кэш сброшен записью
    assert lib.search("byoru")[1] == 2
    got = lib.get_many(["p0", "p1", "nope"]); assert set(got) == {"p0", "p1"} and lib.existing_slugs(["p0", "x"]) == {"p0"}
    ok("поиск: регистр и кириллица, % и _ как символы, один запрос, кэш с инвалидацией, get_many")

    # ---------------- дубликаты
    lib.db.execute("DELETE FROM posts"); lib.db.commit()
    lib.save(rec_of("a1", "Byoru – Marciana Marine Study (NIKKE)", site="mitaku", published="2026-01-01"))
    lib.save(rec_of("a2", "Byoru Marciana Marine Study NIKKE", site="cosplaytele", photos=50, published="2026-01-05"))
    lib.save(rec_of("a3", "Byoru – Marciana Marine Study 2 (NIKKE)", site="mitaku", published="2026-01-06"))
    lib.save(rec_of("a4", "Byoru – Eve Xmas (Stellar Blade)", photos=63)); lib.save(rec_of("a5", "Byoru – Eve Nier (Stellar Blade)", photos=58))
    S = {"publish_video_posts": False, "skip_duplicates": True}
    a2 = lib.get("a2")
    assert b.publish_check(a2, S)[0] == "ok"                                                # оригинал ещё не опубликован
    lib.hist_set("a1", "full", "ok", title="Byoru – Marciana Marine Study (NIKKE)", site="mitaku")
    st, why = b.publish_check(a2, S); assert st == "skip_duplicate" and "Mitaku" in why and "Marciana" in why, (st, why)
    assert b.publish_check(a2, S, force=True)[0] == "ok" and b.publish_check(a2, {**S, "skip_duplicates": False})[0] == "ok"
    assert b.publish_check(lib.get("a3"), S)[0] == "ok" and b.publish_check(lib.get("a5"), S)[0] == "ok"   # «часть 2» и другой образ — не дубли
    ok("publish_check: дубликат уже опубликованного пропускается; часть 2 и другой образ — нет; force/настройка отключают")
    async def fake_pp(slug, force=False, overrides=None): return "ok"
    lib.hist_set("a1", "partial", "x"); assert b.publish_check(a2, S)[0] == "skip_duplicate"            # частично опубликованный тоже оригинал
    b.publish_post_real = b.publish_post
    try: await b.publish_paced("a2", False); assert False
    except b.NotPublishable as e: assert e.status == "skip_duplicate"
    h = lib.hist_get("a2"); assert h["status"] == "skip_duplicate" and "дубликат" in h["detail"]
    assert "skip_duplicate" in mp.HIST_GROUPS["skipped"] and "дубликат" in mp.HIST_LABELS["skip_duplicate"]
    ok("publish_paced: дубликат -> NotPublishable и запись в историю (категория «пропущенные»)")
    groups = lib.dup_groups(); assert len(groups) == 1 and {r["slug"] for r in groups[0]} == {"a1", "a2"} and groups[0][0]["slug"] == "a1" and groups[0][0]["is_published"]
    m = FakeMsg(); await b.show_dups(m, 0, keep=True)
    assert "групп: 1" in m.log[-1][0] and "CosplayTele" in m.log[-1][0] and "✅" in m.log[-1][0]
    assert "🔁 Дубликаты: Mitaku ✅" in b.dup_note(lib.get("a2"))
    assert b.dup_note(lib.get("a4")) is None
    ok("/dups и карточка: группа копий, опубликованная первой, пометка в карточке")
    # плановые функции берут посты пачкой и учитывают дубли
    plan = b.plan_publish({"q": "", "field": None, "items": []}, "l"); assert plan["blocked"] >= 1 and plan["ready"] >= 1, plan
    cands = b.publish_candidates(["a2", "a3", "a4"]); assert cands == ["a3", "a4"] and lib.hist_get("a2")["status"] == "skip_duplicate"
    ok("массовая публикация: дубликаты отсеиваются и записываются в историю")

    # ---------------- подписки
    sid, created = lib.sub_add("cosplayer", "Byoru"); assert created and lib.sub_add("cosplayer", " byoru ")[1] is False
    lib.sub_add("game", "NIKKE"); lib.sub_add("tag", "gold")
    m = FakeMsg(); await b.cmd_sub(m, SimpleNamespace(args="игра Stellar Blade")); assert "создана" in m.log[-1][0]
    await b.cmd_sub(m, SimpleNamespace(args="Nerissa")); assert "косплеер" in m.log[-1][0]
    await b.cmd_sub(m, SimpleNamespace(args="Nerissa")); assert "уже есть" in m.log[-1][0]
    new = rec_of("n1", "Byoru – New Set (NIKKE)"); lib.save(new)
    rec, hits = b.sub_check("n1"); assert {h["kind"] for h in hits} == {"cosplayer", "game"} and lib.sub_list(10, 0)[0][0]["id"] is not None
    assert all(s["hits"] == 1 for s in lib.sub_list(10, 0)[0] if s["kind"] in ("cosplayer", "game") and s["nvalue"] in ("byoru", "nikke"))
    lib.save(rec_of("n2", "Unknown – Thing", "Unknown", source="Foo", tags="x")); assert b.sub_check("n2")[1] == []
    ok("подписки: /sub с типами и по умолчанию на косплеера, без дублей, совпадение и счётчик срабатываний")
    b.SUB_QUEUE.clear(); b.SUB_QUEUE.append((lib.get("n1"), hits)); b.SUB_QUEUE.append((lib.get("a4"), hits[:1]))
    real_sleep = asyncio.sleep
    async def fast(s): await real_sleep(0)
    b.asyncio.sleep = fast
    task = asyncio.create_task(b.sub_digest_loop()); await real_sleep(0.05); task.cancel(); b.asyncio.sleep = real_sleep
    text, markup = notes[-1]
    assert "Новые посты по подпискам: 2" in text and "New Set" in text and isinstance(markup, InlineKeyboardMarkup) and len(markup.inline_keyboard) == 2 and not b.SUB_QUEUE
    ok("дайджест подписок: одно сообщение на пачку постов с кнопками «открыть карточку»")
    m = FakeMsg(); await b.show_subs(m, 0, keep=True)
    assert "Подписки" in m.log[-1][0] and isinstance(m.log[-1][1], ReplyKeyboardMarkup)
    first = [a for a in b.MENUS[1].values() if a.startswith("su:")][0]
    class CB:
        def __init__(s, d): s.data, s.message = d, FakeMsg(True)
        async def answer(s, *a, **k): pass
    before = lib.sub_list(1, 0)[1]; await b.cb_sub_delete(CB(first)); assert lib.sub_list(1, 0)[1] == before - 1
    card_labels = [x.text for row in b.card_rows(lib.get("n1"), None) for x in row]; assert any(l.startswith("🔔 Подписаться на Byoru") for l in card_labels)
    ok("/subs: список, удаление кнопкой; кнопка «Подписаться» в карточке")

    # ---------------- CSV
    lib.search_log("byoru", None, 2); lib.search_log("=cmd|' /C calc'!A0", None, 0)
    lib.hist_set("n1", "full", "фото 52/52; ZIP 1", title="Byoru – New Set", cosplayer="Byoru", site="mitaku", photos_total=52, photos_sent=52, msg_id=7)
    m = FakeMsg(); await b.send_csv(m, "history", "all")
    data, cap = m.docs[-1]; rows = list(csv.reader(io.StringIO(data.data.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:5] == ["slug", "Название", "Косплеер", "Сайт", "Статус"] and data.data.startswith(b"\xef\xbb\xbf") and data.filename.startswith("history_all_")
    row = [r for r in rows if r[0] == "n1"][0]; assert row[5] == "✅ Полностью" and row[7] == "52" and row[-1].startswith("https://mitaku.example/n1")
    await b.send_csv(m, "history", "skipped"); rows2 = list(csv.reader(io.StringIO(m.docs[-1][0].data.decode("utf-8-sig")), delimiter=";"))
    assert all(r[4].startswith("skip_") for r in rows2[1:]) and len(rows2) >= 2
    await b.send_csv(m, "searches"); text = m.docs[-1][0].data.decode("utf-8-sig"); assert "'=cmd" in text and "byoru" in text and "Сколько раз искали" in text   # формулы нейтрализованы
    await b.send_csv(m, "subs"); assert "косплеер" in m.docs[-1][0].data.decode("utf-8-sig")
    await b.send_csv(m, "library"); lib_rows = list(csv.reader(io.StringIO(m.docs[-1][0].data.decode("utf-8-sig")), delimiter=";")); assert len(lib_rows) > 3 and lib_rows[0][0] == "slug"
    await b.send_csv(m, "oops"); assert "Использование: /export" in m.log[-1][0]
    ok("CSV: история (все и по категории), поиски, подписки, библиотека; BOM, «;», формулы нейтрализованы")
    c2 = CB("ex:h:partial"); c2.message = FakeMsg(True); await b.cb_export(c2); assert c2.message.docs and "partial" in c2.message.docs[-1][0].filename
    m = FakeMsg(); await b.show_history(m, "all", 0, keep=True); assert "📤 Экспорт CSV" in b.MENUS[1]
    await b.show_searches(m, 0, keep=True); assert "📤 Экспорт CSV" in b.MENUS[1]
    ok("кнопки «Экспорт CSV» в истории, истории поиска и подписках")

    # ---------------- импорт: дубликаты и подписки не мешают
    assert "skip_duplicate" in b.IMPORT_COUNTERS and "skip_duplicate" in b.KNOWN_STATUSES
    text = b.import_text(b.import_new("mitaku"), {"publish_delay": 1, "import_delay": 1, "pause_every": 1, "pause_seconds": 1})
    assert "дубликаты 0" in text
    ok("импорт: счётчик дубликатов в статусе")

asyncio.run(main())
