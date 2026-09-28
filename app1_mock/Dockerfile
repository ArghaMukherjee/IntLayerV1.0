# Build from the repository root: docker build -f app1_mock/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY app1_mock/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY app1_mock ./app1_mock
RUN useradd --system --uid 10001 app
USER app

EXPOSE 8002
CMD ["uvicorn", "app1_mock.main:app", "--host", "0.0.0.0", "--port", "8002", "--proxy-headers"]
