# Security review

Дата: 13.09.2026. Область: исходный код, миграции, Docker/Compose/nginx-конфигурация и локальные автоматические проверки. Это внутренний инженерный review, не независимый аудит и не подтверждение реального production-развёртывания.

## Результат проверки реализации

| Проверка | Статус | Комментарий / доказательство |
|---|---|---|
| Bot Token не в Git | PASS | Реальные credentials не вводились; `.env` исключён из Git и Docker context. Тестовые токены синтетические. |
| Webhook Secret | PASS | Обязательный отдельный secret; constant-time comparison; без/с неверным заголовком 403 до DB dispatch. |
| Payment idempotency | PASS | UNIQUE charge ID, Order FOR UPDATE, transaction, advisory lock; конкурентные повторы проверены. |
| DB transactions | PASS | Payment + grant + entitlement + order коммитятся вместе. API-вызовы за пределами бизнес-транзакций. |
| Invite protection | PASS | Привязка к user/chat/entitlement, TTL, join request, повторная проверка права, revoke. Shared-link attack отклоняется. |
| Admin ID authorization | PASS | Числовой allowlist в transport и service; ни одна проверка не использует username. |
| Rate limiting | PASS | Redis Lua INCR/EXPIRE атомарно; отдельные лимиты invoice/invite/free/callback/admin/support по user ID. |
| SQL injection protection | PASS | ORM/bind parameters; UUID/JSON/Pydantic validation; SQL-like строка сохраняется как данные. |
| Secret log redaction | PASS | Полные updates не логируются; redaction секретов/URL/token/DSN; скрыты значения config validation errors и exception values. |
| Minimal bot permissions | PASS | Проверка приватного канала, can_invite_users и can_restrict_members. Другие права не запрашиваются. |
| Refund handling | PASS | Durable refund request, retry, charge-level идемпотентность; refund до payment сохраняется в reversal inbox и не выдаёт доступ. |
| Backup strategy documented | PASS | README: encrypted daily backup, off-host retention, restore, WAL/PITR и ограничения RPO. |
| Runtime DB least privilege | PASS | На отдельной PostgreSQL БД проверены отсутствие SUPERUSER/CREATEDB/CREATEROLE/CREATE SCHEMA/DELETE; audit/receipt append-only. |
| Expiration/renewal race | PASS | Один user/product lock для выдачи, продления и ban/unban; повторная проверка active state перед удалением. |
| Worker retry fairness | PASS | Keyset pagination; тест 205 ошибочных записей подтверждает обход следующих пакетов. |
| Dependency pinning | PASS | Прямые зависимости в pyproject; полный runtime/dev lock; версии Docker-образов зафиксированы и теги проверены. |

## Исправления, сделанные во время review

- Исправлен тип recurring expiration: aiogram 3.26 передаёт integer Unix timestamp; хранение и расчёты используют aware UTC datetime.
- Усилены PostgreSQL CHECK: NULL duration больше не обходит условие fixed/recurring за счёт SQL three-valued logic.
- Runtime-контейнеры не получают миграционные и bootstrap credentials.
- Соединения advisory locks вынесены в NullPool: session lock не возвращается в обычный pool.
- Добавлен финансовый учёт лишнего реального charge для одного invoice и автоматическая очередь возврата.
- Добавлен reversal inbox: перестановка refund/successful_payment не создаёт неоплаченный доступ.
- Refund исключает только соответствующий grant, сохраняя независимые покупки.
- Фоновые задачи обходят все пакеты; постоянная ошибка первых записей не блокирует следующие.
- Runtime не может обновлять/удалять audit log и update receipts.

## Границы проверки

- Telegram API в тестах подменён. Реальные платежи, refund, полномочия в настоящем канале и продление через 30 дней требуют проверки владельцем отдельного бота. Реальные токены не использовались.
- Docker daemon в среде выполнения не запущен. `docker compose config --quiet` выполнен, но сборка образа, запуск всего Compose и TLS handshake не проверялись.
- Проверен локальный pg_dump/pg_restore. Автоматическая внешняя доставка, age key management и WAL archiving на production-сервере не настраивались.
- Lock удерживается отдельным PostgreSQL session connection. Нельзя использовать transaction pooling или исполнять mutations альтернативным кодом без общего lock protocol.
- Сетевой сбой Telegram может задержать удаление. Durable state и reconciliation обеспечивают повтор после восстановления, но не синхронную гарантию на недоступном Telegram.
- Reconciliation проверяет известные entitlement. Оно не перечисляет всех участников канала и не импортирует потерянные финансовые события Telegram.
- Операционные метрики представлены structured events. Нужны внешние alerts на webhook 5xx, worker errors, незавершённые refunds, отставание удаления и backup/WAL age.
- Lock-файлы обеспечивают воспроизводимость, но не являются заявлением об отсутствии всех CVE. Регулярно проверяйте advisory базу зависимостей и обновляйте версии с тестированием.

## Реакция на инцидент

При утечке токена отзовите его в BotFather, задайте новый secret/path webhook, перезапустите процессы и повторно установите webhook. При утечке БД отдельно смените app/migrator/bootstrap credentials. Не удаляйте payment/audit history. При подозрении на неоплаченный доступ остановите новые продажи, сохраните backup и журналы, сверяйте charge IDs и grants; не считайте membership доказательством оплаты.

При потере payment history не создавайте права на основании наличия людей в канале. Восстановите проверенную БД и сверяйте пропущенные транзакции с официальной Star transaction history до возобновления продаж.

## Баланс магазина (14.09.2026)

Добавлены CHECK неотрицательного остатка, append-only ledger, привязка расходов к пополнениям,
идемпотентное зачисление и атомарная покупка. Возврат пополнения резервирует средства.
Внешний возврат потраченного пополнения отменяет финансировавшиеся им покупки, сохраняя
средства из других источников. Права и история legacy платежей сохраняются.
Новая схема и границы возвратов: [docs/WALLET.md](docs/WALLET.md).
