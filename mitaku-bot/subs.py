"""subs.py — подписки на новые посты: ник косплеера, игра/аниме, персонаж, тег или любой текст."""
from dedupe import norm_name, norm_text

KINDS = {
    "cosplayer": "👤 косплеер",
    "game": "🎮 игра/аниме",
    "character": "🎭 персонаж",
    "tag": "🏷 тег",
    "any": "🔎 любой текст",
}
ALIASES = {"косплеер": "cosplayer", "ник": "cosplayer", "игра": "game", "аниме": "game",
           "персонаж": "character", "тег": "tag", "текст": "any", "любой": "any"}


def parse_kind(word):
    """'game' / 'игра' -> 'game'; незнакомое слово -> None."""
    word = (word or "").strip().lower()
    return word if word in KINDS else ALIASES.get(word)


def normalize_value(kind, value):
    return norm_text(value) if kind in ("any", "tag") else norm_name(value)


def matches(sub, rec):
    """Подходит ли пост rec (строка posts) под подписку sub (строка subscriptions)."""
    needle = sub["nvalue"]
    if not needle:
        return False
    kind = sub["kind"]
    if kind == "cosplayer":
        ncos = rec.get("ncos") or norm_name(rec.get("cosplayer"))
        return needle == ncos or (len(needle) >= 4 and needle in ncos)
    if kind == "game":
        return needle in norm_text(rec.get("source")) or needle in norm_text(rec.get("title"))
    if kind == "character":
        return needle in norm_text(rec.get("character")) or needle in norm_text(rec.get("title"))
    if kind == "tag":
        tags = [norm_text(t) for t in (rec.get("tags") or "").split(",")]
        return any(needle == t or needle in t for t in tags if t)
    return needle in (rec.get("search_text") or norm_text(rec.get("title")))
