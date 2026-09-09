"""Load the ASGI application inside a request, after Worker bindings exist."""

# Keep framework initialization in deployment scope. Late FastAPI imports bypass
# the runtime's thread-free sync dependency handling (the JIT owner returned 503).
import fastapi  # noqa: F401
from workers import WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        from _worker_application import Default as Application

        return await Application.fetch(self, request)
