SHELL := /bin/bash
.SHELLFLAGS := -O extglob -eo pipefail -c
.DEFAULT_GOAL := html
.SUFFIXES:

.PHONY: sync_and_ingest
sync_and_ingest:
	uv run --script scripts/panzer_imgsync.py $(SYNC_ARGS)


.PHONY: thumbnails
thumbnails:
	uv run --script scripts/generate_thumbnails.py


index.html: templates/*
	uv run --script scripts/gen_html.py index.html

media.html: templates/*
	uv run --script scripts/gen_html.py media.html

.PHONY: classification-index
classification-index:
	uv run --script scripts/export_classifications.py

.PHONY: html
html: classification-index index.html media.html


.PHONY: serve
serve:
	uvx --from uvicorn --with starlette uvicorn scripts.dev_server:app --host 0.0.0.0 --port 8082


.PHONY: watch
watch:
	watch --interval 7200 -c 'make sync_and_ingest 2>>sync.log >> sync.log;tail -n 80 sync.log'
