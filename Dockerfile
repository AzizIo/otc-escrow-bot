FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
RUN pip install --no-cache-dir .
COPY alembic.ini .
COPY migrations ./migrations
CMD ["sh", "-c", "alembic upgrade head && python -m app"]
