# Магазин доступа к Telegram-каналам за Stars

Python 3.12+, aiogram 3, FastAPI, PostgreSQL 16, Redis 7, SQLAlchemy async, Alembic. Интерфейс покупателя и администратора работает в личном чате Telegram. Frontend и Mini App отсутствуют.

Оплата новых покупок выполняется через внутренний баланс: пополнение Telegram Stars → разовая покупка доступа на срок или навсегда. Автопродление отключено. Бесплатные тарифы сохранены. Подробности и миграция старых подписок: [docs/WALLET.md](docs/WALLET.md). Право доступа создаётся в PostgreSQL; временное приглашение само по себе права не даёт. Полный отчёт: [docs/REPORT.md](docs/REPORT.md). Проверки: [SECURITY.md](SECURITY.md).

## Архитектура

```mermaid
flowchart LR
  T[Telegram] --> N[Nginx HTTPS]
  N --> W[FastAPI webhook + secret]
  W --> D[aiogram dispatcher]
  D --> S[Business services]
  S --> P[(PostgreSQL)]
  S --> R[(Redis rate limits)]
  S --> A[Telegram Bot API]
  J[Async worker] --> P
  J --> A
```

Transport находится в `app/api` и `app/bot/handlers`; business/data layer — в `app/services`. `Runtime` связывает сервисы с БД, Redis и Bot. Админку для браузера можно добавить как ещё один transport с отдельной аутентификацией, вызывающий те же сервисы.

В БД 15 таблиц (включая wallet_entries и wallet_allocations): `users`, `channels`, `products`, `plans`, `orders`, `payments`, `entitlements`, `grants`, `invite_links`, `audit_logs`, `update_receipts`, `refund_requests`, `payment_reversals`. Подробности и ограничения — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## До запуска

