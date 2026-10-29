FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

COPY --chown=app:app app ./app
COPY --chown=app:app api ./api
COPY --chown=app:app agents ./agents
COPY --chown=app:app config ./config
COPY --chown=app:app database ./database
COPY --chown=app:app monitoring ./monitoring

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
