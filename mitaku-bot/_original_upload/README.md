# Mitaku Toolkit — парсер косплей-сайтов + Telegram-бот

Сайт содержит контент 18+. Используйте скачанное для личных целей, соблюдайте авторские права
и условия использования сайта.


## 0. Что нового (версия с несколькими сайтами)

| Что | Как |
|---|---|
| **Несколько сайтов** | mitaku, cosplaytele, galleryepic, xasiat 🧪, haneame 🧪. В боте: `/sites` (какие активны) и `/sync` (сайт → раздел → число страниц кнопками). В CLI: `--site cosplaytele --section cosplay-ero --pages 3` |
| **Файлы не хранятся** | По умолчанию `storage=meta`: в базу попадают только метаданные и ссылки на файлы. Когда нужен ZIP, фото или видео, бот скачивает их с сайта во временную папку (`mitaku_data/staging`), отправляет и сразу удаляет. Вернуть старое поведение: `/set storage full` или `--storage full` |
| **Канал** | Бот сам проверяет сайты, публикует новый пост (название, модель, персонаж, игра, теги) и в комментариях присылает фото, видео и ZIP. См. раздел 11 |
| **Большие файлы** | Локальный Bot API сервер поднимает лимит с 50 МБ до ~2 ГБ. См. раздел 12 |
| **Новый сайт** | Один класс в `mitaku_sites.py`. См. раздел 13 |

Файлы проекта: `mitaku_parser.py` (ядро), `mitaku_sites.py` (адаптеры сайтов), `mitaku_jobs.py`
(очередь, пауза), `mitaku_bot.py` (бот). Ключ поста в базе: для mitaku прежний slug, для остальных сайтов
с префиксом (`ct__casual`, `ge__cosplay-10776`, `xa__…`, `hn__…`).

## 1. Что внутри

| Файл | Назначение |
|---|---|
| `mitaku_parser.py` | Парсер (CLI **и** библиотека): скачивает фото/видео, пишет JSON-метаданные, ведёт базу, ищет |
| `mitaku_jobs.py` | `JobManager`: очередь задач, пауза/продолжить/стоп, настройки. Не зависит от Telegram |
| `mitaku_bot.py` | Готовый Telegram-бот на aiogram 3 поверх `JobManager` |
| `migrate_sqlite_to_postgres.py` | Одноразовый перенос локальной SQLite-библиотеки и настроек в Supabase Postgres |
| `requirements.txt`, `.env.example` | Зависимости и пример настроек |

Бот и парсер **не слиты**: парсер работает сам по себе, `JobManager` — тонкая прослойка,
бот — тонкая оболочка. Раздел 7 объясняет, как подключить всё к вашему боту.

## 2. Установка

```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Нужен Python 3.9+. Только парсер без бота: `pip install requests beautifulsoup4`.

## 3. Быстрый старт (без бота)

```bash
# первые 5 страниц главной -> папки
python mitaku_parser.py https://mitaku.net/ --pages 5

# то же, но каждый пост в свой zip
python mitaku_parser.py https://mitaku.net/ --pages 5 --zip

# запустили позже: уже скачанное пропускается, берутся только новые/обновлённые посты
python mitaku_parser.py https://mitaku.net/ --pages 5

# регулярное обновление: идти вглубь, пока не встретится страница, где всё уже известно
python mitaku_parser.py https://mitaku.net/ --pages 50 --until-known

# категория / тег / один пост
python mitaku_parser.py https://mitaku.net/category/ero-cosplay/ --pages 2-4
python mitaku_parser.py https://mitaku.net/ero-cosplay/ely-marciana-marine-study-nikke/

# что найдётся — без скачивания
python mitaku_parser.py <url> --dry-run

# поиск по уже скачанной библиотеке
python mitaku_parser.py --search "hana bunny"
python mitaku_parser.py --search nikke --field game       # all|cosplayer|game|character|tag
python mitaku_parser.py --stats

