FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ANONYMIZED_TELEMETRY=False
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "python scripts/ingest.py --all && (test -f samples/Черновик_с_ошибками.docx || python scripts/make_demo_draft.py) && chainlit run app/main.py --host 0.0.0.0 --port 8000 --headless"]
