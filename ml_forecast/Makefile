.PHONY: proto lint typecheck test smoke clean

PROTO_DIR := proto
GRPC_OUT  := src/ml_forecast/grpc_gen

proto:
	@mkdir -p $(GRPC_OUT)
	@touch $(GRPC_OUT)/__init__.py
	python -m grpc_tools.protoc \
		-I $(PROTO_DIR) \
		--python_out=$(GRPC_OUT) \
		--grpc_python_out=$(GRPC_OUT) \
		$(PROTO_DIR)/finzooka/ml/v1/ml_forecast.proto
	@# grpc_tools generates `from finzooka.ml.v1 import ml_forecast_pb2` imports,
	@# which don't resolve because we place them under src/ml_forecast/grpc_gen.
	@# Rewrite to a relative-style import that works from our package layout.
	@find $(GRPC_OUT)/finzooka -name "*.py" -exec sed -i.bak \
		-e 's/^from finzooka\.ml\.v1 import/from ml_forecast.grpc_gen.finzooka.ml.v1 import/' {} \; \
		&& find $(GRPC_OUT)/finzooka -name "*.bak" -delete
	@# Ensure __init__.py at every nested level so Python treats them as packages.
	@find $(GRPC_OUT)/finzooka -type d -exec touch {}/__init__.py \;

lint:
	ruff check src tests

typecheck:
	mypy src

test:
	pytest -ra

smoke:
	@# End-to-end local smoke: build, migrate, start, seed redis, check health.
	@# Runs from the repo root (uses the top-level docker-compose.yaml).
	@set -e; \
	cd .. && \
	DOCKER_BUILDKIT=1 docker compose build ml && \
	docker compose up -d postgres redis && \
	docker compose up ml-migrate && \
	docker compose up -d ml && \
	sleep 3 && \
	docker compose exec -T ml python -c "import grpc; from grpc_health.v1 import health_pb2, health_pb2_grpc; ch = grpc.insecure_channel('localhost:50051'); resp = health_pb2_grpc.HealthStub(ch).Check(health_pb2.HealthCheckRequest(service='')); assert resp.status == health_pb2.HealthCheckResponse.SERVING, 'health check did NOT report SERVING'; print('health OK')" && \
	docker compose run --rm --no-deps -T -e HOME=/tmp ml pytest -ra -p no:cacheprovider --rootdir=/app && \
	docker compose down && \
	echo "smoke: OK"

clean:
	rm -rf $(GRPC_OUT)/finzooka build dist *.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
