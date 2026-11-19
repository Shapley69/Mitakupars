"""video_fetch.py — скачивание видео из плееров (iframe) и потоков HLS/DASH через yt-dlp.

Сайты из библиотеки не отдают видео файлом: оно лежит во внешнем плеере (iframe). Парсер находит такие
ссылки и сохраняет их в meta как `video_embeds`; скачивает их этот модуль.

Зависимость необязательная: `pip install yt-dlp` (для склейки потоков нужен ещё ffmpeg в PATH).
Поддерживаются те хосты, которые умеет yt-dlp; для остальных скачивание завершится ошибкой
VideoError с причиной — файл по ссылке получить нельзя.
"""
import importlib.util
import logging
import shutil
from pathlib import Path


class VideoError(Exception):
    pass


def available():
    return importlib.util.find_spec("yt_dlp") is not None


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def download(url, out_dir, referer=None, max_bytes=0, progress=None, stop=None):
    """Скачивает видео по ссылке на плеер/поток в out_dir и возвращает путь к mp4-файлу.

    progress(скачано_байт, всего_байт) — вызывается из потока загрузки; stop() -> True прерывает;
    max_bytes — максимальный размер файла (0 — без лимита)."""
    if not available():
        raise VideoError("не установлен yt-dlp: pip install yt-dlp")
    import yt_dlp
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def hook(info):
        if stop and stop():
            raise yt_dlp.utils.DownloadCancelled()
        if progress and info.get("status") == "downloading":
            progress(int(info.get("downloaded_bytes") or 0),
                     int(info.get("total_bytes") or info.get("total_bytes_estimate") or 0))

    options = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        # предпочитаем mp4, который Telegram проигрывает без перекодирования
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True, "quiet": True, "no_warnings": True, "restrictfilenames": True,
        "retries": 3, "fragment_retries": 3, "socket_timeout": 30,
        "progress_hooks": [hook],
    }
    if referer:
        options["http_headers"] = {"Referer": referer}
    if max_bytes:
        options["max_filesize"] = int(max_bytes)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            downloads = (info or {}).get("requested_downloads") or []
            path = Path(downloads[0]["filepath"]) if downloads else Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadCancelled as e:
        raise VideoError("скачивание остановлено") from e
    except yt_dlp.utils.DownloadError as e:
        logging.info("yt-dlp: %s", e)
        raise VideoError(str(e).replace("ERROR: ", "")[:300]) from e
    if not path.is_file() or path.stat().st_size == 0:
        raise VideoError("yt-dlp не создал файл (видео не найдено или превышен лимит размера)")
    return path
