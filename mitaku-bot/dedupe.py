"""dedupe.py — нормализация текста и поиск дубликатов постов (один и тот же сет на разных сайтах).

Приоритет — точность: ложно «пропущенный» уникальный сет хуже, чем пропущенный дубликат. Пост B
считается дубликатом поста A, если совпадает косплеер (после нормализации) и:
- одинаковый набор слов в названии (без ника, числа фото, служебных слов и знаков), а число фото
  близко (разница не больше 15 %, но не меньше двух), либо
- названия почти совпадают (сходство не ниже NEAR: опечатки, транслитерация) и число фото одинаково.
Номера частей/томов («Set 2») должны совпадать. Без косплеера — только полное совпадение названия
и числа фото. Одного сходства «Eve Xmas» и «Eve Nier» недостаточно: это разные сеты.
"""
import re
import unicodedata
from difflib import SequenceMatcher

NEAR = 0.93
MIN_PHOTOS = 5
# служебные слова в названиях, которые не отличают один сет от другого
STOP = {"cosplay", "cosplayer", "photo", "photos", "pic", "pics", "image", "images", "set", "video",
        "videos", "nude", "nsfw", "leak", "leaks", "onlyfans", "patreon", "fansly", "hd", "4k",
        "new", "free", "download", "mega", "pack", "and", "the", "of", "in", "a"}
_COUNT_RE = re.compile(r"\b\d+\s*(?:photos?|pics?|images?|videos?|mb|gb|p|x)\b", re.I)


def norm_text(text):
    """Нижний регистр, NFKC, схлопнутые пробелы (для поиска и сравнения)."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", text).strip()


def norm_name(text):
    """Ник/имя без знаков и регистра: «Rumi_Peach!» -> «rumi peach»."""
    text = re.sub(r"[^\w\s]|_", " ", norm_text(text))
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(title, cosplayer=""):
    t = norm_text(title)
    t = _COUNT_RE.sub(" ", t)
    cos = norm_name(cosplayer)
    t = re.sub(r"[^\w\s]|_", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if cos and t.startswith(cos):
        t = t[len(cos):].strip()
    return [w for w in t.split() if w not in STOP]


def norm_title(title, cosplayer=""):
    return " ".join(title_tokens(title, cosplayer))


def similarity(a, b):
    """Сходство нормализованных названий 0..1: максимум из посимвольного и по набору слов."""
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(SequenceMatcher(None, a, b).ratio(), jaccard)


def is_duplicate(a, b):
    """a, b — dict со столбцами ncos, ntitle, nphotos. Возвращает (дубликат?, сходство)."""
    ncos_a, ncos_b = a.get("ncos") or "", b.get("ncos") or ""
    ta, tb = a.get("ntitle") or "", b.get("ntitle") or ""
    score = similarity(ta, tb)
    # номер части/тома («Set 2», «Vol 3») отличает один сет от другого, как бы ни были похожи названия
    nums = lambda t: {w for w in t.split() if w.isdigit()}
    if nums(ta) != nums(tb):
        return False, score
    pa, pb = int(a.get("nphotos") or 0), int(b.get("nphotos") or 0)
    same_count = pa >= MIN_PHOTOS and pa == pb
    close_count = (pa and pb and abs(pa - pb) <= max(2, round(0.15 * max(pa, pb)))) or not (pa or pb)
    same_words = bool(ta) and set(ta.split()) == set(tb.split())
    if ncos_a and ncos_a == ncos_b:
        return (same_words and bool(close_count)) or (score >= NEAR and same_count), score
    if not ncos_a and not ncos_b:   # ника нет у обоих: только полное совпадение названия и фото
        return same_words and same_count, score
    return False, score
