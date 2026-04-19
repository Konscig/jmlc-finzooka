# syntax=docker/dockerfile:1.6

# --- Stage 1: build ---------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

# Install runtime deps + generate grpc stubs from proto.
COPY proto ./proto
RUN pip install --upgrade pip \
 && pip install ".[dev]" \
 && python -m grpc_tools.protoc \
        -I proto \
        --python_out=src/ml_forecast/grpc_gen \
        --grpc_python_out=src/ml_forecast/grpc_gen \
        proto/finzooka/ml/v1/ml_forecast.proto

# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ML_ARTIFACT_DIR=/data/models \
    ML_ARCHIVE_DIR=/data/archive

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --shell /usr/sbin/nologin mlforecast \
 && mkdir -p /data/models /data/archive \
 && chown -R mlforecast:mlforecast /data

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/src /app/src
COPY --from=builder /build/proto /app/proto
COPY config /app/config
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

WORKDIR /app
ENV PYTHONPATH=/app/src

USER mlforecast

EXPOSE 50051 9100

CMD ["python", "-m", "ml_forecast.main"]
