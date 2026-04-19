# syntax=docker/dockerfile:1.6

# --- Stage 1: build ---------------------------------------------------------
#
# We rely entirely on manylinux wheels for our stack (grpcio, numpy, pandas,
# scikit-learn, statsmodels, lightgbm, shap, psycopg2-binary, …) — none of
# them require a C toolchain when a wheel matching the target platform is
# available on PyPI. That lets us skip apt entirely and keep build memory
# tiny.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY pyproject.toml ./
COPY src ./src
COPY proto ./proto

RUN pip install --upgrade pip \
 && pip install ".[dev]" \
 && python -m grpc_tools.protoc \
        -I proto \
        --python_out=src/ml_forecast/grpc_gen \
        --grpc_python_out=src/ml_forecast/grpc_gen \
        proto/finzooka/ml/v1/ml_forecast.proto \
 && find src/ml_forecast/grpc_gen/finzooka -type d \
        -exec sh -c 'touch "$1/__init__.py"' _ {} \; \
 && find src/ml_forecast/grpc_gen/finzooka -name "*.py" \
        -exec sed -i 's/^from finzooka\.ml\.v1 import/from ml_forecast.grpc_gen.finzooka.ml.v1 import/' {} +

# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ML_ARTIFACT_DIR=/data/models \
    ML_ARCHIVE_DIR=/data/archive \
    PYTHONPATH=/app/src

# libgomp1 is needed by lightgbm at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --shell /usr/sbin/nologin mlforecast \
 && mkdir -p /data/models /data/archive \
 && chown -R mlforecast:mlforecast /data

# Copy the Python site-packages and console scripts that pip installed in
# the builder stage (grpcio, alembic, pytest, …). This keeps the runtime
# image self-contained without re-running pip.
COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app
COPY --from=builder /build/src /app/src
COPY --from=builder /build/proto /app/proto
COPY config /app/config
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

USER mlforecast

EXPOSE 50051 9100

CMD ["python", "-m", "ml_forecast.main"]
