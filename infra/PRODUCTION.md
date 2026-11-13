# PostgreSQL-backed deployment

`docker-compose.prod.yaml` — отдельный single-host стек `imager-temporal-prod`.
Он использует настоящий Temporal Server, PostgreSQL persistence и SQL visibility.
Dev-стек с SQLite остаётся в `docker-compose.dev.yaml`; его контейнеры, секреты и
каталоги данных production не использует.

## Подготовка и запуск

Нужны Docker с Linux containers, Python 3.12+ и установленный OpenSSL. На Windows
setup находит OpenSSL из Git for Windows; другой executable можно указать через
`--openssl`. Setup не скачивает образы и не запускает контейнеры.

```powershell
py -3.12 infra/setup.py --production
docker compose --env-file infra/.env.prod -f infra/docker-compose.prod.yaml up -d --build
```

Настройки путей, имён сертификатов, сроков их действия, retention и PostgreSQL
находятся в `[deployment.production]` файла `config/imager.toml`. При существующем
dev `.env` на E production получает собственный подкаталог `prod` внутри runtime
root; иначе используется portable TOML data root с тем же подкаталогом. Явный путь:

```powershell
py -3.12 infra/setup.py --production --data-root E:/IMagerBOT-1-runtime/data/prod
```

Setup создаёт игнорируемый Git файл `infra/.env.prod`, независимые случайные
MongoDB/MinIO/PostgreSQL credentials и TLS-материалы в `.local/secrets/prod`.
Повторный запуск сохраняет их. Введённый путь production проверяется на пересечение
с каталогами dev-сервисов. `--rotate-ui --prompt-ui-password` меняет только пароль UI.

Production UI опубликован на всех IPv4-интерфейсах сервера: откройте
`http://SERVER_IP:18088/` в браузере. Порт задаётся `TEMPORAL_UI_PORT` в
`infra/.env.prod`; если он изменён, подставьте его в адрес. Логин и пароль UI
записаны в `.local/secrets/prod/ui-credentials.json`.
UI, его HTTP API и статические файлы закрыты nginx BasicAuth. Только `/healthz`
доступен без пароля и возвращает `ok`. MinIO Console остаётся на
`http://127.0.0.1:19001`; прямые порты Temporal, PostgreSQL, MongoDB, MinIO API
и внутреннего UI Server не опубликованы. Dev UI остаётся на loopback.

Бот включается отдельно через `--profile bot`. Один Telegram token должен иметь
один polling bot: остановите dev-бота перед включением production-профиля.

## Порядок инициализации

1. PostgreSQL создаёт две базы и отдельную роль Temporal без superuser/createdb.
2. `temporal-schema` из matching `admin-tools` выполняет `setup-schema -v 0.0` и
   `update-schema` отдельно для persistence и visibility. Скрипт не использует
   `--overwrite` и не удаляет существующие таблицы. Ошибка блокирует старт сервера.
3. Temporal Server запускает frontend/history/matching/worker в одном контейнере.
4. `temporal-namespace` из того же core image подключается через общий Python SDK
   TLS helper и создаёт namespace при отсутствии.
5. После успешной инициализации запускаются приложение и отдельный UI Server.

Проверки и остановка:

```powershell
docker compose --env-file infra/.env.prod -f infra/docker-compose.prod.yaml ps -a
docker compose --env-file infra/.env.prod -f infra/docker-compose.prod.yaml logs temporal-schema temporal-namespace
docker compose --env-file infra/.env.prod -f infra/docker-compose.prod.yaml run --rm --no-deps temporal-namespace
curl.exe -I http://127.0.0.1:18088/
docker compose --env-file infra/.env.prod -f infra/docker-compose.prod.yaml down
```

Без credentials ожидается HTTP 401. `down` сохраняет bind data. Не используйте
глобальный prune или удаление каталогов других проектов.

Namespace bootstrap, dispatcher и worker используют один Python SDK TLS helper
и одинаковые `TEMPORAL_TLS_*` настройки. `admin-tools` используется только для
SQL schema. UI Server использует собственные `TEMPORAL_TLS_CA/CERT/KEY`.

## TLS и доступ

Одна локальная CA подписывает сервер Temporal, клиент приложения/UI и PostgreSQL.
Temporal требует клиентский сертификат на frontend и internode соединениях;
клиенты проверяют CA и DNS name `temporal`. Persistence и schema tool подключаются
к PostgreSQL с проверкой CA и DNS name `postgres`; HBA отвергает plaintext TCP.
PostgreSQL получает собственный leaf certificate. CA private key в контейнеры
не монтируется. Секреты подключены по отдельным файлам, не всем каталогом.

