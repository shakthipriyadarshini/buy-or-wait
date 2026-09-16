"""Gunicorn entrypoint: `gunicorn --chdir code wsgi:app`.

Kept as its own tiny module (rather than pointing gunicorn at api:app)
because the dataset must be loaded before the first request is served, and
`api.app` on its own is an un-loaded Flask instance. create_app() does the
load and hands back the configured app.
"""
from api import create_app

app = create_app()
