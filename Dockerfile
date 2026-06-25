# syntax=docker/dockerfile:1

# --- Stage 1: build the admin UI (Node lives only here, not in the runtime) ---
FROM node:22-slim AS frontend
WORKDIR /web_ui
COPY web_ui/package.json web_ui/package-lock.json ./
RUN npm ci
COPY web_ui/ ./
RUN npm run build

# --- Stage 2: Python runtime ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

# Bundle the built SPA so admin_api.mount_admin_ui can serve it. Absent build
# is a no-op (the API still works), so this COPY is what activates the UI.
COPY --from=frontend /web_ui/dist ./src/mm_jira_bot/admin_static

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["python", "-m", "mm_jira_bot"]
