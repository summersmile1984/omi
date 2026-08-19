"""MOSS pipeline package: transcribe + diarize (MOSS API) + identify (CPU).

Chain:
  MOSS moss-transcribe-diarize -> S01/S02 segments -> speaker clip slicing
  -> wespeaker embedding (CPU) -> people cosine match -> person_id.

No GPU required (MOSS does the heavy lifting server-side; identification
runs on CPU embeddings).
"""
