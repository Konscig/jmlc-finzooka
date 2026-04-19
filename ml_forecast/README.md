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
docker compose up --build -d postgres redis ml-migrate
#    ml-migrate exits after alembic upgrade head; check it succeeded:
docker compose logs ml-migrate | Select-Object -Last 20
#    Then start the server (health-only in this phase):
docker compose up -d ml

# 3. Verify health via gRPC from a temporary busybox container.
#    (Docker Desktop on Windows uses the same `docker` CLI as macOS/Linux.)
docker run --rm --network jmlc-finzooka_default fullstorydev/grpcurl:latest `
  -plaintext finzooka-ml:50051 grpc.health.v1.Health/Check

# 4. Run the unit + contract test suite inside the built image.
docker compose run --rm ml pytest -ra
```

Expected: `{ "status": "SERVING" }` from step 3 and green pytest
output from step 4 (health RPC + anti look-ahead + validators +
forbidden-phrases + ModelState transitions + sentiment monosource
guard — 6 test files, ~50 cases).

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
