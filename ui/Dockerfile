# Build from the repository root: docker build -f ui/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY ui/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY integration_layer/__init__.py integration_layer/templating.py ./integration_layer/
COPY ui ./ui
RUN useradd --system --uid 10001 app
USER app

EXPOSE 8080
CMD ["uvicorn", "ui.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
