FROM python:3.11-slim

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir .

RUN mkdir -p tmp/ai/logs tmp/ai/uploads

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
