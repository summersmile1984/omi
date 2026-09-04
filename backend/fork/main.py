"""ASGI entrypoint: uvicorn fork.main:app. Admission runs before upstream import."""

from .bootstrap import Role, bootstrap

bootstrap(Role.API)

from main import app  # noqa: E402,F401
