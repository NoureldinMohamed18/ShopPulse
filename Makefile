.PHONY: install demo test lint api dashboard docker clean

install:
	pip install -r requirements-dev.txt && pip install -e .

demo:            ## seed + run + report, offline
	python -m shoppulse demo

test:
	pytest --cov=shoppulse --cov-report=term-missing

lint:
	ruff check .

api:
	python -m shoppulse api

dashboard:
	python -m shoppulse dashboard

docker:
	docker compose up --build

clean:
	rm -rf data .pytest_cache .ruff_cache .coverage
