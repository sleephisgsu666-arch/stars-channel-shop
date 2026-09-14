#!/bin/bash
# Double-click in Finder, or run ./Start.command from Terminal.
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="/opt/homebrew/opt/postgresql@16/bin:/usr/local/opt/postgresql@16/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
finish() {
  result=$?
  if [ "$result" -ne 0 ]; then
    printf '\nЗапуск остановлен. Исправьте указанную ошибку и запустите файл снова.\n'
    if [ -t 0 ]; then read -r -p 'Нажмите Enter, чтобы закрыть окно… ' _; fi
  fi
}
trap finish EXIT
if ! command -v python3.12 >/dev/null 2>&1; then
  printf 'Не найден Python 3.12. Установите: brew install python@3.12\n'
  exit 1
fi
for program in initdb pg_ctl redis-server; do
  if ! command -v "$program" >/dev/null 2>&1; then
    printf 'Не найден %s. Установите: brew install postgresql@16 redis\n' "$program"
    exit 1
  fi
done
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  printf 'Создан .env. Заполните BOT_TOKEN, ADMIN_TELEGRAM_IDS и SUPPORT_CONTACT.\n'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  printf 'Создаю виртуальное окружение…\n'
  python3.12 -m venv .venv
fi
printf 'Подготавливаю локальный запуск…\n'
.venv/bin/python scripts/local_start.py
