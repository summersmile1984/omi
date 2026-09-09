"""Keep a user mutation's hashed arguments identical to its applied patch."""

from functools import wraps


def include_patch_arguments(original):
    @wraps(original)
    def wrapped(*args, build_patch, **kwargs):
        def complete_patch(item, now):
            logical, patch = build_patch(item, now)
            if 'arguments' in patch:
                logical = {**logical, 'arguments': patch['arguments']}
            return logical, patch

        return original(*args, build_patch=complete_patch, **kwargs)

    return wrapped
