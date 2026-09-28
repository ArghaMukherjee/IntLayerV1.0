# Build from the repository root: docker build -f app2/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY app2/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY app2 ./app2
RUN useradd --system --uid 10001 app
USER app

EXPOSE 8001
CMD ["uvicorn", "app2.main:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers"]
