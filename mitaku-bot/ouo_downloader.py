import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import aiohttp
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

MIME_EXT = {
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "text/plain": ".txt",
}

OUO_HOSTS = ("ouo.io", "ouo.press")
# Хосты, с которых бот умеет качать (см. mitaku_parser.resolve_archive)
FILE_HOSTS = ("mediafire.com", "pixeldrain.com")
URL_RE = re.compile(r"""https?://[^\s<>"']+""", re.I)

CAPTCHA_HELP = (
    "ouo.io закрыт капчей, бот её не проходит. Откройте ссылку в браузере, пройдите капчу "
    "и пришлите мне итоговую ссылку (Mediafire / Pixeldrain): я скачаю файл, при необходимости "
    "разрежу на ZIP-части меньше 50 МБ и отправлю."
)


class OuoError(Exception):
    pass


class OuoCaptcha(OuoError):
    """ouo.io показал капчу: нужен ручной шаг в браузере."""


def _host(url):
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _host_in(url, hosts):
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in hosts)


def is_ouo(url):
    return _host_in(url, OUO_HOSTS)


def extract_archive_urls(text):
    """Все ссылки в тексте на ouo.io / Mediafire / Pixeldrain (без повторов, по порядку)."""
    out = []
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;)]}>")
        if (is_ouo(url) or _host_in(url, FILE_HOSTS)) and url not in out:
            out.append(url)
    return out


def extract_ouo_url(text):
    for url in extract_archive_urls(text):
        if is_ouo(url):
            return url
    return None


def is_direct_download(url):
    path = urlparse(url).path.lower()
    return bool(re.match(r"^/(?:dl|download|d)/", path))


def extract_file_id(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    for key in ("id", "file_id", "fid"):
        if key in query and query[key]:
            return unquote(query[key][0])

    parts = [part for part in parsed.path.split("/") if part]

    for part in reversed(parts):
        if re.fullmatch(r"[A-Za-z0-9_-]{4,}", part) and part.lower() not in {
            "file",
            "dl",
            "download",
            "d",
        }:
            return unquote(part)

    return None


def sanitize_filename(name, fallback="ouo_file"):
    if not name:
        return fallback

    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(name)).strip(" ._")
    return (name or fallback)[:255]


def clean_title(title):
    if not title:
        return None

    name = re.sub(
        r"\s*[-|–—]\s*(?:ouo|file|download).*$",
        "",
        title,
        flags=re.I,
    ).strip()

    name = sanitize_filename(name)
    return name or None


def guess_extension(content_type):
    if not content_type:
        return ""

    mime = content_type.split(";")[0].strip().lower()
    return MIME_EXT.get(mime, "")


def ensure_extension(filename, content_type):
    base = os.path.basename(filename)
    ext = os.path.splitext(base)[1]

    if ext and 1 < len(ext) <= 6:
        return filename

    guessed = guess_extension(content_type)
    if guessed:
        return f"{filename}{guessed}"

    return filename


def sniff_extension(head):
    """Расширение по первым байтам файла (когда сервер не сообщил имя/тип)."""
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return ".zip"
    if head.startswith(b"Rar!"):
        return ".rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    return ""


def is_html_response(resp):
    ctype = resp.headers.get("Content-Type", "").lower()
    return "text/html" in ctype or "application/xhtml+xml" in ctype


def looks_like_challenge(html):
    low = html[:30000].lower()
    markers = ("just a moment", "cf-browser-verification", "challenge-platform")
    return any(marker in low for marker in markers)


def looks_like_captcha(html):
    low = html[:200000].lower()
    markers = ("g-recaptcha", "recaptcha/api", "cf-turnstile", "hcaptcha", "h-captcha",
               "form-captcha")
    return any(marker in low for marker in markers)


def find_host_link(html):
    """Ссылка на поддерживаемый файловый хост прямо в тексте страницы (meta refresh, JS, <a>)."""
    for match in URL_RE.finditer(html or ""):
        url = match.group(0).rstrip(".,;)]}>\\")
        if _host_in(url, FILE_HOSTS):
            return url
    return None


