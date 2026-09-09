"""Typed mutation admission for the selected deployment, before SQL effects.

Ordinary PostgreSQL compatibility mode has no account lifecycle policy. The
self-hosted entrypoint binds its deletion authority before workload import.
Every facade write, including transactions and batches, uses this one boundary.
"""

from dataclasses import dataclass
from typing import Mapping, Protocol

from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class DocumentAddress:
    collection: str
    table: str
    namespace: str
    document_id: str


class WritePolicy(Protocol):
    def lock(self, conn: Connection, address: DocumentAddress) -> None: ...

    def admit(self, conn: Connection, address: DocumentAddress, data: Mapping, *, merge: bool = False) -> None: ...


class UnrestrictedWrites:
    def lock(self, conn, address):
        pass

    def admit(self, conn, address, data, *, merge=False):
        pass


policy: WritePolicy = UnrestrictedWrites()
