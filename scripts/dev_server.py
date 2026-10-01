"""Concurrent, gzip-enabled static development server; run with `make serve`."""

from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).resolve().parents[1]

app = GZipMiddleware(
    Starlette(routes=[Mount("/", app=StaticFiles(directory=ROOT_DIR, html=True))]),
    minimum_size=1024,
    compresslevel=6,
)
