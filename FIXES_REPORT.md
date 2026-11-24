# Отчёт: технические проблемы и их решения

Дата: 2026-11-24. Ветка: `codex/temporal-minio` (изменения не закоммичены).

Исправлены технические ошибки, найденные при ревью: утечки диска и данных,
списание квоты за сбои системы, хрупкий импорт. Сценарии использования, порядок
шагов в боте и зависимость домена от OpenCV не менялись намеренно.

## Сводка

| # | Проблема | Последствие | Решение |
|---|---|---|---|
| 1 | Кэш Local Bot API не очищается | Диск заполняется копиями всех фото и ZIP (до 256 МБ каждый) | Удаление сразу после передачи в MinIO + периодический janitor |
| 2 | ZIP импорта хранятся в MinIO вечно | +до 256 МБ на каждый импорт навсегда | Удаление архива после завершения импорта |
| 3 | Исходник непринятой заявки остаётся в MinIO | Объекты без владельца, которых не видит retention | Немедленное удаление + janitor бота с grace-периодом |
| 4 | Результат попытки, потерявшей lease, остаётся в MinIO | То же | Удаление самой попыткой + `StorageHygiene` в dispatcher |
| 5 | Квота не возвращается при сбое генерации | Пользователь теряет генерации из-за ошибок системы | Идемпотентный возврат единицы квоты |
| 6 | Один посторонний/битый файл отклоняет весь ZIP | Не импортируется даже штатный `dota_icons` (файл `htt`) | Пропуск с отчётом, опознание файлов без расширения по сигнатуре |
| 7 | Коллекции Mongo растут без предела | `generation_jobs`, `bot_admissions`, `generation_history` | Очистка старых документов и TTL-индексы |
| 8 | Временные каталоги после OOM/SIGKILL не удаляются | Scratch-пути — bind mount, переживают рестарт | Очистка устаревших каталогов при старте процесса |

---

## 1. Кэш Local Bot API

**Проблема.** `telegram-bot-api --local` сохраняет каждый файл, скачанный через
`getFile`, в `IMAGER_BOT_API_DATA_PATH` и никогда его не удаляет. Бот монтировал
этот каталог только на чтение, поэтому никто не мог его очищать. Каждое фото
пользователя и каждый ZIP набора оставались на диске навсегда.

**Решение.**
- После успешной загрузки в MinIO бот удаляет файл из кэша: фото в
  `TelegramImageSource.__call__`, ZIP в `_upload_archive_from_telegram`.
  Удаление best-effort: ошибка не ломает запрос (байты уже в MinIO), файл
  подберёт janitor.
- `StorageJanitor` раз в `bot.janitor_poll_seconds` (600 с) удаляет файлы старше
  `bot.telegram_cache_max_age_seconds` (1 ч). Удаляются только файлы в
  медиа-каталогах `<token>/{photos,documents,stickers,...}`. Базы TDLib
  (`td.binlog` и т.п.) и неизвестные файлы не трогаются.
- Монтирование кэша в боте сделано доступным на запись (`docker-compose.*.yaml`).
- Защита: путь обязан лежать внутри каталога кэша и в медиа-подкаталоге.
  Возраст должен быть больше таймаута скачивания архива, это проверяется при
  загрузке конфига.

**Файлы.** `imager_contracts/scratch.py` (новый),
`telegram_bot/src/infrastructure/services/telegram_files.py`,
`telegram_bot/src/infrastructure/services/storage_janitor.py` (новый),
`telegram_bot/src/commands/image_generation/upload.py`,
`telegram_bot/src/commands/admin_panel/pack_import/handlers.py`,
`infra/docker-compose.dev.yaml`, `infra/docker-compose.prod.yaml`.

## 2. ZIP импорта навсегда в MinIO

**Проблема.** Retention явно защищал префикс `catalog/imports/`, а удалять
архив после импорта никто не умел. Каждый импорт оставлял архив до 256 МБ.

**Решение.** `StorageHygiene.cleanup_import_archives` (в dispatcher ядра)
выбирает импорты в терминальном статусе (`completed`/`failed`) старше
`retention.import_archive_grace_seconds` (600 с). Ключ проверяется на
принадлежность импорту (`catalog/imports/{import_id}/`), объект удаляется с
проверкой SHA-256, состояние записывается в `archive_lifecycle`. Архив вне
layout помечается `unmanaged` и не удаляется. При ошибке MinIO — `pending`,
повтор в следующем проходе. Изображения набора и manifest не затрагиваются.

**Файлы.** `imager/src/infrastructure/jobs/storage_hygiene.py` (новый),
`imager/src/presentation/temporal/dispatcher.py`,
`imager/src/infrastructure/object_storage/minio_store.py` (`list_objects`,
`reference_for_key`).

## 3. Исходники непринятых заявок

**Проблема.** Бот загружает исходник в MinIO *до* записи заявки. Если после
загрузки заявку отклоняли (квота, очередь, выключенная библиотека) или процесс
падал, объект оставался без документа. Retention ядра ходит только по
документам и такие объекты не видит.

