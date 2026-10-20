FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[api]"

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["uvicorn", "backtester.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