# поиск на самом сайте
python mitaku_parser.py --site-search byoru
```

Остальные флаги: `--out папка`, `--images-only`, `--videos-only`, `--workers 4`, `--delay 1.0`,
`--limit N`, `--keep-cover`, `--force`, `--recheck`.

## 4. Как работает «не качать заново»

Всё состояние лежит в `mitaku_data/library.db` (SQLite). Для каждого поста хранится:

- **сигнатура листинга** — заголовок + даты публикации/обновления из карточки на странице списка;
- **отпечаток содержимого** — хэш списка найденных фото/видео, ссылок на архивы и блока описания;
- **список скачанных файлов** и статус `done` / `partial`.

Алгоритм при повторном запуске:

1. Страница листинга загружается всегда (это 1 запрос на страницу).
2. Если пост `done` и сигнатура не изменилась — он **пропускается без запроса к самому посту**.
3. Если сигнатура изменилась (пост обновили) — страница поста открывается, сравнивается отпечаток.
   Не изменился — просто обновляется сигнатура. Изменился — **докачиваются только новые файлы**
   (в zip-режиме они дописываются в существующий архив).
4. Посты со статусом `partial` (остановили, оборвалась сеть, часть файлов не скачалась)
   дорабатываются при следующем запуске автоматически.
5. Если папку или zip удалили руками, пост будет скачан заново.

Флаги: `--force` — перекачать всё; `--recheck` — не доверять сигнатуре, открывать каждый пост
и сверять отпечаток; `--until-known` — остановить обход, когда страница листинга целиком известна.

> Не переключайте `--zip` на лету внутри одной библиотеки: пост запоминает, как он сохранён.
> Для другого режима используйте другую папку `--out`.

## 5. Структура данных

```
mitaku_data/
├── library.db                      # база: посты, статусы, настройки, последняя задача
└── posts/
    ├── Ely – Marciana Marine Study (NIKKE)/     # режим папок
    │   ├── meta.json
    │   ├── Ely-Marciana-...-1.jpg
    │   └── ...
    ├── Byoru – Remielle.zip                     # режим --zip
    └── Byoru – Remielle.json                    # JSON лежит рядом с zip
```

Пример `meta.json`:

```json
{
  "slug": "ely-marciana-marine-study-nikke",
  "url": "https://mitaku.net/ero-cosplay/ely-marciana-marine-study-nikke/",
  "title": "Ely – Marciana Marine Study (NIKKE)",
  "section": "ero-cosplay",
  "categories": ["Ero Cosplay"],
  "tags": ["Ely", "Marciana", "NIKKE"],
  "cosplayer": "Ely",
  "character": "Marciana",
  "source": "NIKKE",
  "published": "2026-11-04T14:00:00+00:00",
  "modified": "2026-11-06T09:00:00+00:00",
  "declared": {"content": "49 Pics", "photos": 49, "videos": null,
               "resolution": "", "file_size": "168 MB", "duration": ""},
  "formats": {"photo": {"jpg": 49}, "video": {}},
  "archives": ["https://ouo.io/8ODDFbc"],
  "password": "mitaku.net",
  "files": [{"name": "...-1.jpg", "kind": "photo", "size_bytes": 412345, "url": "..."}],
  "total_bytes": 20123456,
  "status": "done"
}
```

`declared` — то, что написал автор поста («49 Pics», «168 MB»); `files`/`total_bytes` — то, что
реально скачано. Поле `source` — игра/аниме/фильм (ключи `Game`/`Anime`/`Movie`/`Series`).

## 6. Архивы (Mediafire / Pixeldrain)

Ссылки на архивы на сайте ведут через ouo.io с капчей. Парсер капчу **не обходит**: ссылки
сохраняются в `meta.json` (`archives`), дальше нужен один ручной шаг.

```bash
# 1) выгрузить ссылки на архивы всех постов (или найденных по --search)
python mitaku_parser.py --export-links links_todo.txt
python mitaku_parser.py --search nikke --export-links links_todo.txt

