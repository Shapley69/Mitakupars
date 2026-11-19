"""exports.py — выгрузка списков в CSV (UTF-8 с BOM — Excel открывает кириллицу без танцев)."""
import csv
import io

HISTORY_COLUMNS = [("slug", "slug"), ("title", "Название"), ("cosplayer", "Косплеер"), ("site", "Сайт"),
                   ("status", "Статус"), ("status_label", "Статус (текст)"), ("detail", "Подробности"),
                   ("photos_sent", "Фото отправлено"), ("photos_total", "Фото всего"),
                   ("msg_id", "ID поста в канале"), ("first_at", "Первая запись"),
                   ("updated_at", "Обновлено"), ("url", "Ссылка на пост")]
SEARCH_COLUMNS = [("q", "Запрос"), ("field_label", "Поле"), ("hits", "Сколько раз искали"),
                  ("found", "Найдено в библиотеке"), ("first_at", "Первый раз"),
                  ("last_at", "Последний раз")]
LIBRARY_COLUMNS = [("slug", "slug"), ("title", "Название"), ("cosplayer", "Косплеер"),
                   ("character", "Персонаж"), ("source", "Игра/аниме"), ("site", "Сайт"),
                   ("status", "Статус"), ("published", "Опубликовано на сайте"), ("tags", "Теги"),
                   ("nphotos", "Фото"), ("url", "Ссылка")]
SUBS_COLUMNS = [("kind_label", "Тип"), ("value", "Значение"), ("hits", "Срабатываний"),
                ("created_at", "Создана"), ("last_hit_at", "Последнее срабатывание")]


def safe_cell(value):
    """Значение для CSV без «формульной инъекции»: ячейка, начинающаяся с = + - @, станет текстом."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(rows, columns):
    """rows — список dict, columns — [(ключ, заголовок)]. Возвращает bytes (UTF-8 с BOM, CRLF)."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([title for _, title in columns])
    for row in rows:
        writer.writerow([safe_cell(row.get(key)) for key, _ in columns])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")