**Решение.**
- `SubmitGeneration.execute`: при любой определённой ошибке после загрузки
  исходник удаляется, если по этому `job_id` нет ни admission, ни core job. Ключ
  детерминирован (`uploads/{job_id}/...`), поэтому повтор того же сообщения не
  может потерять чужой исходник. При `AdmissionUncertain` объект сохраняется:
  заявка могла записаться.
- `StorageJanitor.sweep_orphan_sources` удаляет исходники старше
  `bot.orphan_source_grace_seconds` (6 ч) без admission и без core job (остатки
  после падения процесса).
- При подсчёте дневной квоты для legacy-дня (`count_user_jobs_between`)
  неудачные задания больше не учитываются.

**Файлы.** `telegram_bot/src/application/generation.py`,
`telegram_bot/src/infrastructure/services/object_storage/minio_storage.py`,
`telegram_bot/src/infrastructure/services/imager_service/imager_service.py`,
`telegram_bot/src/infrastructure/services/storage_janitor.py`.

## 4. Результаты потерянных попыток

**Проблема.** Ключ результата содержит токен попытки
(`jobs/{job}/attempts/{token}/result.jpg`). Если попытка загрузила результат, но
потеряла lease (`LostExecution` в `complete`) или процесс упал между загрузкой и
публикацией, на объект не ссылался ни один документ.

**Решение.**
- `RenderActivities.render_image`: если после загрузки публикация не удалась,
  попытка удаляет свой объект. Исключение — объект уже стал каноническим
  `result` задания. Если документ прочитать не удалось, объект оставляется
  для фоновой очистки.
- `StorageHygiene.cleanup_orphan_results` удаляет объекты старше
  `retention.orphan_result_grace_seconds` (6 ч), только когда задание завершено
  и его `result` указывает на другой ключ, либо документа нет. Объекты идущих
  заданий не трогаются.

**Файлы.** `imager/src/presentation/temporal/activities.py`,
`imager/src/infrastructure/jobs/storage_hygiene.py`.

## 5. Квота за неудачные генерации

**Проблема.** Счётчик `used` в дневной корзине только увеличивался. Падение
исполнителя, ошибка декодирования или таймаут стоили пользователю одной из
пяти генераций.

**Решение.** `AdmissionRepository.refund(job_id)`: атомарный `$inc: -1` вместе
с флагом `admissions.$.refunded`, поэтому повторы и конкурирующие обработчики
не вернут единицу дважды. Для админских (exempt) корзин флаг ставится на
корзину, и она перестаёт учитываться в `_exempt_count`. Вызов встроен в
`repair_history_once` нотификатора: он обрабатывает каждое завершённое
Telegram-задание и повторяется до успеха, так что возврат не теряется при
сбое Mongo.

**Файлы.** `telegram_bot/src/infrastructure/data/repositories/admission_repository.py`,
`telegram_bot/src/infrastructure/services/imager_service/notifier.py`,
`telegram_bot/main.py`.

## 6. Хрупкий импорт ZIP

**Проблема.** Любая запись с неразрешённым расширением (`readme.txt`, файл без
расширения), пустая запись или одно битое изображение отклоняли весь архив.
Штатный набор `dota_icons` не импортировался из-за одного файла `htt` (JPEG без
расширения). `.webp` вообще не допускался.

**Решение.**
- Распаковка (`extract_catalog_archive`): посторонние и пустые записи
  пропускаются и учитываются в отчёте. Записи без расширения извлекаются и
  опознаются по сигнатуре (PNG/JPEG/BMP/TIFF/WEBP), остальные удаляются.
  Добавлен `.webp`. Архив без единого изображения по-прежнему отклоняется. Все
  структурные проверки безопасности (пути, ссылки, шифрование, CRC, zip-бомбы,
  лимиты) не изменились и по-прежнему отклоняют архив целиком.
- Проверка изображений (`_local_group_plan(skip_invalid=True)`): битое или
  слишком большое изображение пропускается. CLI-импорт из каталога сохраняет
  прежнее строгое поведение.
- Отчёт (`skipped_files`, `duplicate_files`, до 20 примеров с *исходными*
  именами из архива) записывается в документ импорта с fencing-токеном и
  показывается в статусе импорта в боте.

**Файлы.** `imager/src/application/catalog_import/archive.py`,
`imager/src/application/catalog_import/executor.py`,
`imager/src/infrastructure/object_storage/catalog.py`,
`imager/src/infrastructure/catalog_import/mongo_imports.py` (`record_report`),
`telegram_bot/src/commands/admin_panel/pack_import/handlers.py`,
`config/imager.toml`.

## 7. Рост коллекций MongoDB

**Проблема.** `generation_jobs`, `bot_admissions` (каждая запись хранит полную
копию заявки) и `generation_history` не имели ни TTL, ни очистки.

**Решение.**
- `StorageHygiene.purge_job_documents` удаляет терминальные задания старше
  `retention.job_document_days` (30). Условия: исходник и результат уже удалены
  (или их нет) и доставка в Telegram не выполняется.
