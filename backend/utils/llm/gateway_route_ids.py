"""Lightweight identifiers shared by model configuration and gateway transport."""

from __future__ import annotations

LLM_GATEWAY_AUTO_LANE_PREFIX = 'omi:auto:'


def is_auto_lane_id(model_or_lane: object) -> bool:
    return isinstance(model_or_lane, str) and model_or_lane.startswith(LLM_GATEWAY_AUTO_LANE_PREFIX)
