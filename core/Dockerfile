FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[agent,db,worker]"

COPY core ./core

EXPOSE 8000
CMD ["uvicorn", "core.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
