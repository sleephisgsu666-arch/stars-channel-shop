# Итоговый отчёт

> Обновление 14.09.2026: новые покупки переведены на внутренний баланс без автопродления. Актуальная схема — [WALLET.md](WALLET.md). Описание прямых платежей/recurring ниже сохранено как история и контекст совместимости.

## 1. Что реализовано

Полный локальный проект магазина Telegram-каналов: каталог и карточки, бесплатные тарифы, fixed/lifetime/recurring Stars, покупки, приглашения и заявки, expiration/reconciliation, admin CRUD, ручные права, refund, support/terms, лимиты и audit. Это работающая реализация с тестами, а не skeleton. Конфигурация владельца и проверка реальных внешних сервисов остаются обязательным этапом запуска.

## 2. Архитектура

FastAPI webhook → aiogram → сервисы → PostgreSQL entitlement → Telegram access. Redis используется для rate limiting. Async worker выполняет expiration, cleanup, reconciliation и refund queue. Web Admin можно добавить отдельным transport. [Подробная схема](ARCHITECTURE.md).

## 3. Структура файлов

```text
app/
  main.py, runtime.py, cli.py
  api/health.py, webhook.py
  bot/dispatcher.py, keyboards.py, handlers/{shop,admin}.py
  core/{config,security,logging}.py
  db/{models,session}.py
  schemas/admin.py
  services/{catalog,orders,payments,entitlements,telegram_access,subscriptions,refunds,admin}.py
  jobs/worker.py
alembic/versions/      три последовательные миграции
 tests/               unit, integration, security
 deploy/              nginx.conf, init-db.sh
Dockerfile, docker-compose.yml, .env.example
pyproject.toml, requirements.lock, requirements-dev.lock
README.md, SECURITY.md, docs/
```

## 4. Схема БД

13 таблиц. Помимо сущностей ТЗ добавлены grants для пересчёта прав при возврате, update_receipts для durable deduplication, refund_requests для восстановления финансовых операций и payment_reversals для переставленных событий. UUID PK, BIGINT Telegram IDs, UTC TIMESTAMPTZ, JSONB audit. Уникальные charge IDs и user/product entitlement защищены БД. String statuses + CHECK используются вместо native enum для более простых безопасных миграций.

## 5. Stars payment

Серверный Order сохраняет цену и длительность до invoice. Currency только XTR, Stars только integer. Pre-checkout проверяет сумму, валюту, владельца и доступность без выдачи права. Successful payment создаёт payment/grant/entitlement транзакционно; повтор не продлевает срок. Лишнее реальное списание для закрытого разового invoice ставится на возврат.

## 6. Recurring

createInvoiceLink, 2592000 секунд, максимум 10000 Stars. Unix expiration преобразуется в UTC. Renewal charge обновляет срок, старое событие не сокращает новый период. BotSubscriptionUpdated поддержан через валидационный адаптер. Отмена сохраняет уже оплаченный срок.

## 7. Invite protection

Персональная creates_join_request-ссылка на срок до 10 минут хранится с owner ID, chat ID и entitlement. Join проверяет все привязки и текущее право. Чужая ссылка отклоняется без отзыва ссылки настоящего владельца. После approve ссылка помечается использованной и отзывается.

## 8. Удаление истёкшего пользователя

Worker ставит expired/removal_pending, выполняет ban/unban и revoke. Покупка и удаление используют одну блокировку. Ошибки повторяются; reconciliation повторно проверяет известные неактивные права. Неудачное уведомление не влияет на удаление: уведомления об истечении в MVP не отправляются.

## 9. Security mechanisms

Webhook secret, UUID validation, ID-only admin authorization, transaction + row/session locks + UNIQUE, ownership checks, Redis Lua limits, ограничение body/input, redaction, отдельная DB runtime role, append-only audit, non-root/read-only app, HTTPS nginx. Результат: [SECURITY.md](../SECURITY.md).

## 10. Тесты

Unit: тарифы, сроки, lifetime/recurring, redaction, UUID, admin IDs, конфигурация. Integration/security: реальные PostgreSQL и Redis; wrong amount/currency/user, charge/update replay, concurrent purchases, order snapshots, free idempotency, invite sharing/IDOR, revoke/expire, refund retries/reordering, SQL constraints, worker batch fairness, transport E2E aiogram. Сеть Telegram подменяется.

## 11. Результаты

Итоговый протокол в [TEST_RESULTS.md](TEST_RESULTS.md). Дополнительно проверены Ruff, компиляция, чистая Alembic-миграция, отсутствие schema drift, privileges runtime role, Compose config и локальный backup/restore. Реальный Docker stack и Telegram платежи не запускались.

## 12. Локальный запуск

Python 3.12 venv → `pip install -r requirements-dev.lock` → dev PostgreSQL/Redis → `.env` → `alembic upgrade head` → `python -m app.cli poll` и отдельно `python -m app.jobs.worker`. Polling разрешён только в development. [Команды](../README.md#локальная-разработка).

## 13. Production

Заполнить `.env`, подготовить домен/TLS → `docker compose build` → `docker compose up -d postgres redis` → `docker compose run --rm migrate` → `docker compose up -d app worker nginx` → `docker compose exec app python -m app.cli set-webhook`. [Deployment](../README.md#production-deployment).

## 14. Environment

Обязательны BOT_TOKEN, WEBHOOK_BASE_URL/PATH/SECRET, DATABASE_URL, REDIS_URL, ADMIN_TELEGRAM_IDS, SUPPORT_CONTACT, TERMS_URL; для Compose — отдельные bootstrap/app/migrator/Redis passwords и MIGRATION_DATABASE_URL. Все значения объяснены в README и `.env.example`. Реальных секретов в проекте нет.

## 15. Права бота

Администратор закрытого канала: can_invite_users + can_restrict_members. Остальные права не нужны. Публичные каналы не принимаются. Старые общие приглашения должны быть отозваны владельцем.

## 16. BotFather

Создать бота `/newbot`, получить токен, настроить команды start/terms/support/paysupport/admin, при необходимости описание и аватар. Отдельный карточный provider token для Stars не требуется. Затем добавить бота в канал и указать свой числовой admin ID.

## 17. Security audit result

Проверки реализации PASS с привязкой к тестам и проверенным PostgreSQL privileges. Независимый pentest не проводился; этот статус не означает проверенную доступность реального сервера, TLS или финансовых расчётов. Найденные ошибки timestamp, nullable CHECK, refund ordering, duplicate charges и batch starvation исправлены до отчёта.

## 18. Известные ограничения

- Для запуска нужны собственный bot token, числовые IDs, закрытый канал, домен, TLS и опубликованные условия.
- Docker daemon отсутствовал; image build/runtime и реальные Stars не проверены. Теги официальных Docker images существуют.
- Админка поддерживает пошаговые формы и редактирование кнопками, черновики хранятся в Redis 30 минут. Web Admin отсутствует. Канал существующего продукта неизменяем.
- Статистика по продуктам/тарифам выводит top-20; полная история доступна в PostgreSQL.
- Exactly-once доставки исходящих сообщений Telegram нет; после сбоя возможен повтор сообщения/invoice, критические права идемпотентны.
- Recurring state adapter нужен для API поля, ещё не представленного закреплённой версией SDK.
- Идентификаторы updates сохраняются без автоматической очистки; для большого потока потребуется согласованная retention-политика.
- Reconciliation не импортирует потерянные финансовые события и не обнаруживает всех участников, добавленных владельцем канала вручную.
- Внешний backup storage, encryption keys, WAL/PITR и мониторинг описаны, но требуют настройки на сервере.
