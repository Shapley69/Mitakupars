"""
mitaku_sites.py — адаптеры сайтов для парсера.

Каждый сайт — класс Site с тем же набором методов:
  owns(url) / is_post_url(url)        — чей это адрес и пост ли это
  fetch_listing(session, base, n)      — карточки со страницы списка [{url,title,sig,site}] или None (конец)
  fetch_post(session, url)             — (post, raw_html); post = словарь как у mitaku_parser.parse_post
  search(session, query, pages)        — поиск на самом сайте
Остальное (база, очередь, бот, отправка в канал) от сайта не зависит.

Чтобы добавить сайт: унаследуйте Site, реализуйте is_post_url / parse_listing / parse_post
и добавьте экземпляр в SITES внизу файла (подробности — README, раздел «Новый сайт»).
"""
import html as htmllib
import json
import re
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

import mitaku_parser as mp


class SiteBlocked(Exception):
    """Сайт отдал страницу защиты от ботов (Cloudflare и т. п.). Защиту парсер не обходит."""


def check_blocked(r):
    head = r.text[:4000].lower() if "html" in r.headers.get("Content-Type", "") else ""
    if r.status_code in (403, 429, 503) or "just a moment" in head:
        raise SiteBlocked(f"{r.url}: сайт закрыт защитой от ботов (HTTP {r.status_code}). "
                          "Попробуйте позже, увеличьте delay или откройте страницу в браузере")


def make_post(title, images, videos, external, info, tags, categories,
              published="", modified="", warnings=None):
    """Единый формат поста, который понимает Scraper."""
    return {"title": title, "images": images, "videos": videos, "external": external,
            "info": info, "norm": mp.normalize_info(info, title), "tags": tags,
            "categories": categories, "published": published, "modified": modified,
            "warnings": warnings or []}


def merge_post(a, b):
    """Дополняет пост a медиа и ссылками со следующей страницы b."""
    seen = {mp.full_size(c[0]) for c in a["images"] + a["videos"]}
    for key in ("images", "videos"):
        for c in b[key]:
            k = mp.full_size(c[0])
            if k not in seen:
                seen.add(k)
                a[key].append(c)
    a["external"] = mp._uniq(a["external"] + b["external"])
    a["tags"] = mp._uniq(a["tags"] + b["tags"])


JUNK_URL_PARTS = ("thumb", "logo", "avatar", "icon", "banner", "sprite", "placeholder",
                  "loading", "blank", "pixel", "/ads/", "advert")
SOCIAL_AND_ADS = ("shorturl.", "ourdream", "bit.ly", "t.me/cosplaytele")


def clean_media(items):
    out = []
    for cands in items:
        if any(j in cands[0].lower() for j in JUNK_URL_PARTS):
            continue
        out.append(cands)
    return out


class Site:
    key = ""
    title = ""
    host = ""
    prefix = ""          # префикс slug в БД ("" только у mitaku — совместимость со старой базой)
    BASE = ""
    experimental = False

    def __init__(self, base=None):
        self.base = (base or self.BASE).rstrip("/")
        if base:
            self.host = urlsplit(base).netloc

    # ---- адреса
    def sections(self):
        return {"all": self.base + "/"}

    def owns(self, url):
        host = urlsplit(url).netloc.lower()
        return host == self.host or host.endswith("." + self.host)

    def is_post_url(self, url):
        raise NotImplementedError

    def canonical(self, url):
        return url

    def raw_slug(self, url):
        return urlsplit(url).path.strip("/").split("/")[-1]

    def section_of(self, url, post):
        """Раздел поста для колонки section (по умолчанию — первая категория)."""
        return (post.get("categories") or [self.key])[0]

    def slug(self, url):
        return self.prefix + self.raw_slug(url)

    def listing_url(self, base_url, n):
        return mp.listing_page_url(base_url, n)

    def search_url(self, query, n=1):
        return None

    def parse_listing(self, page_html, page_url):
        raise NotImplementedError

    def parse_post(self, page_html, url, keep_cover=False):
        raise NotImplementedError

    def extra_pages(self, page_html, url):
        return []

    def referer(self, url):
        return url

    # ---- сеть
    def _get(self, session, url, allow_404=False):
        r = session.get(url, timeout=30)
        if r.status_code == 404 and allow_404:
            return None
        check_blocked(r)
        r.raise_for_status()
        return mp.fix_enc(r)

    def _tag(self, items):
        for it in items:
            it["site"] = self.key
            it["url"] = self.canonical(it["url"])
        return items

    def fetch_listing(self, session, base_url, n):
        r = self._get(session, self.listing_url(base_url, n), allow_404=True)
        if r is None:
            return None
        return self._tag(self.parse_listing(r.text, r.url))

    def fetch_post(self, session, url, keep_cover=False):
        r = self._get(session, url)
        post = self.parse_post(r.text, url, keep_cover)
        for extra in self.extra_pages(r.text, url)[:60]:
            r2 = self._get(session, extra, allow_404=True)
            if r2 is not None:
                merge_post(post, self.parse_post(r2.text, extra, keep_cover))
        return post, r.text

    def search(self, session, query, pages=None):
        items, seen, n = [], set(), 1
        while n <= (pages or 10):
            url = self.search_url(query, n)
            if not url:
                break
            r = self._get(session, url, allow_404=True)
            if r is None:
                break
            found = [x for x in self.parse_listing(r.text, r.url) if x["url"] not in seen]
            if not found:
                break
            items += found
            seen.update(x["url"] for x in found)
            n += 1
        return self._tag(items)


