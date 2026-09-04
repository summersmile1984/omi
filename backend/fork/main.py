"""ASGI entrypoint: uvicorn fork.main:app. Admission runs before upstream import."""

from .bootstrap import Role, bootstrap

admission = bootstrap(Role.API)

from main import app  # noqa: E402,F401

if admission.target == 'self_hosted':
    from .auth_transport import install

    install(app)

    from .capability_transport import install as install_capabilities

    install_capabilities(app)