- TTL-индекс `admission_bucket_ttl` по `created_at`
  (`bot.admission_retention_days` = 90). Это далеко за пределами окна повторной
  доставки апдейтов Telegram (24 ч), поэтому защита от дублей не ослабевает.
  Тест, который закреплял «квитанции никогда не истекают», заменён тестом на
  ограниченный срок хранения.
- TTL-индекс `generation_history_ttl` (`bot.history_retention_days` = 180).

## 8. Временные каталоги после аварийного завершения

**Проблема.** `TemporaryDirectory` не чистится при OOM-kill или SIGKILL, а
scratch-пути исполнителей и бота — bind mount, который переживает рестарт.

**Решение.** При старте render/import-исполнителей (`purge_core_scratch`) и в
janitor бота удаляются собственные временные каталоги (по известным префиксам)
старше `storage.scratch_max_age_seconds` (2 ч). При загрузке конфига
проверяется, что этот срок больше самого долгого activity: реплики делят один
mount, и нельзя удалить каталог живой попытки соседа.

---

## Новые параметры `config/imager.toml`

| Параметр | Значение | Назначение |
|---|---|---|
| `storage.scratch_max_age_seconds` | 7200 | Возраст остатков во временных каталогах |
| `retention.import_archive_grace_seconds` | 600 | Задержка удаления ZIP после импорта |
| `retention.orphan_result_grace_seconds` | 21600 | Grace для результатов без ссылки |
| `retention.job_document_days` | 30 | Срок хранения завершённых job-документов |
| `retention.hygiene_poll_seconds` | 600 | Период `StorageHygiene` |
| `bot.telegram_cache_max_age_seconds` | 3600 | Возраст файлов в кэше Bot API |
| `bot.janitor_poll_seconds` | 600 | Период janitor бота |
| `bot.orphan_source_grace_seconds` | 21600 | Grace для исходников без заявки |
| `bot.admission_retention_days` | 90 | TTL дневных корзин квоты |
| `bot.history_retention_days` | 180 | TTL проекции истории |
| `catalog.allowed_extensions` | + `.webp` | |

## Проверка

- Офлайн-тесты (`tests/run_offline.py`, зависимости из
  `requirements-windows-py312.txt`): ядро **333 passed** (было 321), бот
  **390 passed** (было 374). Новые тесты: `imager/tests/infrastructure/jobs/test_storage_hygiene.py`,
  `telegram_bot/tests/test_storage_janitor.py`, дополнения в тестах распаковки,
  импорта и отправки генерации.
- Интеграционно на реальных Mongo 7 и MinIO (одноразовые контейнеры):
  - `telegram_bot/tests/integration/smoke_quota_refund.py`: 5 генераций →
    отказ шестой → возврат → повторный возврат не срабатывает → шестая проходит;
    exempt-корзина; TTL-индекс.
  - `imager/tests/integration/smoke_storage_hygiene.py`: удалены архив
    завершённого импорта, результат потерянной попытки, результат без документа,
    два старых документа. Остались архив идущего импорта, объект каталога,
    канонический результат, результат идущего рендера и недоставленные
    документы. Второй проход ничего не удаляет; в grace-период ничего не удаляется.
- Импорт реального `dota_icons` + `README.txt` + битый `broken.png`: `htt`
  опознан как JPEG и импортирован, `README.txt` и `broken.png` пропущены с
  отчётом, в каталоге 1203 уникальных изображения.

Запуск smoke-проверок:

```bash
python telegram_bot/tests/integration/smoke_quota_refund.py mongodb://127.0.0.1:27199
python imager/tests/integration/smoke_storage_hygiene.py mongodb://127.0.0.1:27199 127.0.0.1:9199 KEY SECRET
```

Не проверено: полный прогон в Docker Compose с настоящим Telegram и Local Bot
API. Удаление файлов из кэша Bot API проверено юнит-тестами на файловой
структуре, повторяющей его каталоги.

## Что намеренно не менялось

- **Сценарий, тексты и UX**: порядок «настройки → фото», доставка результата
  документом, однотипные тексты ошибок, ограничение пропорций 16:9,
  отсутствие удаления наборов из админки. По договорённости это вопросы
  сценариев, а не технические ошибки.
- **Зависимость домена от OpenCV** — оставлена по договорённости.
- **Многоступенчатый outbox (бот → inbox ядра → dispatcher → Temporal)** и
  доставка at-most-once — это архитектурные решения, а не ошибки. Их замена
  на активности Temporal — отдельная задача.
- **Legacy-код** (RabbitMQ, FastAPI API, загрузчик fonwall, `CellRepository` на
  файловой системе) — мёртвый, но покрыт тестами; удаление — отдельная
  уборка.
- **Устаревшие `.packages`**: `run_offline.py` без `IMAGER_TEST_DEPS` падает
  (нет `temporalio`, старый `pymongo`). Каталог в `.gitignore`, это локальное
  окружение, а не код; тесты нужно запускать на актуальных зависимостях.
