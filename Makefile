.PHONY: install migrate ingest run test check docker-up docker-down

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

migrate:
	.venv/bin/python manage.py migrate

ingest:
	.venv/bin/python manage.py ingest_logs

run:
	.venv/bin/python manage.py runserver

test:
	.venv/bin/python manage.py test

check:
	.venv/bin/python manage.py check
	.venv/bin/python manage.py makemigrations --check --dry-run

docker-up:
	docker compose up --build

docker-down:
	docker compose down