def find_download_request(html, page_url):
    if is_direct_download(page_url):
        return page_url, "GET", {}

    soup = BeautifulSoup(html, "html.parser")
    best = None
    best_score = -1

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("javascript:", "#")):
            continue

        abs_url = urljoin(page_url, href)
        text = anchor.get_text(strip=True).lower()
        path = urlparse(abs_url).path.lower()
        score = 0

        if "/dl/" in path or path.endswith("/dl") or "/download" in path:
            score += 3
        if "download" in text:
            score += 2
        if any(key in href.lower() for key in ("token", "file_id", "id=")):
            score += 1

        if score > best_score:
            best_score = score
            best = (abs_url, "GET", {})

    if best and best_score >= 2:
        return best

    for form in soup.find_all("form"):
        action = urljoin(page_url, form.get("action") or "")
        method = (form.get("method") or "get").lower()
        data = {}
        for element in form.find_all(["input", "select", "textarea"]):
            name = element.get("name")
            if not name:
                continue

            value = element.get("value", "")
            if element.name == "select":
                selected = element.find("option", selected=True) or element.find("option")
                if selected is not None:
                    value = selected.get("value", "")
            data[name] = value

        score = 0
        action_path = urlparse(action).path.lower()
        if "/download" in action_path or "/dl/" in action_path:
            score += 3
        if any(key in data for key in ("file_id", "id", "token")):
            score += 2
        if method == "post":
            score += 1

        if action and score >= 2 and (best is None or score > best_score):
            return action, "POST" if method == "post" else "GET", data

    match = re.search(
        r"""href=["']([^"']*?(?:/dl|download)/[^"'?]+)""",
        html,
        re.I,
    )
    if match:
        return urljoin(page_url, unquote(match.group(1))), "GET", {}

    raise OuoError("Could not find a direct download link on the page.")