Leaf certificates действуют год, CA — десять лет по умолчанию. Setup проверяет
срок, CA chain, DNS name и соответствие private keys существующих сертификатов,
сохраняет действующий комплект и отказывается
незаметно заменять неполный. Перед истечением срока подготовьте новый совместимый
комплект и выполните согласованный restart клиентов и сервисов. Храните резервную
копию CA, ключей и `.env.prod` отдельно от данных; потеря секретов не должна
приводить к генерации новых паролей поверх существующей базы.

На Linux сервисы Temporal/UI/admin-tools используют UID/GID пользователя setup,
чтобы читать owner-only private keys. На Docker Desktop используется UID/GID 1000.
PostgreSQL копирует свой ключ в ephemeral `/run`, выставляет владельца postgres и
mode 0600: это устраняет несовместимость проверки key permissions с NTFS bind.

## Границы single-host

Это persistent single-host deployment, без HA и автоматического failover.
Сертификат CA даёт доверенному клиенту доступ к Temporal API; отдельного RBAC/
identity provider здесь нет. Публичный UI использует HTTP с nginx BasicAuth;
описанный выше TLS защищает внутренние подключения к Temporal и PostgreSQL.
MongoDB работает в закрытой Docker-сети с password authentication. Доступ
приложений к MinIO проходит через отдельный gateway, описанный ниже.

Сумма memory limits долгоживущих сервисов с одним render worker, одним
catalog-import-worker, MinIO gateway, локальным Telegram Bot API и ботом — 5.176 GB, без учёта
Docker/OS. Дополнительный
worker каждого типа требует ещё до 1 GB. Импорт ZIP использует отдельную
Temporal queue и собственный scratch-каталог. Ротация контейнерных логов
ограничена 10 MB × 3. [Импорт паков через бота](PACK_IMPORT.md).

Для эксплуатации нужны резервные копии PostgreSQL **обеих баз**, MongoDB, MinIO и
секретов с проверкой восстановления, контроль свободного диска и срока сертификатов.
Не меняйте `history_shards` после создания persistence. Перед upgrade сохраните
backup, используйте matching версии server/admin-tools и выполняйте поддерживаемую
последовательность обновления Temporal. Локальная CA не заменяет backup или HA.

## Ограничение уязвимых операций Community MinIO

Собираемый `RELEASE.2025-10-15T17-29-55Z` остаётся затронутым опубликованными
advisories. `minio-gateway` применяет proxy workarounds для используемых IMager
операций. Это **mitigation, а не исправленный бинарник или отсутствие CVE**;
переход на другой продукт или лицензию этим изменением не выполняется.

В обоих compose-файлах настоящий сервис `minio` подключён только к отдельной
`internal` сети `minio-backend`. К ней подключён также gateway, обращающийся к
`minio-storage:9000`. В основной сети имя `minio:9000` принадлежит gateway:
настройки SDK остаются прежними, прямого маршрута к backend у приложений нет.
Gateway использует тот же nginx image, что и Temporal UI, с лимитом 64 MB.
S3-порт на хост не публикуется; MinIO Console сохраняет прежний loopback-порт.

Файл `nginx/minio-gateway.conf`:

