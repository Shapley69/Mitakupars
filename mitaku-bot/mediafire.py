"""mediafire.py — скачивание файлов и папок с Mediafire.

Отдельный модуль без зависимостей от бота: его можно вызвать из консоли
(`python mediafire.py <ссылка> [папка]`) или из кода — `download(url, out_dir)`.

Работает только с публичными ссылками Mediafire. Ссылки ouo.io он не раскрывает и капчу не обходит:
итоговую ссылку Mediafire нужно получить самому (открыть ouo.io в браузере).
Файлы под паролем Mediafire и удалённые файлы не поддерживаются — модуль сообщит об этом ошибкой.
"""
import base64
import html as htmllib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
FOLDER_API = "https://www.mediafire.com/api/1.4/folder/get_content.php"
MAX_FOLDER_FILES = 30          # больше файлов в одной папке за раз не качаем
KEY_RE = r"[A-Za-z0-9]{8,20}"
CHUNK = 256 * 1024

# Признаки страниц-«заглушек» Mediafire (проверяются по тексту страницы без учёта регистра)
GONE_MARKERS = ("invalid or deleted file", "file removed for violation", "has been deleted",
                "the file you attempted to download is invalid", "dmca")
PASSWORD_MARKERS = ("form_password", "enter the password", "password protected")


class MediafireError(Exception):
    pass


class Cancelled(MediafireError):
    """Скачивание остановлено вызывающим кодом (stop() вернул True)."""


@dataclass
class MFile:
    name: str
    page_url: str          # страница файла (или прямая ссылка download*.mediafire.com)
    size: int = 0


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def safe_name(name, fallback="mediafire_file"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name or "").strip(" .")
    return name[:200] or fallback


