FROM node:22-bookworm-slim AS javascript-runtime
FROM python:3.12-slim-bookworm
COPY --from=javascript-runtime /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py media.py authentication.py user_store.py accounts.py archives.py site_cookies.py ./
COPY frontend ./frontend
RUN mkdir -p /app/config /app/temp
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