# 2) открыть ссылки в браузере, пройти капчу, итоговые ссылки Mediafire/Pixeldrain
#    сложить в links.txt (по одной в строке)

# 3) скачать
python mitaku_parser.py --archives links.txt          # -> mitaku_data/archives/
```

Поддерживается: файлы Mediafire, файлы и списки Pixeldrain, прямые ссылки. Папки Mediafire — нет.
Архивы запаролены (пароль в `meta.json`, обычно `mitaku.net`). Распаковывайте через 7-Zip:
`7z x -pmitaku.net файл`. Стандартный `zipfile` Python не поддерживает шифрование AES.

## 7. Telegram-бот

### 7.1 Настройка

1. Создайте бота у [@BotFather](https://t.me/BotFather), получите токен.
2. Узнайте свой Telegram ID (например, у @userinfobot).
3. Скопируйте `.env.example` в `.env` и заполните:
   ```
   BOT_TOKEN=123456:ABC...
   ADMIN_IDS=111111111
   MITAKU_OUT=mitaku_data
   # Необязательно: Cookie-заголовок браузера, если ouo.io блокирует загрузчик
   OUO_COOKIE=
   ```
4. Запуск: `python mitaku_bot.py`

Бот отвечает **только** пользователям из `ADMIN_IDS` (контент 18+, управление загрузками).
Без `ADMIN_IDS` он не стартует.
Администратор может прислать боту ссылку `ouo.io` отдельным сообщением: бот попробует скачать
файл и отправить его в чат. При блокировке сайта можно задать `OUO_COOKIE` через окружение
или `.env`; обращайтесь с этой cookie как с секретом. Лимит размера соответствует текущей
конфигурации Telegram Bot API (обычный или локальный сервер).

### 7.2 Render Free и Supabase Postgres

При заданном `DATABASE_URL` база и настройки хранятся в Supabase Postgres, а не в локальном
файле Render. Без этой переменной бот продолжает использовать SQLite локально.

1. Создайте проект Supabase и скопируйте строку подключения **Session pooler** в разделе
   **Connect**. Для Render используйте pooler-строку, предназначенную для IPv4, и добавьте
   `sslmode=require`, если параметр отсутствует. Не публикуйте пароль и не коммитьте `.env`.
2. Остановите локально запущенного бота. Установите зависимости:
   `python -m pip install -r requirements.txt`.
3. Добавьте в локальный `.env`:
   ```
   DATABASE_URL=postgresql://...
   ```
4. Перенесите локальную SQLite-базу:
   `python migrate_sqlite_to_postgres.py`.
   Скрипт создаёт таблицы, переносит посты с их ID и все настройки `kv`. Если в таблице `posts`
   Supabase уже есть записи, скрипт остановится, чтобы не смешать данные. Исходная SQLite-база
   не изменяется. Проверьте итоговое число строк до запуска бота.
5. Создайте на Render **Web Service**, подключите GitHub-репозиторий и укажите:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python mitaku_bot.py`
   - тариф Free, без persistent disk
6. В Environment Variables Render задайте `BOT_TOKEN`, `ADMIN_IDS` и `DATABASE_URL` (тот же
   Session pooler URL). Секреты добавляйте через Dashboard, не в Git.
7. В настройках сервиса задайте Health Check Path `/health`. После деплоя проверьте
   `https://<имя-сервиса>.onrender.com/health`, затем команды `/stats` и `/list`.

Render Free может останавливать сервис после периода без входящего HTTP-трафика; при первом
запросе после остановки бот будет ждать пробуждения. Можно настроить внешний монитор на
проверку `/health` примерно раз в 5 минут, но это не гарантия постоянной работы и не замена
платному тарифу. Supabase Free ограничивает базу 500 МБ и может приостановить проект после
недели низкой активности; автоматические скачиваемые бэкапы базы в этом тарифе не включены.
Перед миграцией и периодически сохраняйте Postgres-дамп отдельно. Локальные HTML/медиафайлы
и папка `mitaku_data` в Postgres не переносятся; библиотека по-прежнему хранит их метаданные
и ссылки.

