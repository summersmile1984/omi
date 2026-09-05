# Canonical-memory PostgreSQL v6 verification

Integrated source: `9f8b688209` (`d68458a944` before integration).

Schema v6 freezes twelve collection IDs reached through the production
`MemoryCollections` owner, including `memory_deletion_receipts`. Earlier v1–v5
inventories and physical mappings are unchanged. The inventory guard now derives
the serving memory collection paths from that typed owner, so adding a future
path fails until a new explicit schema version is added.

The focused formal backend lane passed 29 fork/startup checks. Its PostgreSQL
module was correctly skipped locally because no DSN was configured. The same
two new PostgreSQL tests then ran inside the isolated standard-Linux test
network: a v5 ledger upgraded to exact versions 1–6 while preserving an existing
row and all twelve hashed mappings; the production
`replace_conversation_source_firestore` transaction read the privacy receipt and
committed a replacement candidate. The result was two passed tests in 0.96s.

Evidence is `/tmp/memweft-implementation/server/llm/pg-v6-transaction-exact-container.log`.
The test network and credentials are isolated; no production deployment or
customer data was used.

This admits the authoritative memory transaction. It does not prove a complete
self-hosted retrieval loop: the standard Compose target still has no canonical
memory outbox/maintenance consumer, so outbox drain, Qdrant/keyword projection
and `search_memories` require their own implementation and runtime evidence.