* Блокирует unsigned-trailer marker в `X-Amz-Content-Sha256`, включая другой
  регистр, окружающие пробелы и повторяющиеся значения заголовка. Это workaround
  для [query-credential signature bypass](https://github.com/minio/minio/security/advisories/GHSA-hv4r-mvr4-25vw)
  и [Snowball auto-extract bypass](https://github.com/minio/minio/security/advisories/GHSA-9c4q-hq6p-c237).
* Запрещает POST с query-параметрами `select` или `select-type` независимо от
  порядка, регистра и однократного percent-encoding имени. Это отключает
  [уязвимый S3 Select](https://github.com/minio/minio/security/advisories/GHSA-h749-fxx7-pwpg),
  который IMager не использует.
* Удаляет шесть replication-заголовков, которые
  [pinned source преобразует во внутренние метаданные](https://github.com/minio/minio/blob/RELEASE.2025-10-15T17-29-55Z/cmd/handler-utils.go#L99).
  Параметры query `X-Minio-Replication-*` также запрещены: MinIO извлекает
  метаданные и из query/form. Browser-form загрузки (`multipart/form-data`,
  `application/x-www-form-urlencoded`) отключены. Это ограничивает
  [SSE metadata injection](https://github.com/minio/minio/security/advisories/GHSA-3rh2-v3gr-35p9).
  Multipart через SDK использует обычные POST/PUT и продолжает поддерживаться.

Остальные заголовки, Host с портом, исходный URI и query проходят без изменения
для SigV4. Request/response buffering отключён: большие ZIP не копируются в
временные файлы nginx. Query и авторизация не записываются в access log.

Защита действует только для трафика через gateway. Console и доступ владельца
Docker/хоста считаются административными; дополнительные сети, публикация API
backend или прямое подключение другого клиента обходят эти ограничения.
Репликация, browser-form upload и S3 Select здесь намеренно не поддерживаются.
Новые advisories, другие операции и base-image CVE нужно проверять отдельно.
При применении изменения к существующему стеку надо пересоздать его MinIO и
клиентские контейнеры, чтобы прежний прямой network route исчез.

`imager/tests/infrastructure/test_minio_gateway.py` проверяет compose topology
через `docker compose config` без запуска сервисов. Опциональные HTTP-тесты
работают только с отдельным stub upstream с маркером `isolated-stub`:
`IMAGER_MINIO_GATEWAY_TEST_ALLOW=isolated-stub` и
`IMAGER_MINIO_GATEWAY_TEST_URL=http://<isolated-gateway>:9000`.
Тот же файл в режиме `--serve-upstream` запускает только тестовый upstream.
Перед развёртыванием нужны `nginx -t`, эти HTTP-проверки и обычный MinIO SDK
roundtrip через gateway в отдельном fixture, включая multipart upload.

## Хранение загруженных фото и результатов

`[retention]` в `config/imager.toml` задаёт общий для приложения лимит:
`source_count = 10`, `result_count = 10`. Это два независимых списка принятых
исходников и опубликованных результатов, а не лимит на пользователя. Исходники
сортируются по времени приёма; результаты — по времени завершения.

Фоновый цикл существующего dispatcher удаляет старые объекты небольшими пачками.
Паковые изображения, manifests, ZIP импорта, данные Local Bot API и базы не входят
в эту очистку. Запрос и ссылка результата остаются неизменными для аудита;
`artifact_lifecycle.source/result` содержит состояние очистки и `deleted_at`.
Ошибка удаления логируется и повторяется позже; повтор после падения процесса
безопасен, если объект уже отсутствует. Повторная генерация или отправка из-за
очистки не запускается.

Незавершённые расчёты сохраняют исходники. Telegram-доставка в состояниях
`pending/preparing/claimed` сохраняет результат до завершения отправки. Поэтому
во время работы файлов временно может быть больше десяти. Общие ссылки разных
заданий и неизвестные старые layouts консервативно сохраняются; очистка не
угадывает владельца и не удаляет префикс целиком. Перед удалением MinIO сверяет
размер и SHA-256 metadata с принятой ссылкой.

Политика охватывает канонические ссылки из принятых core jobs. Неопубликованные
загрузки, abandoned attempt outputs и другие объекты без записи в core inbox
не удаляются автоматически: загрузка бота может ещё ожидать публикации из его
outbox. Для orphan cleanup нужен отдельный согласованный grace/pin protocol.
Проверочный запуск `ImageRetention(...).cleanup_once(dry_run=True)` возвращает
число кандидатов без изменения Mongo или MinIO.

## Проверенные официальные источники

Используются [Temporal Server 1.32.2](https://github.com/temporalio/temporal/releases/tag/v1.32.2),
matching `admin-tools`, [UI 2.58.1](https://github.com/temporalio/ui/releases/tag/v2.58.1)
и [PostgreSQL 16.15](https://www.postgresql.org/docs/16/release-16-15.html).
`auto-setup` не используется: [официальное руководство](https://docs.temporal.io/self-hosted-guide/deployment)
предписывает отдельно управлять SQL schema при использовании `temporalio/server`.
Конфигурация опирается на [service configuration](https://docs.temporal.io/references/service-configuration),
[UI TLS settings](https://docs.temporal.io/references/web-ui-environment-variables)
и [SQL tool 1.32.2](https://github.com/temporalio/temporal/blob/v1.32.2/tools/sql/main.go).
