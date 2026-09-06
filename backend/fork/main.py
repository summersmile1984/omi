"""ASGI entrypoint: uvicorn fork.main:app. Admission runs before upstream import."""

from .bootstrap import Role, bootstrap

admission = bootstrap(Role.API)

from main import app  # noqa: E402,F401

if admission.target == 'self_hosted':
    from .auth_transport import install

    install(app)

    from .brand_transport import install as install_brand

    install_brand(app)

    from .referral_transport import install as install_referrals

    install_referrals(app)

    from .capability_transport import install as install_capabilities

    from .profile import current

    row = current()
    install_capabilities(app, row)
    from .llm_transport import install as install_llm

    install_llm(app)
    if row.get('speech') or row.get('operator_ai'):
        from .speech_transport import install as install_speech

        install_speech(app)
