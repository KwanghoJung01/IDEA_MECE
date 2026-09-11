"""WSGI 진입점 (gunicorn 등 운영 서버용)."""
from app import app

__all__ = ["app"]