# =========================================================================== Mitaku
class Mitaku(Site):
    key, title, host, prefix, BASE = "mitaku", "Mitaku", "mitaku.net", "", "https://mitaku.net"

    def __init__(self, base=None):
        super().__init__(base or mp.SITE)

    def section_of(self, url, post):
        parts = urlsplit(url).path.split("/")
        return parts[1] if len(parts) > 1 else "mitaku"

    def sections(self):
        b = self.base
        return {"all": b + "/", "ero-cosplay": b + "/category/ero-cosplay/",
                "sexy-set": b + "/category/sexy-set/", "online-video": b + "/category/online-video/",
                "nude": b + "/category/nude/"}

    def owns(self, url):
        return urlsplit(url).netloc.endswith(mp.SITE_HOST)

    def is_post_url(self, url):
        return mp.mitaku_is_post_url(url)

    def search_url(self, query, n=1):
        return mp.listing_page_url(f"{self.base}/?s={quote_plus(query)}", n)

    def parse_listing(self, page_html, page_url):
        return mp.parse_listing(page_html, page_url)

    def parse_post(self, page_html, url, keep_cover=False):
        return mp.parse_post(page_html, url, keep_cover)


# =========================================================================== CosplayTele
class CosplayTele(Site):
    key, title, host, prefix = "cosplaytele", "CosplayTele", "cosplaytele.com", "ct__"
    BASE = "https://cosplaytele.com"
    NOT_POST = {"explore-categories", "best-cosplayer", "top-search", "24-hours", "3-day",
                "7-day", "video-cosplay", "about-us", "contact", "dmca", "privacy-policy",
                "terms-of-service", "feed", "sitemap"}
    CATS = {"cosplay-ero": "cosplay-ero", "cosplay-nude": "cosplay-nudee", "cosplay": "cosplay",
            "free-style": "free-style", "video": "video-cosplayyy"}
    MARKERS = ("Recommend For You", "Related Posts", "You may also like")

    def __init__(self, base=None):
        super().__init__(base)
        self._cat_ids = {}

    def sections(self):
        s = {"all": self.base + "/"}
        s.update({k: f"{self.base}/category/{v}/" for k, v in self.CATS.items()})
        return s

    def is_post_url(self, url):
        if not self.owns(url):
            return False
        path = urlsplit(url).path.strip("/")
        return bool(path) and "/" not in path and path not in self.NOT_POST \
            and not path.startswith(("wp-", "feed", "tag", "author", "category", "page"))

    def search_url(self, query, n=1):
        return mp.listing_page_url(f"{self.base}/?s={quote_plus(query)}", n)

    # --- листинг: сначала REST API WordPress (чисто и с датой обновления), потом HTML
    def _rest(self, session, path, params):
        r = session.get(self.base + "/wp-json/wp/v2/" + path, params=params, timeout=30)
        if r.status_code == 400:
            return None  # страница за пределами списка
        check_blocked(r)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("REST API вернул не список")
        return data

    def _cat_id(self, session, slug):
        if slug not in self._cat_ids:
            data = self._rest(session, "categories", {"slug": slug, "_fields": "id"})
            if not data:
                raise ValueError("категория не найдена")
            self._cat_ids[slug] = data[0]["id"]
        return self._cat_ids[slug]

    @staticmethod
    def _rest_items(data):
        return [{"url": p["link"], "title": htmllib.unescape(p["title"]["rendered"]),
                 "sig": f"{htmllib.unescape(p['title']['rendered'])}|{p.get('modified', '')}"}
                for p in data]

    def fetch_listing(self, session, base_url, n):
        m = re.search(r"/category/([^/]+)/?", urlsplit(base_url).path)
        try:
            params = {"per_page": 20, "page": n, "_fields": "id,link,title,modified"}
            if m:
                params["categories"] = self._cat_id(session, m.group(1))
            data = self._rest(session, "posts", params)
            return None if data is None else self._tag(self._rest_items(data))
        except SiteBlocked:
            raise
        except Exception:  # noqa: BLE001  — REST закрыт: разбираем HTML
            return super().fetch_listing(session, base_url, n)

    def search(self, session, query, pages=None):
        items, n = [], 1
        try:
            while n <= (pages or 5):
                data = self._rest(session, "posts", {"search": query, "per_page": 20, "page": n,
                                                     "_fields": "id,link,title,modified"})
                if not data:
                    break
                items += self._rest_items(data)
                n += 1
            return self._tag(items)
        except SiteBlocked:
            raise
        except Exception:  # noqa: BLE001
            return super().search(session, query, pages)

    def parse_listing(self, page_html, page_url):
        soup = BeautifulSoup(page_html, "html.parser")
        for junk in soup.select("aside, .widget, #secondary, .sidebar, footer, nav, header"):
            junk.decompose()
        items, seen = [], set()
        for a in soup.select("article h2 a, article h3 a, article h5 a, h5.post-title a, "
                             ".post-title a, .entry-title a"):
            u = urljoin(page_url, a.get("href", "")).split("#")[0]
            if self.is_post_url(u) and u not in seen:
                seen.add(u)
                title = a.get_text(strip=True)
                items.append({"url": u, "title": title, "sig": title})
        return items

    def parse_post(self, page_html, url, keep_cover=False):
        soup = BeautifulSoup(page_html, "html.parser")
        h1 = soup.select_one("h1.entry-title, h1")
        title = h1.get_text(strip=True) if h1 else ""
        scope = soup.select_one("article") or soup
        cats = mp._uniq(a.get_text(strip=True) for a in scope.select("a[href*='/category/']"))
        tags = mp._uniq(a.get_text(strip=True) for a in scope.select("a[href*='/tag/']"))
        pub = soup.select_one("meta[property='article:published_time']")
        mod = soup.select_one("meta[property='article:modified_time']")
        node = soup.select_one(".entry-content") or scope
        raw = str(node)
        for marker in self.MARKERS:  # дальше идут «рекомендуемые» — их фото чужие
            i = raw.find(marker)
            if i > 0:
                raw = raw[:i]
        cut = BeautifulSoup(raw, "html.parser")
        images, videos = mp.scan(raw, url, keep_cover)
        info = mp.parse_info(cut.get_text("\n", strip=True))
        ext = [u for u in mp.external_links(cut, url)
               if not self.owns(u) and not any(b in u for b in SOCIAL_AND_ADS)]
        post = make_post(title, clean_media(images), clean_media(videos), ext, info,
                         mp._uniq(cats + tags), cats,
                         pub.get("content") if pub else "", mod.get("content") if mod else "")
        want = post["norm"]["declared"]["photos"]
        if want and len(images) < want * 0.8:
            post["warnings"].append(f"найдено {len(images)} фото из заявленных {want}")
        return post