class OuoClient:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)

    async def _request(self, session, method, url, data=None, referer=None,
                       allow_redirects=True):
        headers = dict(HEADERS)
        cookie = os.getenv("OUO_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        if referer:
            headers["Referer"] = referer

        return await session.request(
            method,
            url,
            headers=headers,
            data=data,
            allow_redirects=allow_redirects,
            timeout=self.timeout,
        )

    @staticmethod
    def _filename_from_headers(headers):
        content_disposition = headers.get("Content-Disposition", "")
        if not content_disposition:
            return None

        match = re.search(
            r"""filename\*=(?:UTF-8''|utf-8'')?([^;]+)""",
            content_disposition,
            re.I,
        )
        if match:
            return unquote(match.group(1).strip().strip('"'))

        match = re.search(r"""filename="?([^";]+)"?""", content_disposition, re.I)
        if match:
            return unquote(match.group(1).strip())

        return None

    async def _read_file(self, resp, url, page_title=None):
        if resp.status != 200:
            raise OuoError(f"Download failed with HTTP {resp.status}")

        tmp = tempfile.NamedTemporaryFile(prefix="ouo-", delete=False)
        path = Path(tmp.name)
        completed = False
        size = 0
        sample = b""

        try:
            async for chunk in resp.content.iter_chunked(64 * 1024):
                tmp.write(chunk)
                size += len(chunk)

                if len(sample) < 30000:
                    sample += chunk[:30000 - len(sample)]

            if size == 0:
                raise OuoError("Empty file response")

            if is_html_response(resp) and not is_direct_download(url):
                text_sample = sample.decode("utf-8", "ignore")
                if looks_like_challenge(text_sample):
                    raise OuoError(
                        "Anti-bot/Cloudflare challenge detected. "
                        "Try setting OUO_COOKIE from your browser."
                    )

            content_type = resp.headers.get("Content-Type", "")
            filename = (
                self._filename_from_headers(resp.headers)
                or clean_title(page_title)
                or "ouo_file"
            )
            filename = ensure_extension(sanitize_filename(filename), content_type)
            if not os.path.splitext(filename)[1]:
                filename += sniff_extension(sample[:8])
            completed = True
            return path, filename, size
        finally:
            tmp.close()
            if not completed:
                path.unlink(missing_ok=True)

    async def resolve(self, start_url, max_hops=6):
        """Идёт по редиректам ouo.io и возвращает ссылку на итоговый хост (Mediafire и т. д.).
        Капчу не обходит: если ouo.io её показывает, бросает OuoCaptcha."""
        url = start_url
        async with aiohttp.ClientSession(headers=HEADERS, timeout=self.timeout) as session:
            for _ in range(max_hops):
                resp = await self._request(session, "GET", url, allow_redirects=False)
                async with resp:
                    location = resp.headers.get("Location")
                    if resp.status in (301, 302, 303, 307, 308) and location:
                        nxt = urljoin(url, location)
                        if not is_ouo(nxt):
                            return nxt
                        url = nxt
                        continue
                    if resp.status == 403:
                        raise OuoError(
                            "HTTP 403: сайт блокирует бота. Можно задать OUO_COOKIE "
                            "из браузера или открыть ссылку вручную.")
                    html = await resp.text(errors="replace")
                if looks_like_captcha(html):
                    raise OuoCaptcha(CAPTCHA_HELP)
                if looks_like_challenge(html):
                    raise OuoError("Защита Cloudflare. Откройте ссылку в браузере вручную.")
                link = find_host_link(html)
                if link:
                    return link
                raise OuoError("На странице ouo.io нет ссылки на поддерживаемый хост.")
        raise OuoError("Слишком много перенаправлений на ouo.io.")

    async def download(self, start_url):
        parsed_start = urlparse(start_url)
        base_url = f"{parsed_start.scheme}://{parsed_start.netloc}"

        async with aiohttp.ClientSession(
            headers=HEADERS,
            timeout=self.timeout,
        ) as session:
            if is_direct_download(start_url):
                resp = await self._request(session, "GET", start_url, referer=start_url)
                return await self._read_file(resp, start_url)

            page_resp = await self._request(session, "GET", start_url)
            if page_resp.status == 403:
                raise OuoError(
                    "HTTP 403. The site may be blocking bots; "
                    "try setting OUO_COOKIE from your browser."
                )

            html = await page_resp.text(errors="replace")
            if looks_like_captcha(html):
                raise OuoCaptcha(CAPTCHA_HELP)
            if looks_like_challenge(html):
                raise OuoError(
                    "Anti-bot/Cloudflare challenge detected. "
                    "Use a browser cookie or Playwright/curl_cffi."
                )

            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.string if soup.title else None

            try:
                url, method, data = find_download_request(html, start_url)
                resp = await self._request(
                    session,
                    method.upper(),
                    url,
                    data=data,
                    referer=start_url,
                )
                return await self._read_file(resp, url, title)
            except OuoError:
                file_id = extract_file_id(start_url)
                if not file_id:
                    raise

                candidates = [
                    f"{base_url}/dl/{file_id}",
                    f"{base_url}/download/{file_id}",
                    f"{base_url}/d/{file_id}",
                    f"{base_url}/file/{file_id}/download",
                    f"{base_url}/api/download/{file_id}",
                ]

                last_error = None
                for candidate in candidates:
                    resp = await self._request(
                        session, "GET", candidate, referer=start_url)
                    if resp.status == 200 and not is_html_response(resp):
                        return await self._read_file(resp, candidate, title)

                    last_error = OuoError(
                        f"Candidate {candidate} returned HTTP {resp.status}")
                    resp.release()

                raise OuoError(
                    "Could not find a working direct download URL.") from last_error


async def fetch_archive(url, dest_dir, progress=None, stop=None, max_bytes=0):
    """Скачивает файл(ы) по ссылке ouo.io / Mediafire / Pixeldrain в dest_dir.
    Возвращает [(путь, имя_файла)]. Ошибки — OuoError (OuoCaptcha — нужна капча).
    progress(имя, скачано, всего, №файла, файлов), stop() и max_bytes — только для Mediafire."""
    import mitaku_parser as mp   # импорт здесь: парсер не нужен, пока ничего не качаем
    import requests

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    client = OuoClient()
    if is_ouo(url):
        try:
            url = await client.resolve(url)
        except OuoCaptcha:
            raise
        except OuoError:
            # Запасной путь: страница сама отдаёт файл (прежняя логика загрузчика)
            path, name, _ = await client.download(url)
            target = dest_dir / sanitize_filename(name)
            shutil.move(str(path), target)
            return [(target, target.name)]

    if _host_in(url, ("mediafire.com",)):   # Mediafire — отдельный модуль (файлы и папки)
        import mediafire
        try:
            paths = await asyncio.to_thread(mediafire.download, url, dest_dir, progress, stop, None, max_bytes)
        except mediafire.MediafireError as e:
            raise OuoError(f"Mediafire: {e}") from e
        return [(p, p.name) for p in paths]

    session = mp.make_session()
    try:
        links = await asyncio.to_thread(mp.resolve_archive, session, url)
        out = []
        for direct, name in links:
            path = await asyncio.to_thread(
                mp.download_file, session, direct, dest_dir, name, url)
            if not path.suffix:
                with path.open("rb") as fh:
                    ext = sniff_extension(fh.read(8))
                if ext:
                    path = path.rename(path.with_name(path.name + ext))
            out.append((path, path.name))
        return out
    except OuoError:
        raise
    except ValueError as e:
        host = _host(url)
        hint = "" if _host_in(url, FILE_HOSTS) else f" (хост {host} не поддерживается)"
        raise OuoError(f"{e}{hint}") from e
    except requests.RequestException as e:
        raise OuoError(f"Ошибка сети: {type(e).__name__}: {e}") from e
    finally:
        session.close()
