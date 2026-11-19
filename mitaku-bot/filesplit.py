"""filesplit.py — нарезка большого файла на ZIP-части меньше лимита Telegram (50 МБ).

Каждая часть — обычный ZIP (без сжатия: архивы и медиа уже сжаты) с одним файлом внутри
`<имя>.001`, `<имя>.002`, ... Части распаковываются в одну папку и склеиваются:
Windows `copy /b "имя.0*" "имя"`, Linux/macOS `cat имя.0* > имя`, либо открыть `имя.001` в 7-Zip.
"""
import math
import re
import zipfile
from pathlib import Path

MB = 1024 * 1024
CHUNK = 1024 * 1024


def safe_part_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:120] or "file"


def part_count(size, part_size):
    return max(1, math.ceil(size / part_size))


def join_hint(name):
    """HTML-подсказка, как склеить части. name уже должен быть экранирован для HTML."""
    return (
        "Файл больше лимита Telegram, поэтому он разделён на ZIP-части. "
        "Распакуйте все части в одну папку, затем:\n"
        f"• Windows: <code>copy /b \"{name}.0*\" \"{name}\"</code>\n"
        f"• Linux/macOS: <code>cat \"{name}\".0* &gt; \"{name}\"</code>\n"
        f"• или откройте <code>{name}.001</code> в 7-Zip — он склеит части сам."
    )


def iter_zip_parts(path, display_name, out_dir, part_size):
    """Генератор (номер, всего, путь_к_zip). Части создаются по одной: отправьте и удалите
    каждую, прежде чем брать следующую, — так на диске не лежит вторая копия файла."""
    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    display_name = safe_part_name(display_name)
    total = part_count(path.stat().st_size, part_size)
    with path.open("rb") as source:
        for index in range(1, total + 1):
            zip_path = out_dir / f"{safe_part_name(Path(display_name).stem)}_{index:03d}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
                with archive.open(f"{display_name}.{index:03d}", "w", force_zip64=True) as target:
                    left = part_size
                    while left > 0:
                        chunk = source.read(min(CHUNK, left))
                        if not chunk:
                            break
                        target.write(chunk)
                        left -= len(chunk)
            yield index, total, zip_path
