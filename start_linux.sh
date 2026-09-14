#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "========================================="
echo "   Быстрый запуск Stars Channel Shop     "
echo "========================================="

# 1. Проверка .env
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "[+] Создан файл .env"
fi

# Настройка локальных URL баз данных в .env
sed -i 's|^APP_ENV=.*|APP_ENV=development|' .env 2>/dev/null || true
sed -i 's|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://shop_app:app_pass@localhost:5432/shop|' .env 2>/dev/null || true
sed -i 's|^MIGRATION_DATABASE_URL=.*|MIGRATION_DATABASE_URL=postgresql+asyncpg://shop_migrator:migrator_pass@localhost:5432/shop|' .env 2>/dev/null || true
sed -i 's|^REDIS_URL=.*|REDIS_URL=redis://localhost:6379/0|' .env 2>/dev/null || true

# 2. Если запущен от root и есть apt — ставим и настраиваем службы
if [ "$(id -u)" -eq 0 ]; then
  if command -v apt >/dev/null 2>&1; then
    echo "[+] Запуск от root: установка пакетов apt..."
    export DEBIAN_FRONTEND=noninteractive
    apt update -qq || true
    apt install -y -qq python3 python3-venv python3-pip postgresql redis-server || true
    
    service postgresql start 2>/dev/null || systemctl start postgresql 2>/dev/null || true
    service redis-server start 2>/dev/null || systemctl start redis-server 2>/dev/null || true

    echo "[+] Настройка базы данных PostgreSQL..."
    su - postgres -c "psql" >/dev/null 2>&1 << 'EOF' || true
CREATE USER shop_migrator WITH PASSWORD 'migrator_pass';
CREATE USER shop_app WITH PASSWORD 'app_pass';
CREATE DATABASE shop OWNER shop_migrator;
\c shop
CREATE SCHEMA IF NOT EXISTS shop AUTHORIZATION shop_migrator;
GRANT USAGE ON SCHEMA shop TO shop_app;
ALTER DEFAULT PRIVILEGES FOR ROLE shop_migrator IN SCHEMA shop GRANT SELECT, INSERT, UPDATE ON TABLES TO shop_app;
ALTER DEFAULT PRIVILEGES FOR ROLE shop_migrator IN SCHEMA shop GRANT USAGE, SELECT ON SEQUENCES TO shop_app;
ALTER ROLE shop_app SET search_path = shop;
ALTER ROLE shop_migrator SET search_path = shop;
EOF
  fi
else
  echo "[i] Запуск от обычного пользователя ($(whoami)). Пропуск системной установки пакетов."
fi

# 3. Проверка Python
if ! command -v python3 >/dev/null 2>&1; then
  echo "ОШИБКА: python3 не найден в системе. Установите python3."
  exit 1
fi

# 4. Виртуальное окружение Python
echo "[+] Настройка окружения Python..."
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.lock

# 5. Проверка BOT_TOKEN
if grep -q "^BOT_TOKEN=$" .env || grep -q "^BOT_TOKEN=[[:space:]]*$" .env; then
  echo ""
  echo "ВНИМАНИЕ: Не задан токен бота в .env!"
  echo -n "Введите ваш BOT_TOKEN от @BotFather: "
  read -r input_token
  if [ -n "$input_token" ]; then
    sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$input_token|" .env 2>/dev/null || true
  fi
fi

# 6. Накат миграций
echo "[+] Накат миграций БД..."
if ! alembic upgrade head; then
  echo ""
  echo "=========================================================="
  echo "ОШИБКА: Не удалось подключиться к PostgreSQL."
  echo "Если PostgreSQL ещё не запущен или вы не root, выполните:"
  echo "  1) Переключитесь на root: su -"
  echo "  2) Запустите скрипт заново: bash start_linux.sh"
  echo "Или используйте Docker: docker compose run --rm migrate"
  echo "=========================================================="
  exit 1
fi

# 7. Запуск бота
echo "========================================="
echo "   Бот успешно запускается в режиме poll "
echo "========================================="
python -m app.cli poll