Без `DATABASE_URL` сохраняется локальный SQLite-режим и ранее описанные бэкапы `library.db`.

### 7.3 Команды

| Команда | Действие |
|---|---|
| `/sync [раздел] [N или A-B]` | Скачать N первых страниц (по умолчанию 5). Разделы: `all`, `ero-cosplay`, `sexy-set`, `online-video`, `nude`. Добавьте `force` или `recheck` |
| `/update [раздел]` | Только новое: идёт по страницам, пока не встретит известные |
| `/get <ссылка или slug>` | Скачать или перепроверить один пост |
| `/pause`, `/resume` | Пауза и продолжение (загрузки замирают на ближайшем куске) |
| `/stop` | Остановить, очистить очередь. Прогресс сохраняется |
| `/continue` | Продолжить незавершённую задачу, в том числе после перезапуска бота |
| `/status` | Страница, пост, файлы, счётчики, очередь |
| `/search <текст>` | Поиск по библиотеке. Обычное сообщение без `/` тоже ищет |
| `/cosplayer`, `/game`, `/character`, `/tag` | Поиск по конкретному полю |
| `/list [стр]`, `/stats` | Последние посты и статистика |
| `/links [текст]` | Файл со ссылками на архивы (см. раздел 6) |
| `/set [параметр значение]` | `zip on/off`, `images on/off`, `videos on/off`, `workers 1-16`, `delay сек` |

Поиск: несколько слов работают как «И» (`ely nikke`). Если в библиотеке ничего нет, бот сам
ищет на сайте и предлагает скачать найденные посты кнопками. В карточке поста есть кнопки
«Перепроверить», «meta.json», «Скачать ZIP» и «Выбрать файл»; если в библиотеке сохранено фото,
его превью, информация о посте и кнопки показываются в одном сообщении.
После загрузки отдельного поста бот присылает превью первой фотографии с автором и названием,
а также кнопки «Скачать ZIP» и «Выбрать файл». ZIP отправляются отдельными архивами меньше
лимита Telegram; если отдельный файл превышает лимит, он разделяется на части, а бот присылает
команду Windows для их объединения. Кнопки «Назад» возвращают из карточки к результатам,
а из выбора файлов — к карточке. При вызове новой команды бот удаляет свои предыдущие сообщения
в чате, в том числе сохранённые после перезапуска; сообщения пользователя не затрагиваются.
При переходе между страницами поиска или списка файлов сообщение обновляется на месте. Сообщение
о ходе задачи обновляется и удаляется после выдачи результата.

### 7.3 Автозапуск (Linux, systemd)

```ini
# /etc/systemd/system/mitaku-bot.service
[Unit]
Description=Mitaku bot
After=network-online.target

[Service]
WorkingDirectory=/home/user/mitaku
ExecStart=/home/user/mitaku/venv/bin/python mitaku_bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now mitaku-bot`. Если процесс убьют посреди загрузки, при старте бот
напишет «есть незавершённая задача», достаточно `/continue`.

## 8. Интеграция в ваш проект

### 8.1 Только парсер как библиотека

```python
from mitaku_parser import Scraper, Options, Control

ctl = Control()                                   # для паузы/остановки из другого потока
sc = Scraper("mitaku_data", Options(zip=False, workers=4, delay=1.0), ctl, log=print)

sc.sync(["https://mitaku.net/"], first=1, last=5)   # блокирующий вызов
print(sc.status())                                  # прогресс (можно читать из другого потока)

rows, total = sc.lib.search("byoru", field="cosplayer", limit=10)
items = sc.site_search("byoru")                    # [{url, title, sig}]
sc.sync([items[0]["url"]])                         # скачать найденный пост
```

`ctl.pause()`, `ctl.resume()`, `ctl.stop()` вызываются из любого потока. `sync()` блокирующий,
поэтому в асинхронном приложении запускайте его в потоке (`asyncio.to_thread`) или используйте
`JobManager`.