# =========================================================================== GalleryEpic
class GalleryEpic(Site):
    key, title, host, prefix = "galleryepic", "GalleryEpic", "galleryepic.com", "ge__"
    BASE = "https://galleryepic.com"
    POST_RE = re.compile(r"^/(?:[a-z]{2}/)?(cosplay|album)/(\d+)/?$")
    IMG_RE = re.compile(r"https?://static\.galleryepic\.[a-z]+/image/(?!avatar/)([0-9a-f-]{30,40})")
    LABELS = ("cosplayer", "character", "series", "contents", "published", "model", "parody")

    def sections(self):
        return {"all": f"{self.base}/en/cosplays/1", "albums": f"{self.base}/en/albums/1"}

    def is_post_url(self, url):
        return self.owns(url) and bool(self.POST_RE.match(urlsplit(url).path))

    def raw_slug(self, url):
        m = self.POST_RE.match(urlsplit(url).path)
        return f"{m.group(1)}-{m.group(2)}" if m else super().raw_slug(url)

    def listing_url(self, base_url, n):
        s = urlsplit(base_url)
        path = re.sub(r"/\d+/?$", "", s.path).rstrip("/") + f"/{n}"
        return urlunsplit(s._replace(path=path))

    def parse_listing(self, page_html, page_url):
        soup = BeautifulSoup(page_html, "html.parser")
        items, seen = [], set()
        for a in soup.select("a[href]"):
            u = urljoin(page_url, a["href"]).split("#")[0]
            if not self.is_post_url(u) or u in seen:
                continue
            seen.add(u)
            img = a.select_one("img[alt]")
            title = a.get_text(" ", strip=True) or (img.get("alt") if img else "") or u
            items.append({"url": u, "title": title, "sig": title})
        return items

    def parse_post(self, page_html, url, keep_cover=False):
        soup = BeautifulSoup(page_html, "html.parser")
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
        text = page_html.replace("\\/", "/")
        urls = mp._uniq(m.group(0) for m in self.IMG_RE.finditer(text))
        raw_id = self.raw_slug(url)
        images = [[f"{u}#name={raw_id}-{i:03d}.jpg"] for i, u in enumerate(urls, 1)]
        lines = [x.strip() for x in soup.get_text("\n", strip=True).splitlines() if x.strip()]
        info = {}
        for i, line in enumerate(lines[:-1]):
            if line.lower() in self.LABELS and line.lower() not in info:
                info[line.lower()] = lines[i + 1]
        content = info.get("contents", "")
        size = re.search(r"(\d+(?:\.\d+)?\s*[KMG]B)", content, re.I)
        if size:
            info["file size"] = size.group(1)
        if info.get("series"):
            info["game"] = info["series"]
        info["content"] = content
        tags = mp._uniq([info.get("cosplayer", ""), info.get("character", ""),
                         info.get("series", "")])
        post = make_post(title, images, [], [], info, tags, [], info.get("published", ""), "")
        want = post["norm"]["declared"]["photos"]
        if want and len(images) < want:
            post["warnings"].append(
                f"найдено {len(images)} фото из {want}: остальные сайт подгружает кнопкой More "
                "(через JavaScript) — пришлите debug-файл, подберу способ")
        return post


