"""Guards for fork-owned MLX diarization configuration."""

from __future__ import annotations

import unittest

from config.prerecorded_stt import PrerecordedSTTConfigurationError
from fork.prerecorded_stt_config import ForkPrerecordedSTTService, get_mlx_moss_diarize_config


class ForkPrerecordedSTTServiceTests(unittest.TestCase):
    def test_missing_endpoint_is_a_typed_configuration_error(self):
        with self.assertRaises(PrerecordedSTTConfigurationError) as error:
            get_mlx_moss_diarize_config({"MLX_MOSS_DIARIZE_MODEL": "operator-model"})
        self.assertEqual(error.exception.provider, ForkPrerecordedSTTService.MLX_MOSS_DIARIZE)
        self.assertEqual(error.exception.missing_env, "MLX_MOSS_DIARIZE_ENDPOINT")

    def test_private_mlx_endpoint_is_admitted_with_model_and_key(self):
        value = get_mlx_moss_diarize_config(
            {
                "MLX_MOSS_DIARIZE_ENDPOINT": "http://mlx-audio:5002/v1/audio/transcriptions",
                "MLX_MOSS_DIARIZE_MODEL": "operator-model",
                "MLX_MOSS_DIARIZE_API_KEY": "operator-key",
            }
        )
        self.assertEqual(value.endpoint, "http://mlx-audio:5002/v1/audio/transcriptions")
        self.assertEqual(value.model, "operator-model")
        self.assertEqual(value.api_key, "operator-key")
        self.assertEqual(ForkPrerecordedSTTService.MLX_MOSS_DIARIZE, "mlx_moss_diarize")


if __name__ == "__main__":
    unittest.main(verbosity=2)
