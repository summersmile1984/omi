"""Map model admission failures to explicit HTTP outcomes before a response starts."""

from starlette.responses import JSONResponse

from .capabilities import CapabilityDisabled
from .local_llm import LLMInputRejected, LLMUnavailable


def install(app):
    async def disabled(request, error):
        return JSONResponse(status_code=503, content=error.detail())

    async def unavailable(request, error):
        return JSONResponse(status_code=503, content={'code': 'local_model_unavailable', 'retryable': True})

    async def invalid(request, error):
        return JSONResponse(status_code=422, content={'code': 'local_model_input_rejected', 'retryable': False})

    app.add_exception_handler(CapabilityDisabled, disabled)
    app.add_exception_handler(LLMUnavailable, unavailable)
    app.add_exception_handler(LLMInputRejected, invalid)
