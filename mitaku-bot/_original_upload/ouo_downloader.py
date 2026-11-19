import os
import re
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
    "application/x-rar-compressed": ".rar",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "text/plain": ".txt",
}


class OuoError(Exception):
    pass


def extract_ouo_url(text):
    if not text:
        return None

    match = re.search(
        r"""https?://(?:www\.)?ouo\.io/[^\s<>"']+""",
        text,
        re.I,
    )
    if not match:
        return None

    return match.group(0).rstrip(".,;)]}")


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


def is_html_response(resp):
    ctype = resp.headers.get("Content-Type", "").lower()
    return "text/html" in ctype or "application/xhtml+xml" in ctype


def looks_like_challenge(html):
    low = html[:30000].lower()
    markers = (
        "just a moment",
        "cf-browser-verification",
        "challenge-platform",
        "enable javascript",
    )
    return any(marker in low for marker in markers)


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

    async def _request(self, session, method, url, data=None, referer=None):
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
            allow_redirects=True,
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
            completed = True
            return path, filename, size
        finally:
            tmp.close()
            if not completed:
                path.unlink(missing_ok=True)

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
