"""
ADX confidence tiering — an additive, opt-in alternative to the
existing binary USE_ADX_FILTER gate.

Does NOT replace or modify the existing filter. cfg.ADX_MODE controls
which behavior is active:

  "off"     -> no ADX gating at all (matches USE_ADX_FILTER=False)
  "binary"  -> existing pass/fail threshold behavior (matches USE_ADX_FILTER=True)
  "dynamic" -> tiered confidence labels, all tiers except REJECTED allow the trade

Default (when ADX_MODE isn't set) mirrors the existing USE_ADX_FILTER
flag exactly, so nothing changes for anyone not opting in.
"""
from typing import Optional
import pandas as pd


def resolve_adx_mode(cfg) -> str:
    explicit = getattr(cfg, "ADX_MODE", None)
    if explicit:
        return explicit.lower()
    return "binary" if getattr(cfg, "USE_ADX_FILTER", False) else "off"


def adx_confidence(adx_value, cfg) -> Optional[str]:
    """
    Returns None (mode is "off"), or one of:
    "REJECTED", "CONFIRMED" (binary mode),
    "REJECTED", "MEDIUM", "HIGH", "VERY_STRONG" (dynamic mode).
    """
    mode = resolve_adx_mode(cfg)

    if mode == "off":
        return None

    if pd.isna(adx_value):
        return "REJECTED"

    if mode == "binary":
        threshold = getattr(cfg, "ADX_THRESHOLD", 25)
        return "CONFIRMED" if adx_value >= threshold else "REJECTED"

    if mode == "dynamic":
        medium_min = getattr(cfg, "ADX_DYNAMIC_MIN", 20)
        high_min = getattr(cfg, "ADX_THRESHOLD", 25)
        strong_min = getattr(cfg, "ADX_DYNAMIC_STRONG", 35)
        if adx_value < medium_min:
            return "REJECTED"
        if adx_value < high_min:
            return "MEDIUM"
        if adx_value < strong_min:
            return "HIGH"
        return "VERY_STRONG"

    return None
