#!/usr/bin/env bash
# Проверка: процесс приложения не устанавливает соединений с внешними хостами.
# Делает несколько запросов к API и выводит все сетевые соединения процесса (должны быть только localhost).
set -euo pipefail
PORT="${PORT:-8000}"
PID=$(pgrep -f "chainlit run app/main.py" | head -1)
[ -z "$PID" ] && { echo "приложение не запущено"; exit 1; }
for q in "Когда регистрировать входящий документ?" "Кто визирует распоряжение о создании рабочей группы?" "Как приготовить борщ?"; do
  curl -s -X POST "localhost:$PORT/api/ask" -H 'Content-Type: application/json' -d "{\"text\":\"$q\"}" >/dev/null &
done
sleep 0.5
CONN=$(lsof -nP -a -p "$PID" -i 2>/dev/null | awk 'NR>1{print $9}' | sort -u)
wait
echo "Соединения процесса $PID:"; echo "$CONN" | sed 's/^/  /'
EXT=$(echo "$CONN" | grep -vE '^(127\.0\.0\.1|localhost|\[::1\]|\*|::1)[:.]' | grep -vE '^(127\.0\.0\.1|\[::1\]):[0-9]+->(127\.0\.0\.1|\[::1\]):[0-9]+$' | grep -v '^\*:' || true)
if [ -n "$EXT" ]; then echo "ВНЕШНИЕ СОЕДИНЕНИЯ:"; echo "$EXT"; exit 1; fi
echo "OK: внешних соединений нет"
