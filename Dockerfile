# --- stage 1: build the frontend ---
# Done in its own stage so Node and node_modules never ship in the final
# image — only the built static files do.
FROM node:22-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- stage 2: python runtime ---
FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY code/ ./code/
COPY dataset/ ./dataset/
COPY --from=frontend-build /build/dist ./frontend/dist

EXPOSE 5000

# 1 worker, 2 threads: the whole dataset is held in memory per worker
# (~8MB of CSVs parsed into objects), and the Gemini rate limiter is a
# per-process in-memory window — multiple workers would each keep their own
# counter and collectively blow through the free-tier quota.
CMD ["sh", "-c", "gunicorn --chdir code wsgi:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 2 --timeout 120"]