### 8.2 `JobManager` в любом Telegram-фреймворке

`JobManager` сам запускает рабочий поток и очередь, поэтому ваш бот только вызывает методы:

```python
from mitaku_jobs import JobManager

def on_event(job, kind, text):          # ВАЖНО: вызывается из рабочего потока
    ...                                 # kind: started | finished | stopped | failed

jm = JobManager("mitaku_data", on_event)

job = jm.submit(["https://mitaku.net/"], first=1, last=5, chat_id=chat_id)
jm.pause(); jm.resume(); jm.stop()
jm.status()                  # {"state": idle|running|paused, "job": ..., "queued": n, "progress": {...}}
jm.pending_job()             # последняя незавершённая задача или None
jm.resume_last(chat_id)      # перезапустить её
jm.set_setting("zip", "on"); jm.get_settings()
jm.lib.search("nikke", field="game", limit=8, offset=0)   # -> (rows, total)
jm.lib.get_by_id(5)          # запись поста (в т.ч. meta_json)
jm.scraper.site_search("byoru")                           # блокирующий, вызывайте в потоке
```

**Отправка уведомлений из `on_event` (поток -> бот):**

- aiogram: `asyncio.run_coroutine_threadsafe(bot.send_message(chat, text), loop)`, где
  `loop = asyncio.get_running_loop()` взят в `main()` (так сделано в `mitaku_bot.py`).
- python-telegram-bot v20+: то же самое, `asyncio.run_coroutine_threadsafe(application.bot.send_message(...), loop)`.
- pyTelegramBotAPI (telebot): синхронная, можно вызывать `bot.send_message(chat, text)` прямо из `on_event`.

**Мини-пример для telebot:**

```python
import telebot
from mitaku_jobs import JobManager

bot = telebot.TeleBot("TOKEN")
jm = JobManager("mitaku_data", lambda job, kind, text: bot.send_message(job["chat_id"], text))

@bot.message_handler(commands=["sync"])
def sync(m):
    job = jm.submit(["https://mitaku.net/"], 1, 5, chat_id=m.chat.id)
    bot.reply_to(m, f"В очереди, позиция {job['position']}")

@bot.message_handler(commands=["pause"])
def pause(m): jm.pause(); bot.reply_to(m, "Пауза")

bot.infinity_polling()
```

### 8.3 Добавить свою команду в `mitaku_bot.py`

Каждая команда — функция с декоратором. Пример `/random` (случайный пост из библиотеки; добавьте `import random` вверху файла):

```python
@router.message(Command("random"))
async def cmd_random(m: Message):
    rows, total = jm.lib.search("", None, 1, random.randrange(max(jm.lib.stats()["posts"], 1)))
    if rows:
        await m.answer(card(rows[0]))
```

Новое поле поиска: добавьте его в `SEARCH_FIELDS` в `mitaku_parser.py`
(`"series": ("source", "title")`) и сделайте команду по образцу `/game`.

Новые колонки в БД: добавьте в `SCHEMA` и `COLS`. Для уже существующей `library.db` выполните
`ALTER TABLE posts ADD COLUMN ...` (`CREATE TABLE IF NOT EXISTS` существующую таблицу не меняет).

### 8.4 Если сайт поменяет разметку

Все эвристики собраны наверху `mitaku_parser.py`: `CONTAINERS` (где искать контент),
`JUNK_SEL` (что выкидывать: сайдбар, виджеты), `BAD` (подстроки URL, которые пропускаются),
`KEY_GROUPS` (какие поля описания распознаются: `Cosplayer`, `Game`, `Anime`...).
Если в посте не нашлось медиа, а оно заявлено, парсер сам сохранит `mitaku_data/debug_<slug>.html`.
Откройте его и посмотрите, где лежат ссылки на изображения.

## 9. Если что-то не работает

