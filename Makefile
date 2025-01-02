SHELL := /bin/bash
.SHELLFLAGS := -O extglob -eo pipefail -c
.DEFAULT_GOAL := html
.SUFFIXES:

.PHONY: sync_and_ingest
sync_and_ingest:
	git checkout scripts/telegram_messages_cache.json;
	git pull --rebase;
	python3 scripts/panzer_imgsync.py;
	python3 scripts/ingest_uploads.py;

	@if [ $$(git status --porcelain -- images/ | wc -l) -gt 0 ]; then \
		git add scripts/telegram_messages_cache.json; \
		git add images/; \
		git commit -m "update $(shell date --iso)"; \
		git push; \
	fi


.PHONY: debug_ingest
debug_ingest:
# 	touch images/*/*/*.json
	touch images/2024/06/*.json
	python3 scripts/ingest_uploads.py
	ls -lh images/2024/*/thumbnails.jpg


index.html: templates/*
	python3 scripts/gen_html.py index.html

media.html: templates/*
	python3 scripts/gen_html.py media.html

.PHONY: html
html: index.html media.html


.PHONY: serve
serve:
	python3 -m http.server 8080


.PHONY: watch
watch:
	watch --interval 7200 -c 'make sync_and_ingest 2>>sync.log >> sync.log'
