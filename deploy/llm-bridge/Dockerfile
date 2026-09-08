FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --requirement requirements.txt
COPY app.py ./

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bridge && \
    mkdir -p /data/fastembed && chown -R bridge:bridge /app /data
USER bridge

EXPOSE 11434
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "11434"]