| Симптом | Что делать |
|---|---|
| `403` / `503` / HTML вместо файла | Защита сайта. Увеличьте `--delay`, уменьшите `--workers`, попробуйте позже. Капчу парсер не обходит |
| Фото не находятся | Файл `debug_<slug>.html` (см. 8.4). Пришлите его, чтобы поправить селекторы |
| Поиск по сайту ничего не возвращает | Поиск использует стандартный WordPress `?s=`. Если сайт его отключил, остаётся локальный поиск |
| Пост `partial` | Часть файлов не скачалась (404 или сеть). Запустите `/continue` или `/get <slug>` |
| Хочу перекачать | `--force` или `/sync force` |
| Нужно удалить пост из библиотеки | Удалите папку/zip и строку в БД, либо `--force` для пересохранения |

## 10. Что проверено, а что нет

- Логика парсера (поиск медиа, инкрементальность, докачка, zip-дозапись, пауза/стоп/продолжение, поиск)
  проверена на локальном тестовом сервере, имитирующем структуру сайта.
- Структура сайта (WordPress, тема ColorMag, ленивая галерея) изучена по живым страницам, но
  массовых скачиваний на реальном сайте я не делал: имена атрибутов и поля описания могут
  отличаться в отдельных постах.
- Бот проверен на уровне логики команд с заглушкой aiogram; реальный запуск с Telegram вам нужно
  сделать самому. Если библиотека изменит API, смотрите `requirements.txt` (aiogram >= 3.7).
- Telegram не позволяет ботам отправлять файлы больше 50 МБ. По запросу бот упаковывает
  скачанные файлы в несколько ZIP-архивов подходящего размера; слишком большие файлы внутри
  архивов разделяются на части.


## 11. Публикация в Telegram-канал

**Как это выглядит:** бот публикует в канале пост с превью-фото и текстом (название, 👤 модель, 🎭 персонаж,
🎮 игра/аниме, счётчики фото и видео, размер, хэштеги, ссылка на источник). Под ним, в комментариях, присылает
альбомы фото (по 10 штук, сжатые до ≤10 МБ), видео и ZIP-архивы (части по ≤44 МБ, при локальном Bot API ~1,9 ГБ).

**Настройка (один раз):**
1. Создайте канал и **группу для обсуждения**, привяжите её к каналу (настройки канала → Обсуждение).
2. Сделайте бота **администратором канала** (право публиковать сообщения).
3. Сделайте бота **администратором группы обсуждения** (нужно, чтобы бот видел автопересылку поста в группу;
   вместо этого можно отключить Privacy mode у @BotFather: `/setprivacy` → Disable).
4. В `.env` укажите `CHANNEL_ID=@имя_канала` (или `-100…`).
5. В боте: `/channel` → проверка подключения → «🧪 Тест» → «▶️ Включить».

**Как работает автопубликация:** раз в `channel_interval` минут (по умолчанию 30) бот проверяет первые 3 страницы
каждого **активного** сайта (`/sites`). Первая проверка сайта только запоминает существующие посты (baseline) и
ничего не публикует, чтобы не залить канал старым. Дальше каждый **новый** пост публикуется автоматически.
Обновлённые старые посты не публикуются. Вручную: `/publish <slug или ссылка>` или кнопка «📣 В канал» в карточке.

**Настройки:** `/set channel on|off`, `channel_interval 30`, `channel_media photos|documents`
(documents — файлы без сжатия), `channel_zip on|off`, `channel_videos on|off`.

**Лимиты Telegram:** в группу можно слать около 20 сообщений в минуту, поэтому бот делает паузы
(`SEND_PAUSE` в `mitaku_bot.py`) и сам ждёт при ошибке flood limit. Пост со 100 фото и ZIP публикуется несколько минут.
Публикации идут по очереди, по одному посту.

## 12. Большие файлы (до ~2 ГБ вместо 50 МБ)

Облачный Bot API принимает от бота файлы до 50 МБ. Лимит снимает **локальный Bot API сервер**:
загрузка до 2000 МБ. Боты, которые шлют 2–3 ГБ, либо используют его, либо работают как пользовательский
аккаунт (Telethon/Pyrogram; до 4 ГБ при Telegram Premium), либо отдают ссылку на файл.

