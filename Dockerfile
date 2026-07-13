# syntax=docker/dockerfile:1.7

FROM rust:1.88-bookworm AS rust-builder
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --locked --release && \
    cp target/release/llm-rust /tmp/llm-rust

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    RAG_HOST=0.0.0.0 \
    RAG_PORT=8000 \
    GIGACHAT_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

RUN apt-get update && apt-get install --yes --no-install-recommends \
        build-essential \
        curl \
        openssl \
        ca-certificates \
        libgomp1 \
        libmagic1 \
        postgresql-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY certs/ /usr/local/share/ca-certificates/gigachat/
RUN set -eux; \
    for certificate in /usr/local/share/ca-certificates/gigachat/*.cer; do \
        case "$certificate" in *gost*) continue ;; esac; \
        target="${certificate%.cer}.crt"; \
        if grep -q -- "-----BEGIN CERTIFICATE-----" "$certificate"; then \
            cp "$certificate" "$target"; \
        else \
            openssl x509 -inform DER -in "$certificate" -out "$target"; \
        fi; \
    done; \
    update-ca-certificates; \
    rm -rf /usr/local/share/ca-certificates/gigachat
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip && \
    python -m pip install --requirement requirements.txt

RUN groupadd --gid 10001 rag && \
    useradd --uid 10001 --gid rag --create-home --shell /usr/sbin/nologin rag

COPY --chown=rag:rag . .
COPY --from=rust-builder --chown=rag:rag /tmp/llm-rust /app/target/debug/llm-rust
RUN install -o rag -g rag -m 0755 deploy/entrypoint.sh /usr/local/bin/rag-entrypoint && \
    mkdir -p /app/.data /app/.fastembed_cache && \
    chown -R rag:rag /app

USER rag
EXPOSE 8000
ENTRYPOINT ["tini", "--", "/usr/local/bin/rag-entrypoint"]
CMD ["api"]