1. Создайте бота через официальный [@BotFather](https://t.me/BotFather), команда `/newbot`. Сохраните токен только в `.env` или secret storage.
2. Через `/setcommands` задайте команды:
   ```text
   start - Магазин
   terms - Условия
   support - Поддержка
   paysupport - Поддержка платежей
   admin - Администрирование
   ```
3. Узнайте собственный числовой Telegram ID доверенным способом и задайте `ADMIN_TELEGRAM_IDS=[123456789]`. Username не используется для авторизации.
4. Подготовьте реальные условия продажи, правила возврата и контакт поддержки. Значения из примера — заготовки, не юридические условия вашего магазина.
5. Создайте закрытый канал без публичного username. Добавьте бота администратором с **can_invite_users** и **can_restrict_members**. Права публикации, изменения информации, назначения администраторов, управления видеочатом не нужны.
6. Уберите старые общие ссылки. Другие администраторы не должны вручную принимать заявки в обход магазина. Бот не может запретить владельцу канала самостоятельно добавить человека.

## Конфигурация

```sh
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Запустите генератор отдельно для `WEBHOOK_PATH`, `WEBHOOK_SECRET` и каждого пароля. Для удобного включения в connection URL используйте URL-safe значения. Не используйте токен бота как путь webhook.

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | Токен BotFather |
| `WEBHOOK_BASE_URL` | Домен с HTTPS без пути, например `https://bot.example.org` |
| `WEBHOOK_PATH` | Независимый случайный URL-safe идентификатор 32–128 символов |
| `WEBHOOK_SECRET` | Независимый случайный секрет заголовка 32–256 символов |
| `DATABASE_URL` | `postgresql+asyncpg://shop_app:ПАРОЛЬ@postgres:5432/shop` |
| `MIGRATION_DATABASE_URL` | Аналогично, пользователь `shop_migrator` |
| `APP_DB_PASSWORD` | Тот же пароль, что в DATABASE_URL |
| `MIGRATOR_DB_PASSWORD` | Тот же пароль, что в MIGRATION_DATABASE_URL |
| `POSTGRES_PASSWORD` | Отдельный пароль bootstrap/superuser, только для postgres service |
| `REDIS_PASSWORD` | Пароль Redis |
| `REDIS_URL` | `redis://:ПАРОЛЬ@redis:6379/0` |
| `ADMIN_TELEGRAM_IDS` | JSON-массив положительных числовых ID |
| `SUPPORT_CONTACT` | Контакт реальной поддержки |
| `TERMS_URL` | HTTPS-адрес опубликованных условий |
| `TERMS_TEXT` | Краткие условия, показываемые ботом |
| `APP_ENV` | `production`, `development` или `test` |
| `LOG_LEVEL` | По умолчанию INFO |
| `EXPIRE_INTERVAL` | Поиск истёкших доступов, секунды, default 60 |
| `CLEANUP_INTERVAL` | Очистка приглашений, default 300 |
| `RECONCILIATION_INTERVAL` | Повторная проверка, default 7200 |
| `INVITE_TTL` | 60–600 секунд, default 600 |
| `ORDER_TTL` | Действие invoice до pre-checkout, default 1800 |

Приложение проверяет обязательную конфигурацию на старте. Runtime app/worker не получают bootstrap-пароль PostgreSQL и миграционный URL из Compose.

## Production deployment

Нужны Docker Engine с Compose, домен, TLS-сертификат, открытый порт 443 и исходящий доступ к Telegram API. Закрывайте прямой доступ извне к PostgreSQL, Redis и порту приложения. Compose не публикует эти порты.

Поместите сертификаты в `deploy/tls/fullchain.pem` и `deploy/tls/privkey.pem` на сервере. Каталог исключён из Git и Docker build context. Обновляйте сертификат средствами вашего ACME-клиента; после обновления выполняйте `docker compose exec nginx nginx -s reload`. Nginx не выпускает сертификаты автоматически. `TERMS_URL` должен указывать на уже опубликованную страницу: встроенный nginx обслуживает webhook, а не сайт условий.

```sh
docker compose build
docker compose up -d postgres redis
docker compose run --rm migrate
docker compose up -d app worker nginx
docker compose exec app python -m app.cli set-webhook
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/ready').status)"
docker compose ps
```

`migrate` — одноразовая команда `alembic upgrade head`. При использовании локального Python вместо migration service: `MIGRATION_DATABASE_URL=... alembic upgrade head`. Не запускайте миграции конкурентно. Инициализация ролей в `deploy/init-db.sh` выполняется только при **первом запуске пустого** volume PostgreSQL. Изменение переменных паролей у существующего volume само пароль в БД не меняет.

Webhook: `POST /telegram/webhook/<WEBHOOK_PATH>`, отдельный secret header, HTTPS на nginx. CLI устанавливает `allowed_updates`, включая `subscription`, и сохраняет ожидающие updates. Команда настройки webhook вызывает Telegram API; выполнять её нужно после готовности сервера. Runtime не меняет webhook при каждом перезапуске.

`GET /health` проверяет жизнь процесса. `GET /ready` проверяет PostgreSQL и Redis и доступен внутри сети Compose. Worker пишет heartbeat в Redis; его healthcheck не заменяет мониторинг ошибок отдельных задач.

Приложение работает UID 10001, без capabilities, с read-only файловой системой и writable `/tmp`. Ограничение конкурентности uvicorn — 32. Advisory locks используют прямые соединения к PostgreSQL: **PgBouncer в transaction pooling не поддерживается**. При масштабировании учитывайте отдельные соединения блокировок и размер пула, настройте `max_connections` и лимиты числа реплик.

## Локальная разработка

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.lock
```

Нужны отдельные PostgreSQL 16 и Redis 7, доступные с локальной машины. Создайте dev database и пользователя; никогда не используйте production БД для тестов. В `.env` задайте локальные connection URLs, `APP_ENV=development`, остальные обязательные значения.

```sh
alembic upgrade head
python -m app.cli poll
```

В другом терминале с тем же окружением:

```sh
python -m app.jobs.worker
```

Polling удаляет webhook без удаления ожидающих updates и запрещён при `APP_ENV=production`. Для локальной проверки webhook запустите `uvicorn app.main:create_app --factory --no-access-log`, обеспечьте внешний HTTPS-туннель и выполните `python -m app.cli set-webhook`. Не запускайте polling и webhook одновременно для одного бота.

## Первый продукт без изменения кода

Выполните `/start` с аккаунта из `ADMIN_TELEGRAM_IDS` → «Админ-панель» (или `/admin`).

1. **Каналы → Добавить**: отправьте ID канала или перешлите сообщение из него, затем введите название и подтвердите. Бот должен иметь права администратора канала.
2. **Продукты → Добавить**: выберите канал кнопкой, введите название и описание, отправьте фото обложки или нажмите «Без обложки». Выберите, показывать ли продукт, и подтвердите.
3. **Тарифы → Добавить**: выберите продукт, введите название тарифа, выберите его тип, цену и срок. Для бесплатного тарифа цена 0 устанавливается автоматически; для lifetime — навсегда. Автопродление отсутствует.
4. Для редактирования нажмите на запись в списке, затем на нужное поле. Остальные поля сохранятся.
5. **Доступы → Выдать доступ**: выберите пользователя и продукт, задайте срок или «Навсегда», подтвердите. Для продления/отзыва выберите существующий доступ.
6. **Платежи → нужная оплата → Вернуть Stars**: бот покажет сумму и запросит подтверждение.

На каждом шаге доступна «Отмена» или `/cancel`. Черновик хранится в Redis 30 минут. `/admin` открывает главное меню и сбрасывает текущий черновик. Старые кнопки формы не изменяют новую форму. Если другой администратор уже изменил запись, сохранение устаревшего черновика будет отклонено — откройте запись заново.

Списки показывают названия, а не JSON. Копировать UUID не требуется. Доступы и платежи показывают продукт и числовой ID пользователя. Обложку отправляйте фотографией на соответствующем шаге формы.

Для опытных пользователей прежние команды `/save`, `/grant`, `/refund` сохранены. Примеры ниже — альтернативный способ:

```text
/save channel new {"telegram_chat_id":-1001234567890,"title":"Мой закрытый канал"}
```

Бот проверит приватность канала и права. Скопируйте UUID канала из ответа:

```text
/save product new {"channel_id":"UUID-КАНАЛА","title":"Product A","description":"Описание контента","is_active":false}
/save plan new {"product_id":"UUID-ПРОДУКТА","name":"30 дней","billing_type":"fixed","price_stars":250,"duration_days":30}
/save plan new {"product_id":"UUID-ПРОДУКТА","name":"90 дней","billing_type":"fixed","price_stars":600,"duration_days":90}
/save plan new {"product_id":"UUID-ПРОДУКТА","name":"Навсегда","billing_type":"lifetime","price_stars":1500,"duration_days":null}
/save plan new {"product_id":"UUID-ПРОДУКТА","name":"Пробный доступ","billing_type":"free","price_stars":0,"duration_days":3}
/save product UUID-ПРОДУКТА {"channel_id":"UUID-КАНАЛА","title":"Product A","description":"Описание контента","is_active":true}
```

`UUID-*` замените полным реальным UUID. `/save` принимает **полный объект**, а не patch: неуказанные необязательные поля получают defaults. `/adminhelp` показывает справку. Команда не исполняет JSON как код, неизвестные поля запрещены.

Для обложки откройте продукт → «Обложка», отправьте фотографию и подтвердите. Допустимы `short_description`, `sort_order`, `is_active`. Скрытие продукта останавливает новые продажи, но сохраняет уже оплаченный доступ. `channel.is_active=false` также приостанавливает выдачу новых ссылок. Идентичность канала продукта и product_id тарифа неизменяемы: для другого канала создайте новый продукт, чтобы не переместить старые права доступа.

Бесплатная активация однократна для пары пользователь/тариф, включая период после истечения пробного доступа. Для новой акции создайте новый тариф. Цена, billing type и длительность старых заказов не меняются при редактировании тарифа.

## Доступы, платежи, возвраты и статистика

Для покупок с баланса используйте раздел «Покупки с баланса», для возврата Stars — «Пополнения / возвраты». Правила разделения возвратов и резервирования описаны в [WALLET.md](docs/WALLET.md).

```text
/grant {"telegram_user_id":123456789,"product_id":"UUID","days":30}
/grant {"telegram_user_id":123456789,"product_id":"UUID","days":null}
/grant {"telegram_user_id":123456789,"product_id":"UUID","revoke":true}
/list payment 0
/refund UUID-ПЛАТЕЖА
/stats
/list audit 0
```

Для ручной выдачи пользователь сначала должен выполнить `/start`. `days=30` продлевает остаток, `days=null` выдаёт навсегда. Отзыв инвалидирует существующие grants; последующая новая покупка может создать новый доступ.

Возврат требует нажатия кнопки подтверждения. Запрос сохраняется в PostgreSQL; worker вызывает Telegram refund API и повторяет действие после сбоев. Проверяйте `payment.status`, audit log и события worker. Возврат удаляет вклад конкретного платежа; независимые покупки сохраняются. Если других оснований нет, entitlement становится `refunded`, затем worker удаляет пользователя и отзывает ссылки. Для recurring worker также отменяет дальнейшие списания.

Повторная фактическая оплата уже оплаченного разового invoice сохраняется отдельным платежом и автоматически ставится на возврат без второго grant. В audit log `admin_telegram_user_id=0` означает системный автоматический возврат, не администратора Telegram.

Статистика содержит пользователей, новых за 24 часа, действующие доступы, число невозвращённых оплат, Stars за вычетом возвратов, число возвратов, top-20 продуктов и тарифов по выручке. Исторические сущности доступны с пагинацией; физического удаления платежей нет.

## Предыдущая схема Stars и совместимость с историческими платежами

Новые покупки работают по [схеме баланса](docs/WALLET.md). Описание ниже относится к обработке старых заказов и сохранённой истории. Создание новых recurring invoice отключено.

Для цифрового доступа используются только `XTR`, одна целочисленная позиция цены и пустой provider token. Отдельный карточный payment provider в BotFather не подключается. Сначала создаётся Order, затем invoice с непрозрачным UUID заказа.

Pre-checkout проверяет владельца, сумму, валюту, состояние, активность тарифа/продукта/канала и срок invoice. Он **не выдаёт доступ**. Подтверждённый successful payment обрабатывается транзакционно; уникальный charge ID исключает повторное продление. Поздний успешный платёж учитывается даже после истечения invoice или скрытия продукта: деньги уже списаны.

Recurring создаётся через `createInvoiceLink` с `subscription_period=2592000`. Цена recurring ограничена 10000 Stars. `subscription_expiration_date` преобразуется из Unix timestamp в UTC и определяет оплаченный период. События `subscription` меняют только состояние автопродления; они никогда не создают права доступа. На aiogram 3.26 используется небольшой проверяемый Pydantic-адаптер этого поля Bot API.

Отмена в «Мои покупки» отключает дальнейшее продление, сохраняет оплаченный срок. При сбое очередного списания дата доступа не продлевается. Приложение не запускает собственный график списаний.

Контракт сверён с [официальным Telegram Bot API](https://core.telegram.org/bots/api) 13.09.2026. Ссылки на конкретные методы: [docs/TELEGRAM_API.md](docs/TELEGRAM_API.md).

## Тестирование Stars

Автоматические тесты используют реальный PostgreSQL и Redis, подменяя только сеть Telegram. Настоящие Stars не списываются. Перед приёмом покупателей выполните проверку отдельным ботом и закрытым каналом: бесплатная выдача, небольшая оплаченная покупка, возврат, попытка входа чужим аккаунтом и снятие доступа. Проверяйте текущую процедуру Telegram в [руководстве Stars](https://core.telegram.org/bots/payments-stars).

Для тестовой среды Telegram нужен отдельный test bot/test account и соответствующий API endpoint; этот проект по умолчанию подключается к обычному Bot API и не переключается в test environment автоматически. Нельзя считать локальные mocks подтверждением реального денежного расчёта или ожидания продления через 30 дней.

## Автоматические тесты

Создайте **пустую disposable** БД и выделенную Redis DB, примените миграции к ней:

```sh
export TEST_DATABASE_URL='postgresql+asyncpg://test_user:password@127.0.0.1:5432/shop_test'
export TEST_REDIS_URL='redis://127.0.0.1:6379/15'
MIGRATION_DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
pytest -q
ruff check app tests alembic
ruff format --check app tests alembic
```

Integration fixtures очищают таблицы test database и Redis DB. Без TEST_* соответствующие тесты **пропускаются**; одного успешного unit-прогона недостаточно для проверки PostgreSQL locks. Тесты не предназначены для `pytest-xdist` с общей БД.

## Миграции

Миграции заморожены в `alembic/versions`, runtime не вызывает `create_all`. Перед обновлением делайте backup, сначала применяйте миграции на копии БД и проверяйте `alembic check`. Начальные миграции создают историю и усиливают ограничения. Автоматический destructive downgrade запрещён: откатывайте совместимый код, а при необходимости восстанавливайте backup в отдельную БД. Для будущего NOT NULL сначала добавляйте nullable поле, backfill и проверку, затем ограничение.

## Backup и restore

Payment history критична. Рекомендуемая базовая политика: ежедневный полный `pg_dump -Fc`, шифрование `age`, отдельное хранилище вне сервера, 30 ежедневных и 12 ежемесячных копий, ежемесячная проверка восстановления. Секретный age key храните отдельно от сервера и backup. Мониторьте exit status backup-задачи, наличие нового объекта и срок последнего успешного restore drill.

Пример для защищённой оболочки администратора (установите `age`; публичный recipient не является секретным ключом):

```sh
set -o pipefail
mkdir -p backups
chmod 700 backups
docker compose exec -T postgres pg_dump -U postgres -d shop -Fc | age -r AGE_PUBLIC_RECIPIENT > "backups/shop-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
```

Планировщик сервера должен выполнять команду ежедневно и копировать зашифрованный результат во внешнее хранилище. Retention применяйте политикой этого хранилища; не удаляйте последнюю проверенную копию.

Ежедневный dump допускает потерю до 24 часов данных при аварии. Для денежных данных production включите **WAL archiving/PITR** средствами PostgreSQL/managed provider, задайте требуемый RPO и мониторьте доставку WAL. Compose сам не настраивает внешнее хранилище и PITR.

Восстановление: остановите app/worker, сохраните повреждённую БД для расследования, подготовьте **новую пустую** БД с владельцем `shop_migrator` (`createdb -O shop_migrator shop_restore` через административное соединение). Роли shop_migrator/shop_app должны существовать с атрибутами из `deploy/init-db.sh`, но schema shop заранее не создавайте: она восстановится из dump. Расшифруйте backup в защищённый каталог или pipe и выполните `pg_restore --no-owner --role=shop_migrator -d shop_restore` через административное соединение к новой БД. Восстановите grants `shop_app` (SELECT/INSERT/UPDATE, без DELETE; audit/update_receipts без UPDATE), проверьте Alembic head, количества payments, уникальные charge IDs и entitlements. Переключайте приложение только после проверки. Никогда не выполняйте `pg_restore --clean` на работающей production БД.

После потери части payment history не открывайте продажи до сверки с Telegram Star transaction history. Текущий reconciliation восстанавливает состояние доступа и приглашений, но **не импортирует потерянную историю финансовых событий**. Резервная копия Redis не заменяет backup PostgreSQL.

## Logs и troubleshooting

```sh
docker compose logs --tail 100 app worker
docker compose exec app python -m app.cli worker-health
docker compose exec nginx nginx -t
```

Логи — JSON; полные updates, invite URLs, секреты и exception values не пишутся. Используйте event, order_id/payment_id/entitlement_id и error_type. Не включайте debug-логи HTTP-клиента или SQL parameters на production.

- **403 webhook:** проверьте совпадение секрета, повторно установите webhook, проверьте домен/TLS. Не копируйте секрет в тикеты.
- **503 readiness/webhook:** проверьте PostgreSQL и Redis. Telegram повторяет неуспешную доставку; критические операции защищены БД.
- **Тариф не виден:** нужны активные продукт, канал и тариф.
- **Нет приглашения:** проверьте действующий entitlement и права бота; подождите rate-limit окно.
- **Участник остался после истечения:** проверьте worker heartbeat и события `telegram_api_error`, права удаления. Повторная reconciliation повторит операцию.
- **Refund pending:** не исправляйте paid/refunded вручную. Проверьте worker, доступ к Telegram и баланс Stars; повтор выполняется по тому же charge ID.
- **Ошибка ролей после смены .env:** init script работает только с пустым volume; изменяйте пароль существующей роли административной операцией.

Ограничения эксплуатации и результаты проверки безопасности перечислены в SECURITY.md и отчёте. Реальный запуск Docker/TLS и расчётов Stars требует конфигурации владельца бота.

## Запуск двойным кликом на macOS без Docker

В корне проекта есть **`Start.command`**. Для него нужны локально установленные Python 3.12, PostgreSQL 16+ и Redis. Если их нет, установите через Homebrew:

```sh
brew install python@3.12 postgresql@16 redis
```

Запускать `brew services start` не нужно: файл сам управляет отдельными сервисами проекта.

1. В `.env` заполните `BOT_TOKEN`, `ADMIN_TELEGRAM_IDS`, `SUPPORT_CONTACT`. При наличии укажите свои `TERMS_URL` и `TERMS_TEXT`.
2. Откройте `Start.command` двойным кликом в Finder. Если macOS не разрешает открыть файл, используйте правую кнопку → «Открыть» либо команду `./Start.command` в терминале проекта.
3. Дождитесь сообщения о запуске процессов и отправьте боту `/start` в Telegram.
4. Для остановки нажмите **Ctrl+C** в открывшемся терминале. Данные сохранятся между запусками.

Лаунчер устанавливает зависимости из `requirements.lock`, создаёт PostgreSQL-кластер, отдельные DB-роли и Redis, применяет миграции, запускает polling, worker и FastAPI. Первому запуску нужен интернет для установки зависимостей.

`.env` не изменяется. Для дочерних процессов автоматически используются `APP_ENV=development` и отдельные локальные credentials/URLs. Docker-пароли и адреса из `.env` этому режиму не нужны. Webhook secrets генерируются автоматически; домен и сертификат не требуются. Если TERMS_URL пуст, используется технический placeholder — перед настоящими продажами задайте опубликованные условия.

Порты: PostgreSQL `127.0.0.1:55432`, Redis `127.0.0.1:56380`, FastAPI `127.0.0.1:18080`. Готовность: `http://127.0.0.1:18080/ready`.

Данные и локальные секреты находятся в **`.local/`**, исключённом из Git. **Не удаляйте этот каталог**, если хотите сохранить покупки и пользователей. Это отдельная база: существующая Docker/production БД автоматически не импортируется.

Логи: `.local/bot.log`, `.local/worker.log`, `.local/api.log`, `.local/migrations.log`, `.local/postgres.log`, `.local/redis.log`. Не отправляйте каталог `.local` целиком другим людям: в нём есть credentials и данные пользователей.

Polling отключает webhook выбранного бота, сохраняя ожидающие updates. Используйте отдельного бота для локальной разработки, если основной уже работает на сервере. После локальной проверки webhook на сервере нужно установить заново.

На всех новых экранах, кроме самого главного меню, есть кнопка **«🏠 Главное меню»**: каталог, карточки, оплата,
поддержка, ошибки, админка и формы. Она открывает меню `/start`; для администратора
незавершённый черновик отменяется. Команда `/start` также отменяет черновик.
Уже отправленные ранее сообщения автоматически не обновляются — откройте `/start` заново.
