"""sys.modules registration: make ``google.cloud.firestore`` resolve to firestore_pg.

Importing this module rewrites the module registry so that business code's
existing imports work unchanged:

    from google.cloud import firestore            -> firestore_pg (Client, transactional, ArrayUnion, ...)
    from google.cloud.firestore import FieldFilter -> FieldFilter
    from google.cloud.firestore_v1 import FieldFilter -> FieldFilter

Call ``install()`` at process start (before business modules import the SDK).
"""

from __future__ import annotations

import sys
import types
from typing import Any

from . import (  # noqa: F401
    DELETE_FIELD,
    SERVER_TIMESTAMP,
    ArrayRemove,
    ArrayUnion,
    FieldFilter,
    Increment,
)
from .client import (
    Client,
    CollectionReference,
    DocumentReference,
    DocumentSnapshot,
    Query,
    QuerySnapshot,
    Transaction,
    transactional,
)

# ---------------------------------------------------------------------------
# google.cloud.firestore facade
# ---------------------------------------------------------------------------


class _FirestoreModule(types.ModuleType):
    """Module object served under ``google.cloud.firestore``.

    A real ``types.ModuleType`` (not a plain object) so importlib machinery
    (``__spec__``/``__loader__``/``__path__`` lookups) keeps working.
    """

    Client = Client
    CollectionReference = CollectionReference
    DocumentReference = DocumentReference
    DocumentSnapshot = DocumentSnapshot
    Query = Query
    QuerySnapshot = QuerySnapshot
    Transaction = Transaction
    transactional = staticmethod(transactional)
    FieldFilter = FieldFilter
    Increment = Increment
    ArrayUnion = ArrayUnion
    ArrayRemove = ArrayRemove
    DELETE_FIELD = DELETE_FIELD
    SERVER_TIMESTAMP = SERVER_TIMESTAMP

    def __getattr__(self, name: str) -> Any:
        # Forward any other attribute to the installed google.cloud.firestore
        # module if it exists (e.g. BaseCompositeFilter, FieldPath, exceptions).
        # Submodule attributes (base_query.BaseCompositeFilter) live in the
        # real submodule, captured in install() as ``_real_base_query``.
        candidates = [
            sys.modules.get("google.cloud.firestore._real"),
            getattr(self, "_real_v1", None),
            getattr(self, "_real_base_query", None),
        ]
        for mod in candidates:
            if mod is not None and hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(f"firestore_pg: no attribute {name!r}")


_installed = False


def install() -> None:
    """Rewrite sys.modules entries for google.cloud.firestore(-v1).

    Uses hard assignment (not setdefault) so the facade wins even when the real
    SDK was imported earlier (``_client.py`` imports it at module top). The real
    package is preserved under ``google.cloud.firestore._real`` so ``__getattr__``
    can forward rarely used pieces, and ``__path__`` is copied so submodule
    imports (``google.cloud.firestore_v1.transforms``) keep working.
    """
    global _installed
    if _installed:
        return
    _installed = True

    facade = _FirestoreModule("google.cloud.firestore")
    facade.__package__ = "google.cloud"

    real = sys.modules.get("google.cloud.firestore")
    real_v1 = sys.modules.get("google.cloud.firestore_v1")
    if real is not None and real is not facade:
        sys.modules["google.cloud.firestore._real"] = real
    if real is not None and hasattr(real, "__path__"):
        facade.__path__ = list(real.__path__)
    elif real_v1 is not None and hasattr(real_v1, "__path__"):
        facade.__path__ = list(real_v1.__path__)
    if real is not None:
        facade.__spec__ = getattr(real, "__spec__", None)
        facade.__loader__ = getattr(real, "__loader__", None)
        facade.__file__ = getattr(real, "__file__", None)
    facade._real_v1 = real_v1

    # Capture the real base_query submodule before shadowing it, so
    # ``from google.cloud.firestore_v1.base_query import BaseCompositeFilter``
    # can be forwarded by the facade.
    if real_v1 is not None:
        try:
            import importlib

            facade._real_base_query = importlib.import_module("google.cloud.firestore_v1.base_query")
        except Exception:  # pragma: no cover - submodule not present
            facade._real_base_query = None

    sys.modules["google.cloud.firestore"] = facade
    sys.modules["google.cloud.firestore_v1"] = facade

    # Also alias submodule imports used by the codebase:
    #   from google.cloud.firestore import FieldFilter
    #   from google.cloud.firestore_v1 import FieldFilter
    sys.modules["google.cloud.firestore._base_query"] = facade
    sys.modules["google.cloud.firestore_v1.base_query"] = facade
