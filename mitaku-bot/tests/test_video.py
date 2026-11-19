import asyncio, json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE / "stubs")]   # заглушки aiogram/telethon/yt_dlp, код проекта
tmp = tempfile.mkdtemp()
os.environ.update(BOT_TOKEN="1:x", ADMIN_IDS="1", CHANNEL_ID="@test", MITAKU_OUT=tmp + "/data")
os.environ.pop("DATABASE_URL", None); os.environ.pop("MT_API_ID", None); os.environ.pop("MT_API_HASH", None)
import mitaku_bot as b, video_fetch, yt_dlp
from mitaku_jobs import JobManager
ok = lambda n: print("OK  ", n)

def rec_of(slug="r", title="Byoru – Rabbit Set (NIKKE)", cosplayer="Byoru", character="", source="NIKKE", files=(), archives=(), embeds=None, declared_videos=0):
    meta = {"cosplayer": cosplayer, "character": character, "source": source, "tags": ["cosplay"], "archives": list(archives),
            "declared": {"videos": declared_videos} if declared_videos else {}, "formats": {"photo": {"jpg": 3}, "video": {}}, "files": []}
    if embeds is not None: meta["video_embeds"] = embeds
    fl = [{"name": f"{slug}-{i}.jpg", "kind": "photo", "url": "u"} for i in range(3)] + list(files)
    return {"id": 1, "slug": slug, "url": f"https://fake/{slug}/", "title": title, "cosplayer": cosplayer, "status": "meta", "site": "mitaku",
            "files_json": json.dumps(fl), "meta_json": json.dumps(meta), "published": "2026", "section": "x", "character": character,
            "source": source, "tags": "cosplay", "zipped": 0, "folder": "", "total_bytes": 0}

class FakeMsg:
    def __init__(s):
        s.log, s.chat, s.text, s.from_user, s.photo = [], SimpleNamespace(id=1), "x", SimpleNamespace(is_bot=True), None
        async def _noop(*a, **k): return SimpleNamespace(message_id=1)
        s.bot = SimpleNamespace(send_message=_noop, delete_message=_noop)
    async def answer(s, text, **k): s.log.append(text); return s
    async def edit_text(s, text, **k): s.log.append(text)
class CB:
    def __init__(s, data): s.data, s.message, s.alerts = data, FakeMsg(), []
    async def answer(s, text=None, show_alert=False): 
        if text: s.alerts.append(text)

