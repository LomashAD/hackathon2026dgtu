#!/usr/bin/env bash
# Запуск одной командой: ./run.sh
# Готовит окружение, поднимает Ollama (нативно, с GPU), скачивает модели из config.yaml,
# индексирует базу знаний (если ещё не проиндексирована) и запускает веб-интерфейс.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
export ANONYMIZED_TELEMETRY=False
HOST="${HOST:-0.0.0.0}"; PORT="${PORT:-8000}"
mkdir -p storage/logs

PY=python3
for c in python3.13 python3.12 python3.11; do command -v $c >/dev/null && PY=$c && break; done
$PY -c 'import sys; assert sys.version_info >= (3, 11), "нужен Python 3.11+"'

if [ ! -x .venv/bin/python ]; then
  echo "→ создаю виртуальное окружение"; $PY -m venv .venv
fi
if ! cmp -s requirements.txt .venv/.req.stamp 2>/dev/null; then
  echo "→ устанавливаю зависимости"; .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt
  cp requirements.txt .venv/.req.stamp
fi

if ! curl -sf "$OLLAMA_HOST/api/tags" >/dev/null; then
  if command -v ollama >/dev/null; then
    echo "→ запускаю Ollama (лог: storage/logs/ollama.log)"
    nohup ollama serve > storage/logs/ollama.log 2>&1 &
    for _ in $(seq 1 30); do curl -sf "$OLLAMA_HOST/api/tags" >/dev/null && break; sleep 1; done
  else
    echo "Ollama не найдена. Установите: brew install ollama (macOS) или https://ollama.com/download, либо docker compose up"; exit 1
  fi
fi

for m in $(.venv/bin/python -c 'from app.config import get_config as g; c=g(); print(c["llm"]["model"], c["embeddings"]["model"])'); do
  if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m\(:latest\)\?"; then
    echo "→ скачиваю модель $m (однократно)"; ollama pull "$m"
  fi
done

echo "→ проверяю базу знаний"
.venv/bin/python scripts/ingest.py --all | sed 's/^/   /'
[ -f samples/Черновик_с_ошибками.docx ] || .venv/bin/python scripts/make_demo_draft.py >/dev/null
echo "→ проверяю учётные записи"
.venv/bin/python scripts/users.py init | sed 's/^/   /'

echo "→ http://localhost:$PORT  (макет портала с виджетом)   http://localhost:$PORT/?chat  (чат)"
exec .venv/bin/chainlit run app/main.py --host "$HOST" --port "$PORT" --headless
