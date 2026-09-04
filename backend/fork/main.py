"""ASGI entrypoint: uvicorn fork.main:app. Admission runs before upstream import."""

from .bootstrap import Role, bootstrap

admission = bootstrap(Role.API)

from main import app  # noqa: E402,F401

if admission.target == 'self_hosted':
    from .auth_transport import install

    install(app)

    from .capability_transport import install as install_capabilities

    from .profile import current

    row = current()
    install_capabilities(app, row)
    from .llm_transport import install as install_llm

    install_llm(app)
    if row.get('speech'):
        from .speech_transport import install as install_speech

        install_speech(app)
