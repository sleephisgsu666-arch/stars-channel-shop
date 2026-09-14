# Протокол локальной проверки

Дата: 13.09.2026.

| Проверка | Результат |
|---|---|
| `pytest -q` с PostgreSQL и Redis | **71 passed in 2.84s**, без skipped |
| `ruff check app tests alembic` | All checks passed |
| `ruff format --check app tests alembic` | 49 files already formatted |
| `python -m compileall -q app tests alembic` | Успешно |
| Alembic upgrade head на чистой отдельной БД | Все 3 миграции применены |
| `alembic check` после миграций | No new upgrade operations detected |
| DB runtime role | NOSUPERUSER, NOCREATEDB, NOCREATEROLE; нет CREATE SCHEMA и DELETE; audit/receipts без UPDATE |
| `docker compose config --quiet` с синтетической конфигурацией | Успешно |
| Docker tags | Python 3.12.12-slim, PostgreSQL 16.14-alpine, Redis 7.4.8-alpine, nginx 1.28.2-alpine существуют в официальном Docker Hub |
| Локальный pg_dump → новая БД → pg_restore → alembic check | Успешно; schema drift не обнаружен |
| Docker build и запуск полного стека | Не выполнены: Docker daemon не запущен |
| Настоящие Telegram Stars/recurring/refund/TLS | Не выполнены: не использовались реальные credentials/канал/домен |

Тесты выполнялись Python 3.12 на macOS, PostgreSQL 16.14 и отдельном локальном Redis. БД и Redis использовались только для этого проекта. Telegram API подменялся на уровне Bot transport или async mock; никакие настоящие сообщения, платежи и возвраты не отправлялись.

Проверены успешные сценарии и отказы: pre-checkout без доступа, verified payment, неправильная сумма/валюта/владелец, replay charge/update, конкурентные покупки и refund, snapshot тарифа, free activation, lifetime, renewal timestamp, link sharing, IDOR, истечение, ban/unban recovery, refund-before-payment, SQL injection-like input, admin impersonation, Redis atomic rate limits, malformed IDs, webhook forgery/body limit, worker pagination и применение миграций с минимальными privileges.

Воспроизведение: инструкции TEST_DATABASE_URL/TEST_REDIS_URL в README. Эти URL должны указывать на disposable сервисы: fixtures очищают тестовые таблицы и выбранную Redis database.

## Обновление: пошаговая админка

После добавления форм: **94 passed**, без skipped, на отдельных PostgreSQL/Redis. Ruff прошёл.
Проверены добавление продукта до/после подтверждения, переход recurring → free,
сохранение остальных полей при редактировании, конфликт двух администраторов,
устаревшие кнопки, повторный текст, отмена, права администратора, загрузка обложки,
лимит callback 64 байта и безопасная обработка ошибки проверки канала.
Реальные Telegram-вызовы в тестах подменены. Рабочие данные и `.env` не изменялись.

## Исправление остановки polling на старом callback

Из лога воспроизведена ошибка Telegram `query is too old` в ответе на кнопку админки.
Истёкшее подтверждение кнопки теперь не прерывает обработчик. Остальные ошибки API не скрываются.
Polling повторяет обработку после временной ошибки, не продвигая offset за неуспешный update.
Конфликт двух polling-процессов и неверная авторизация остаются явными ошибками.
Проверки: 51 unit-тест прошёл, включая 6 новых регрессионных тестов; Ruff и компиляция успешны.
Реальный бот не запускался; .env и данные пользователей не изменялись.

## Обновление: баланс и разовые покупки доступа

Полный набор: **120 passed**, без skipped, на отдельных PostgreSQL/Redis.
Ruff check и проверка форматирования прошли (61 файл).
Проверены пополнение без выдачи доступа, покупка с баланса, пожизненный доступ,
повторная доставка платежей и нажатий, конкурентные списания, недостаточный баланс,
проверка владельца, возвраты покупок и пополнений, внешнее аннулирование платежа,
статистика и полный сценарий через Telegram transport с подменённым API.

Миграция `7a171d0f4508` применена в отдельной тестовой базе, в том числе
с ролью `shop_migrator`; `alembic check` не обнаружил расхождений схемы.
Для `shop_app` в таблицах `wallet_entries` и `wallet_allocations` подтверждены
разрешение INSERT и запреты UPDATE/DELETE.
Настоящие платежи Telegram не выполнялись. Рабочая база и `.env` не изменялись;
миграция рабочей базы применяется при следующем запуске `Start.command`.
