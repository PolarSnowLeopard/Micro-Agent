FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir . 2>/dev/null || true

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8010 22 8000

CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8010"]
