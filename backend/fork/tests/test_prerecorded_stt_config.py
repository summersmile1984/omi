"""Guards for the fork's prerecorded-STT constants.

`ForkPrerecordedSTTService` subclasses upstream's constant class so callers keep
one import. Subclassing is silent by nature: if upstream later defines one of
these names itself, the fork's value would quietly win and a runtime would route
to a provider upstream meant to handle differently. These tests turn that into a
failure on the sync that introduces it.
"""

from __future__ import annotations

import unittest

from config.prerecorded_stt import PrerecordedSTTService
from fork.prerecorded_stt_config import ForkPrerecordedSTTService

FORK_ONLY = ("SENSEVOICE", "MIMO", "MLX_MOSS_DIARIZE")


class ForkPrerecordedSTTServiceTests(unittest.TestCase):
    def test_fork_only_names_are_not_defined_upstream(self):
        collisions = [name for name in FORK_ONLY if hasattr(PrerecordedSTTService, name)]
        self.assertEqual(
            collisions,
            [],
            f"upstream now defines {collisions}; the fork subclass would silently shadow it. "
            f"Reconcile the value with upstream and drop the fork constant -- "
            f"do not keep both.",
        )

    def test_upstream_constants_still_resolve_through_the_subclass(self):
        # The reason callers can import one name instead of two.
        self.assertEqual(ForkPrerecordedSTTService.DEEPGRAM, PrerecordedSTTService.DEEPGRAM)
        self.assertEqual(ForkPrerecordedSTTService.MOSS, PrerecordedSTTService.MOSS)

    def test_fork_values_are_the_operator_facing_strings(self):
        # These are what operators put in STT_SERVICE_MODELS; renaming one is a
        # breaking config change, not a refactor.
        self.assertEqual(ForkPrerecordedSTTService.SENSEVOICE, "sensevoice")
        self.assertEqual(ForkPrerecordedSTTService.MIMO, "mimo")
        self.assertEqual(ForkPrerecordedSTTService.MLX_MOSS_DIARIZE, "mlx_moss_diarize")


if __name__ == "__main__":
    unittest.main(verbosity=2)