# =========================================================================== Xasiat (экспериментальный)
class Xasiat(Site):
    key, title, host, prefix = "xasiat", "Xasiat 🧪", "xasiat.com", "xa__"
    BASE = "https://www.xasiat.com"
    experimental = True
    POST_RE = re.compile(r"^/albums/(\d+)/([^/]+)/?$")
    IMG_RE = re.compile(r"(?:https?:)?//[^\s\"'<>()\\]+?/get_image/[^\s\"'<>()\\]+?\.(?:jpe?g|png|webp)/?"
                        r"|/get_image/[^\s\"'<>()\\]+?\.(?:jpe?g|png|webp)/?", re.I)

    def sections(self):
        return {"cosplay": f"{self.base}/albums/categories/cosplay/", "all": f"{self.base}/albums/"}

    def is_post_url(self, url):
        return self.owns(url) and bool(self.POST_RE.match(urlsplit(url).path))

    def raw_slug(self, url):
        m = self.POST_RE.match(urlsplit(url).path)
        return f"{m.group(1)}-{m.group(2)}" if m else super().raw_slug(url)

    def listing_url(self, base_url, n):
        if n == 1:
            return base_url
        s = urlsplit(base_url)
        path = re.sub(r"/\d+/?$", "", s.path).rstrip("/") + f"/{n}/"
        return urlunsplit(s._replace(path=path))

    def search_url(self, query, n=1):
        return self.listing_url(f"{self.base}/search/{quote_plus(query)}/", n)

    def parse_listing(self, page_html, page_url):
        soup = BeautifulSoup(page_html, "html.parser")
        items, seen = [], set()
        for a in soup.select("a[href]"):
            u = urljoin(page_url, a["href"]).split("#")[0]
            if not self.is_post_url(u) or u in seen:
                continue
            seen.add(u)
            title = a.get("title") or a.get_text(" ", strip=True) or \
                (a.select_one("img[alt]") or {}).get("alt", "") or u
            items.append({"url": u, "title": title.strip(), "sig": title.strip()})
        return items

    def parse_post(self, page_html, url, keep_cover=False):
        soup = BeautifulSoup(page_html, "html.parser")
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
        models = mp._uniq(a.get_text(strip=True) for a in soup.select("a[href*='/models/']"))
        tags = mp._uniq(a.get_text(strip=True) for a in soup.select("a[href*='/tags/']"))
        cats = mp._uniq(a.get_text(strip=True) for a in soup.select("a[href*='/categories/']"))
        for junk in soup.select("aside, footer, nav, .related, .list-albums"):
            junk.decompose()
        raw = str(soup).replace("\\/", "/")
        found = [urljoin(url, htmllib.unescape(m.group(0))) for m in self.IMG_RE.finditer(raw)]
        generic, videos = mp.scan(raw, url, keep_cover)
        images = [[u] for u in mp._uniq(found)] or clean_media(generic)
        info = {"cosplayer": models[0]} if models else {}
        post = make_post(title, images, clean_media(videos), [], info, mp._uniq(models + tags),
                         cats)
        if not images:
            post["warnings"].append("фото не найдены (сайт мог отдать страницу защиты)")
        return post