def is_mediafire_url(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host == "mediafire.com" or host.endswith(".mediafire.com")


def parse_link(url):
    """('file'|'folder'|'direct', ключ_или_url) либо None, если это не ссылка Mediafire."""
    if not is_mediafire_url(url):
        return None
    p = urlparse(url)
    if re.match(r"^download\d*\.mediafire\.com$", p.netloc.lower()):
        return "direct", url
    parts = [x for x in p.path.split("/") if x]
    if parts and parts[0] == "folder" and len(parts) > 1:
        return "folder", parts[1]
    if parts and parts[0] in ("file", "view", "download", "file_premium") and len(parts) > 1:
        return "file", parts[1]
    if not parts and re.fullmatch(KEY_RE, p.query or ""):   # старый вид: mediafire.com/?<ключ>
        return "file", p.query
    return None


# --------------------------------------------------------------------------- разбор страниц
def _decode_scrambled(value):
    try:
        link = base64.b64decode(value).decode()
    except Exception:  # noqa: BLE001
        return None
    return link if link.startswith("http") else None


def direct_link_from_html(text):
    """Прямая ссылка на файл из страницы Mediafire (кнопка Download) или None."""
    soup = BeautifulSoup(text, "html.parser")
    button = (soup.select_one("a#downloadButton") or soup.select_one("a.popsok")
              or soup.select_one("a[aria-label*='ownload']"))
    if button:
        scrambled = button.get("data-scrambled-url")
        if scrambled and _decode_scrambled(scrambled):
            return _decode_scrambled(scrambled)
        href = (button.get("href") or "").strip()
        if href.startswith("http") and "mediafire.com" in href:
            return htmllib.unescape(href)
    m = re.search(r'data-scrambled-url="([^"]+)"', text)
    if m and _decode_scrambled(m.group(1)):
        return _decode_scrambled(m.group(1))
    m = re.search(r"https?://download\d*\.mediafire\.com/[^\"'<>\s]+", text)
    return htmllib.unescape(m.group(0)) if m else None


def page_problem(text):
    """Человекочитаемая причина, почему файл нельзя скачать, или None."""
    low = text[:200000].lower()
    if any(x in low for x in PASSWORD_MARKERS):
        return "файл защищён паролем Mediafire — бот такие файлы не скачивает"
    if any(x in low for x in GONE_MARKERS):
        return "файл удалён или недоступен на Mediafire"
    return None


def name_from_page(text, url):
    soup = BeautifulSoup(text, "html.parser")
    label = soup.select_one("div.dl-btn-label")
    if label and label.get("title"):
        return safe_name(label["title"])
    node = soup.select_one("div.filename")
    if node and node.get_text(strip=True):
        return safe_name(node.get_text(strip=True))
    return name_from_url(url)


def name_from_url(url):
    parts = [x for x in urlparse(url).path.split("/") if x]
    if len(parts) >= 3 and parts[0] in ("file", "view"):
        return safe_name(unquote(parts[2]))
    return safe_name(unquote(parts[-1])) if parts else "mediafire_file"


def name_from_headers(headers, fallback):
    disposition = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=(?:UTF-8''|utf-8'')([^;]+)", disposition, re.I)
    if m:
        return safe_name(unquote(m.group(1).strip().strip('"')))
    m = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    return safe_name(unquote(m.group(1))) if m else fallback


# --------------------------------------------------------------------------- получение списка файлов
def list_folder(session, key):
    """Файлы публичной папки Mediafire (через их же API)."""
    files, chunk = [], 1
    while True:
        r = session.get(FOLDER_API, timeout=30, params={
            "folder_key": key, "content_type": "files", "chunk": chunk, "chunk_size": 100,
            "response_format": "json", "version": "1.5"})
        r.raise_for_status()
        data = r.json().get("response", {})
        if data.get("result") != "Success":
            raise MediafireError(f"папка недоступна: {data.get('message') or 'ошибка API'}")
        content = data.get("folder_content", {})
        for f in content.get("files", []):
            link = (f.get("links") or {}).get("normal_download") or (
                f"https://www.mediafire.com/file/{f['quickkey']}/{f.get('filename', '')}/file")
            files.append(MFile(safe_name(f.get("filename")), link, int(f.get("size") or 0)))
        if content.get("more_chunks") != "yes":
            break
        chunk += 1
        if len(files) > MAX_FOLDER_FILES:
            break
    if not files:
        raise MediafireError("в папке нет файлов")
    if len(files) > MAX_FOLDER_FILES:
        raise MediafireError(f"в папке больше {MAX_FOLDER_FILES} файлов — пришлите ссылки на "
                             "нужные файлы по отдельности")
    return files


def resolve(session, url):
    """Список MFile по ссылке на файл или папку."""
    kind = parse_link(url)
    if not kind:
        raise MediafireError("это не ссылка Mediafire")
    if kind[0] == "folder":
        return list_folder(session, kind[1])
    return [MFile(name_from_url(url), url)]


def resolve_direct(session, mfile):
    """(прямая_ссылка, имя) для файла: открывает его страницу и берёт ссылку кнопки Download."""
    if parse_link(mfile.page_url)[0] == "direct":
        return mfile.page_url, mfile.name
    r = session.get(mfile.page_url, timeout=30, stream=True)
    try:
        if r.status_code == 404:
            raise MediafireError("файл не найден на Mediafire (404)")
        r.raise_for_status()
        if "text/html" not in r.headers.get("Content-Type", "").lower():
            return r.url, name_from_headers(r.headers, mfile.name)   # редирект сразу на файл
        text = r.text
    finally:
        r.close()
    problem = page_problem(text)
    direct = direct_link_from_html(text)
    if not direct:
        raise MediafireError(problem or "не нашёл кнопку скачивания на странице Mediafire")
    name = name_from_page(text, mfile.page_url) if mfile.name == "mediafire_file" else mfile.name
    return direct, name


# --------------------------------------------------------------------------- скачивание
def _free_path(directory, name):
    path = directory / name
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = directory / f"{stem} ({n}){suffix}"
        n += 1
    return path


def download(url, out_dir, progress=None, stop=None, session=None, max_bytes=0):
    """Скачивает файл или все файлы папки Mediafire в out_dir. Возвращает список путей.

    progress(имя, скачано_байт, всего_байт, номер_файла, всего_файлов) — не чаще раза в 3 с;
    stop() -> True прерывает скачивание (Cancelled); max_bytes — максимум на один файл (0 — без
    лимита): слишком большой файл отклоняется до загрузки, по заголовку Content-Length."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    own = session is None
    session = session or make_session()
    try:
        files = resolve(session, url)
        paths = []
        for index, mfile in enumerate(files, 1):
            direct, name = resolve_direct(session, mfile)
            with session.get(direct, stream=True, timeout=(15, 60)) as r:
                r.raise_for_status()
                if "text/html" in r.headers.get("Content-Type", "").lower():
                    raise MediafireError("Mediafire вернул страницу вместо файла "
                                         "(ссылка устарела или нужна авторизация)")
                name = name_from_headers(r.headers, name)
                total = int(r.headers.get("Content-Length") or mfile.size or 0)
                if max_bytes and total > max_bytes:
                    raise MediafireError(
                        f"файл {total / 1024 ** 3:.2f} ГБ не помещается: доступно {max_bytes / 1024 ** 3:.2f} ГБ "
                        "(свободное место на диске бота или лимит)")
                target = _free_path(out_dir, name)
                partial = target.with_name(target.name + ".part")
                done, last = 0, 0.0
                try:
                    with open(partial, "wb") as fh:
                        for chunk in r.iter_content(CHUNK):
                            if stop and stop():
                                raise Cancelled("скачивание остановлено")
                            fh.write(chunk)
                            done += len(chunk)
                            if max_bytes and done > max_bytes:
                                raise MediafireError("файл больше допустимого размера — остановлено")
                            now = time.monotonic()
                            if progress and now - last >= 3:
                                last = now
                                progress(target.name, done, total, index, len(files))
                    if done == 0:
                        raise MediafireError("Mediafire отдал пустой файл")
                    partial.replace(target)
                except BaseException:
                    partial.unlink(missing_ok=True)
                    raise
            paths.append(target)
        return paths
    except requests.RequestException as e:
        raise MediafireError(f"ошибка сети: {type(e).__name__}: {e}") from e
    finally:
        if own:
            session.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Использование: python mediafire.py <ссылка Mediafire> [папка]")
    target = sys.argv[2] if len(sys.argv) > 2 else "."

    def show(name, done, total, i, n):
        print(f"[{i}/{n}] {name}: {done / 1048576:.1f} / {total / 1048576:.1f} МБ")

    try:
        for saved in download(sys.argv[1], target, progress=show):
            print("Готово:", saved)
    except MediafireError as e:
        sys.exit(f"Ошибка: {e}")