1. Получите `api_id` и `api_hash` на https://my.telegram.org.
2. Запустите сервер (официальный `telegram-bot-api`, режим `--local`):
   `telegram-bot-api --api-id=ID --api-hash=HASH --local --http-port=8081`
   (из исходников tdlib/telegram-bot-api или готовым Docker-образом, например `aiogram/telegram-bot-api`;
   переменные окружения образа смотрите в его описании).
3. **Один раз** выйдите с облачного сервера: `python mitaku_bot.py --logout`
   (после этого к облаку бот сможет вернуться только через ~10 минут).
4. В `.env`: `LOCAL_API_URL=http://localhost:8081`. Лимиты в боте переключатся сами.

Больше 2 ГБ: ZIP автоматически режется на части, видео крупнее лимита уходит ZIP-частями.

## 13. Режим «только метаданные»: что хранится

В `library.db` лежат поля поста, ссылки на все фото и видео (`files_json`) и ссылки на архивы. На диске
ничего нет, кроме временной папки `staging`, которая очищается после каждой выдачи и при запуске бота.
Минусы: каждая выдача ждёт скачивания; если сайт удалит пост или поменяет адреса файлов, получить его уже не выйдет.
Для постов, скачанных раньше в режиме full, ничего не меняется (файлы остаются как были).

## 14. Новый сайт

Создайте класс в `mitaku_sites.py` по образцу `CosplayTele`:

```python
class MySite(Site):
    key, title, host, prefix = "mysite", "MySite", "mysite.com", "ms__"
    BASE = "https://mysite.com"
    def sections(self): return {"all": self.base + "/"}
    def is_post_url(self, url): ...            # это пост?
    def parse_listing(self, html, page_url): ...  # [{"url","title","sig"}]; sig меняется при обновлении поста
    def parse_post(self, html, url, keep_cover=False):
        return make_post(title, images, videos, external_links, info, tags, categories)
```
Затем добавьте `MySite()` в кортеж `SITES` внизу файла. Всё остальное (база, очередь, бот, канал, поиск,
кнопки выбора сайта) подхватится само. Поддержка пагинации внутри поста: метод `extra_pages` (см. `Haneame`).
Если у сайта есть JSON API, переопределите `fetch_listing`/`search` (пример: `CosplayTele` берёт список через WordPress REST).

## 15. Проверено и не проверено (версия с несколькими сайтами)

| Часть | Статус |
|---|---|
| Ядро (инкрементальность, режим meta, переход meta→full, докачка, пауза/стоп) | Проверено на локальных тестовых серверах |
| mitaku | Структура изучена по живым страницам; массовое скачивание не запускалось |
| cosplaytele | Разбор поста проверен на HTML по образцу реальной страницы. REST API списка и поиска не проверялся на живом сайте (есть запасной разбор HTML) |
| galleryepic | Фото ищутся во всём HTML и в данных Next.js. На странице видно только 12 из 61 фото, остальные подгружаются кнопкой «More» через JS. Если в данных их нет, пост помечается ⚠️ «найдено N из M» и сохраняется `debug_<slug>.html` |
| xasiat 🧪, haneame 🧪 | **Не проверены**: при моей проверке оба сайта закрыли доступ защитой от ботов, поэтому адаптеры написаны по типовой структуре. Защиту от ботов парсер не обходит: при блокировке он остановится с понятной ошибкой (`SiteBlocked`). Если с вашего IP страницы открываются, запустите `python mitaku_parser.py --dump-html <ссылка на пост>` и `--dump-html <ссылка на список>`, пришлите файлы, и я подгоню селекторы |
| Бот | Логика команд, публикации в канал и очистки временных файлов проверена с заглушкой aiogram. Реальный запуск с Telegram (права бота, автопересылка в группу, лимиты) нужно проверить самому |