async def main():
    jm = b.jm = JobManager(tmp + "/data")
    # ---------- video_fetch
    out = Path(tmp) / "vf"
    path = video_fetch.download("https://bysesayeveum.com/e/x", out, referer="https://mitaku.net/p/", max_bytes=5000, progress=lambda d, t: prog.append((d, t)), stop=None) if (prog := []) is not None else None
    o = yt_dlp.YoutubeDL.last_options
    assert path.name == "vid1.mp4" and path.stat().st_size == 2048 and prog == [(5, 10)]
    assert o["http_headers"] == {"Referer": "https://mitaku.net/p/"} and o["max_filesize"] == 5000 and o["merge_output_format"] == "mp4" and o["noplaylist"]
    yt_dlp.YoutubeDL.behavior = "error"
    try: video_fetch.download("https://x/e/1", out); assert False
    except video_fetch.VideoError as e: assert "Unsupported URL" in str(e) and "ERROR:" not in str(e)
    yt_dlp.YoutubeDL.behavior = "ok"
    ok("video_fetch: yt-dlp с Referer, лимитом размера, прогрессом; ошибки -> VideoError")

    # ---------- плееры / архивы / подписи
    r = rec_of(archives=["https://ouo.io/a", "https://bysesayeveum.com/e/old1"], embeds=["https://cossora.stream/e/n1"], declared_videos=2)
    arch, players = b.post_archives(r)
    assert arch == ["https://ouo.io/a"] and players == ["https://cossora.stream/e/n1", "https://bysesayeveum.com/e/old1"]
    old = rec_of(archives=["https://ouo.io/a", "https://bysesayeveum.com/e/old1"])      # разобран до video_embeds
    assert b.post_archives(old) == (["https://ouo.io/a"], ["https://bysesayeveum.com/e/old1"])
    assert b.publish_verdict(r, {})[0] == "skip_video" and b.publish_verdict(r, {"publish_video_posts": True})[0] == "ok"
    ok("post_archives: плееры отдельно от архивов (новые и старые посты), правило «видео не публикуем»")
    cases = [
        (dict(), "Byoru – Rabbit Set (NIKKE)"),                                            # из названия: без ника и скобки + игра
        (dict(character="Rabbit", title="Byoru – Crystal Wave (NIKKE)"), "Byoru – Rabbit (NIKKE)"),
        (dict(source="", title="Byoru - Velvet Rabbit"), "Byoru – Velvet Rabbit"),
        (dict(cosplayer="", character="Mikasa", source="Attack on Titan"), "Mikasa (Attack on Titan)"),
        (dict(character="Byoru", source="NIKKE"), "Byoru (NIKKE)"),
    ]
    for kw, want in cases:
        got = b.archive_label(rec_of(**kw)); assert got == want, (kw, got, want)
    ok("archive_label: «Ник – персонаж (игра/аниме)» для разных данных")
    text = b.card(r); assert "🎬 Видео: файлов 0, в плеерах 2" in text and "Плееры:" in text and "ouo.io/a" in text and "bysesayeveum.com/e/old1" in text.split("Плееры:")[1], text
    assert "заявлено на сайте, но не найдено" in b.card(rec_of(declared_videos=1, embeds=[]))
    labels = [x.text for row in b.card_rows(r, None) for x in row]; assert "🎬 Скачать видео" in labels and "🔗 Архивы" in labels
    ct = b.channel_text(r); assert "🎬 Видео: 2" in ct and "bysesayeveum" not in ct and "cossora" not in ct and "Архив 1" in ct
    ok("карточка и пост в канале: счётчик видео, плееры отдельно, кнопка «Скачать видео»")

    # ---------- ZIP-имя и подпись
    rr = rec_of(character="Rabbit", title="Byoru – Crystal Wave (NIKKE)")
    b.ZIP_PART_SIZE = 10**9
    b.staged = None
    # ---------- cb_videos
    sent = []
    async def fake_send(chat_id, path, **k): sent.append((chat_id, Path(path).name, k.get("caption"), k.get("name"), k.get("video"))); return 1
    b.send_file_any = fake_send
    made = []
    async def fake_dl(url, rec, progress=None):
        d = Path(tmp) / f"dl{len(made)}"; d.mkdir(exist_ok=True); f = d / "vid1.mp4"; f.write_bytes(b"x"); made.append(d); return f
    b.download_video = fake_dl
    jm.lib.save(rec_of("v1", character="Rabbit", title="Byoru – Crystal Wave (NIKKE)", embeds=["https://bysesayeveum.com/e/1", "https://cossora.stream/e/2"], declared_videos=2))
    rid = jm.lib.get("v1")["id"]
    video_fetch.available = lambda: True
    c = CB(f"vd:{rid}"); await b.cb_videos(c)
    assert [s[1] for s in sent] == ["vid1.mp4", "vid1.mp4"] and sent[0][3] == "Byoru – Rabbit (NIKKE).mp4" and sent[0][4] is True
    assert "Отправлено видео: 2" in c.message.log[-1] and all(not d.exists() for d in made)
    ok("cb_videos: видео из плееров отправлены с именем «Ник – персонаж (игра).mp4», временные файлы удалены")
    video_fetch.available = lambda: False
    c2 = CB(f"vd:{rid}"); await b.cb_videos(c2); assert any("yt-dlp" in a for a in c2.alerts)
    video_fetch.available = lambda: True

    # ---------- send_comments: видео из плееров по настройке
    async def fake_parts(*a, **k): return 0
    b.send_parts = fake_parts; b.SEND_PAUSE = 0
    b.file_exists = lambda rec, f: True; b.photo_bytes = lambda rec, f: b"x"
    class FB:
        async def send_media_group(s, **k): pass
        async def send_photo(s, **k): pass
    b.BOT = FB()
    rec = jm.lib.get("v1"); sent.clear(); made.clear()
    stats, prog = {}, {}
    await b.send_comments(-100, 5, rec, {"channel_videos": True, "download_embedded_video": True, "channel_zip": False, "channel_media": "photos"}, stats, prog)
    assert stats["videos"] == 2 and prog["embeds"] == 2 and len(sent) == 2
    sent.clear(); stats2 = {}
    await b.send_comments(-100, 5, rec, {"channel_videos": True, "download_embedded_video": True, "channel_zip": False, "channel_media": "photos"}, stats2, {"embeds": 1})
    assert stats2["videos"] == 2 and len(sent) == 1                 # одно уже отправлено раньше
    sent.clear(); stats3 = {}
    await b.send_comments(-100, 5, rec, {"channel_videos": True, "download_embedded_video": False, "channel_zip": False, "channel_media": "photos"}, stats3, {})
    assert stats3["videos"] == 0 and not sent                        # по умолчанию выкл.
    async def bad_dl(url, rec, progress=None): raise RuntimeError("видео из плеера: Unsupported URL")
    b.download_video = bad_dl; stats4 = {}
    await b.send_comments(-100, 5, rec, {"channel_videos": True, "download_embedded_video": True, "channel_zip": False, "channel_media": "photos"}, stats4, {})
    assert stats4["video_errors"] == 2 and stats4["videos"] == 0
    ok("send_comments: видео из плееров по download_embedded_video, продолжение с места обрыва, ошибки не валят публикацию")

    # ---------- переименование архива из поста
    async def fake_fetch(url, work, progress=None, max_bytes=0):
        work.mkdir(parents=True, exist_ok=True); p1 = work / "weird_name.zip"; p1.write_bytes(b"z"); return [(p1, "weird_name.zip")]
    b.fetch_archive = fake_fetch
    names = []
    async def fake_big(m, path, name, caption, progress_for=None): names.append((name, Path(path).exists(), caption))
    b.send_big_file = fake_big
    async def edit(*a, **k): pass
    class M(FakeMsg):
        async def answer(s, text, **k): s.log.append(text); return s
    async def noop(*a, **k): pass
    b.delete_message = noop
    b.STAGING = Path(tmp) / "stg"
    res = await b.deliver_archive(M(), "https://www.mediafire.com/file/abcd1234efgh/x.zip/file", "Byoru – Rabbit (NIKKE)", "mitaku.net", rename_to="Byoru – Rabbit (NIKKE)")
    assert res == "ok" and names[0][0] == "Byoru – Rabbit (NIKKE).zip" and names[0][1] and "Пароль архива" in names[0][2]
    names.clear(); await b.deliver_archive(M(), "https://www.mediafire.com/file/abcd1234efgh/x.zip/file", "T", None, rename_to="Byoru – Rabbit (NIKKE) (архив 2)")
    assert names[0][0] == "Byoru – Rabbit (NIKKE) (архив 2).zip"
    names.clear(); await b.deliver_archive(M(), "https://www.mediafire.com/file/abcd1234efgh/x.zip/file")
    assert names[0][0] == "weird_name.zip"                          # ссылка из чата: имя файла не трогаем
    ok("архивы поста: имя «Ник – персонаж (игра).zip» и подпись с паролем; из чата имя не меняется")

    # ---------- video_report / refresh
    jm.lib.save(rec_of("old1", archives=["https://bysesayeveum.com/e/q"], declared_videos=1))         # без video_embeds
    jm.lib.save(rec_of("new1", embeds=["https://x.streamtape.com/e/1"], declared_videos=1))
    jm.lib.save(rec_of("none1", embeds=[], declared_videos=1))
    jm.lib.save(rec_of("novid", embeds=[]))
    rpt = jm.lib.video_report()
    assert rpt["declared"] == 4 and rpt["embeds"] == 3 and rpt["missing"] == 1 and rpt["refresh"] == ["https://fake/old1/"], rpt
    m = M(); await b.cmd_videos(m); assert "постов с заявленным видео: 4" in m.log[-1].lower() or "заявленным видео: 4" in m.log[-1], m.log[-1]
    ok("/videos: статистика, посты без данных о плеерах идут на обновление")

asyncio.run(main())
