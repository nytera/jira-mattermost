FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["python", "-m", "mm_jira_bot"]
