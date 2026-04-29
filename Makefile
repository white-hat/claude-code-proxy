.PHONY: venv lint format check build up down

venv:
	uv sync

lint:
	uv run ty check
	uv run ruff check proxy.py

format:
	uv run ruff format proxy.py

check: lint format

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down
