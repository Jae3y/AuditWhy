FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    SAMPLE_MODE=true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run unprivileged; only logs/ and data/ need to be writable (traces, H-MEM state, warehouse snapshot).
RUN useradd --create-home --uid 10001 auditwhy && chown -R auditwhy:auditwhy /app/logs /app/data
USER auditwhy

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"

CMD ["python", "run.py"]
