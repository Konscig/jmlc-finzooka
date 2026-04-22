# ml-forecast

gRPC microservice hosting the price-forecasting model for Finzooka.
See `specs/001-ml-forecast-service/spec.md` for the feature specification.

**Status**: Phase 1 Setup + Phase 2 Foundational complete (33/98 tasks).
No forecasting RPC yet — only health check and the full domain /
persistence / feature-engineering foundation. See PR #1 for progress.

## Quickstart (Windows + Docker Desktop)

Prerequisites on the target machine:

- **Docker Desktop for Windows** ≥ 4.26 (Linux containers mode; WSL2 backend
  is fine — it is the default). Check with `docker --version` and
  `docker compose version`.
- **git for Windows** (any recent version).
- That is all. Python does **not** need to be installed on the host —
  everything runs inside containers.

Steps (PowerShell):

```powershell
# 1. Clone the repo and switch to the feature branch.
git clone https://github.com/Konscig/jmlc-finzooka.git
cd jmlc-finzooka
git checkout 001-ml-forecast-service
git pull

# 2. Build the image and bring up Postgres + Redis + ml-forecast.
#    First run downloads python:3.11-slim and installs deps (~8-12 min).
docker compose up --build -d postgres redis

#    One-shot DB migration. Exits 0 when the schema is in place.
docker compose up ml-migrate
docker compose logs ml-migrate | Select-Object -Last 20

#    Then start the server (health-only in this phase):
docker compose up -d ml

# 3. Verify health from a Python one-liner inside the ml container
#    (grpcurl without reflection needs proto files — we'll skip that
#    until Phase 6 adds reflection). Tested output: "Health status: SERVING".
docker compose exec ml python -c "import grpc; from grpc_health.v1 import health_pb2, health_pb2_grpc; ch = grpc.insecure_channel('localhost:50051'); resp = health_pb2_grpc.HealthStub(ch).Check(health_pb2.HealthCheckRequest(service='')); print('Health status:', health_pb2.HealthCheckResponse.ServingStatus.Name(resp.status))"

# 4. Run the unit + contract test suite inside the built image.
docker compose run --rm ml pytest -ra -p no:cacheprovider
```

Expected:

- **Step 3** prints `Health status: SERVING`.
- **Step 4** reports `84 passed` (health RPC × 3, ModelState transitions × 6,
  anti look-ahead property × 14, validators × 6, forbidden-phrases × 25,
  sentiment monosource × 3, explain + freshness coverage).

Build was verified end-to-end on Mac arm64; image size ≈ 1.42 GB.

## Stop / reset

```powershell
docker compose down              # stop, keep volumes
docker compose down -v           # stop + wipe postgres + ml_models
```

## Directory map (inside the image / on the host)

```
ml_forecast/
├── src/ml_forecast/              # Python package (import ml_forecast.*)
│   ├── config.py                 # pydantic Settings (ML_* env prefix)
│   ├── main.py                   # gRPC server bootstrap
│   ├── api/                      # gRPC servicers
│   ├── domain/                   # immutable value objects
│   ├── models/                   # baselines + ARMAExo + LightGBM (WIP)
│   ├── features/                 # OHLCV + sentiment features, validators
│   ├── inference/                # freshness + explain + forbidden filter
│   ├── storage/                  # postgres, redis, artifact store
│   └── grpc_gen/                 # generated proto stubs (gitignored)
├── tests/
│   ├── unit/                     # domain + features + inference
│   └── contract/                 # gRPC contract tests
├── migrations/versions/          # Alembic schema migrations
├── proto/finzooka/ml/v1/         # gRPC contract (source of truth:
│                                 #   specs/001-ml-forecast-service/contracts/)
├── config/                       # forbidden_phrases.yaml, moex_holidays_*.yaml
├── Dockerfile                    # multistage, non-root runtime user
├── Makefile                      # proto / lint / typecheck / test
└── pyproject.toml                # deps + ruff + mypy config
```

## Development outside Docker (Mac / Linux)

```bash
cd ml_forecast
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
make proto           # generate src/ml_forecast/grpc_gen/
pytest -ra
```

## Making changes

- Edit proto → update `proto/finzooka/ml/v1/ml_forecast.proto` AND
  `specs/001-ml-forecast-service/contracts/ml_forecast.proto` (source
  of truth — keep them in sync). Then `make proto`.
- New DB column / table → add to `src/ml_forecast/storage/orm.py`
  and generate a migration via
  `alembic revision --autogenerate -m "your message"`.
- Forbidden phrases — edit `config/forbidden_phrases.yaml`; the
  service hot-reloads on SIGHUP (runbook lands in T097).
