"""Translate the canonical D1 write-lock denial without exposing storage errors."""

from fastapi.responses import JSONResponse


def memory_mutation_error(error: Exception, *, unavailable: str = 'memories unavailable', key: str = 'error'):
    if 'memory_locked_for_mutation' in str(error):
        return JSONResponse(
            (
                {'detail': 'A paid plan is required to access this memory.'}
                if key == 'detail'
                else {'error': 'A paid plan is required to access this memory.'}
            ),
            status_code=402,
        )
    return JSONResponse({'error': unavailable}, status_code=503)