# =========================================================================== Haneame (экспериментальный)
class Haneame(Site):
    key, title, host, prefix = "haneame", "Haneame 🧪", "haneame.net", "hn__"
    BASE = "https://haneame.net"
    experimental = True
    POST_RE = re.compile(r"^/albumset/([^/]+)/?(?:\d+/?)?$")

    def sections(self):
        return {"all": self.base + "/", "albums": f"{self.base}/albumset/"}

    def is_post_url(self, url):
        return self.owns(url) and bool(self.POST_RE.match(urlsplit(url).path))

    def raw_slug(self, url):
        m = self.POST_RE.match(urlsplit(url).path)
        return m.group(1) if m else super().raw_slug(url)

    def canonical(self, url):
        m = self.POST_RE.match(urlsplit(url).path)
        return f"{self.base}/albumset/{m.group(1)}/" if m else url

    def search_url(self, query, n=1):
        return mp.listing_page_url(f"{self.base}/?s={quote_plus(query)}", n)

    def parse_listing(self, page_html, page_url):
        soup = BeautifulSoup(page_html, "html.parser")
        items, seen = [], set()
        for a in soup.select("a[href]"):
            u = self.canonical(urljoin(page_url, a["href"]).split("#")[0])
            if not self.is_post_url(u) or u in seen:
                continue
            seen.add(u)
            title = a.get("title") or a.get_text(" ", strip=True) or \
                (a.select_one("img[alt]") or {}).get("alt", "") or u
            items.append({"url": u, "title": title.strip(), "sig": title.strip()})
        return items

    def extra_pages(self, page_html, url):
        """Альбом разбит на страницы /albumset/<slug>/<n>/ — забираем все."""
        soup = BeautifulSoup(page_html, "html.parser")
        slug = self.raw_slug(url)
        pages = set()
        for a in soup.select("a[href]"):
            m = re.match(rf"^/albumset/{re.escape(slug)}/(\d+)/?$",
                         urlsplit(urljoin(url, a["href"])).path)
            if m:
                pages.add(int(m.group(1)))
        current = re.search(r"/(\d+)/?$", urlsplit(url).path)
        base = self.canonical(url)
        return [f"{base}{n}/" for n in sorted(pages) if n > 1 and not (current and int(current.group(1)) == n)]

    def parse_post(self, page_html, url, keep_cover=False):
        soup = BeautifulSoup(page_html, "html.parser")
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
        tags = mp._uniq(a.get_text(strip=True) for a in soup.select("a[rel='tag'], a[href*='/tag/']"))
        for junk in soup.select("aside, footer, nav, header, .sidebar, .related"):
            junk.decompose()
        node = soup.select_one("article, .entry-content, #content, main") or soup
        images, videos = mp.scan(str(node), url, keep_cover)
        post = make_post(title, clean_media(images), clean_media(videos), [],
                         mp.parse_info(node.get_text("\n", strip=True)), tags, [])
        if not post["images"]:
            post["warnings"].append("фото не найдены на странице")
        return post


# =========================================================================== реестр
SITES = {s.key: s for s in (Mitaku(), CosplayTele(), GalleryEpic(), Xasiat(), Haneame())}


def site_for_url(url):
    for s in SITES.values():
        if s.owns(url):
            return s
    return None


def is_post_url(url):
    s = site_for_url(url)
    return bool(s and s.is_post_url(url))


def slug_of(url):
    s = site_for_url(url)
    return s.slug(url) if s else urlsplit(url).path.strip("/").split("/")[-1]
