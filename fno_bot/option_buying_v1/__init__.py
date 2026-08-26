"""Isolated, PAPER-first NFO option-buying execution path.

The equity strategy produces only an underlying direction.  This package
owns contract resolution, affordability, option position state and exits.
It intentionally does not import the root equity ``config.py``, executor,
watchlist or position store.
"""

from .config import OptionBuyingConfig
from .engine import OptionBuyingEngine, UnderlyingSignal
from .resolver import OptionContractResolver, OptionRejection, ResolvedOption

__all__ = [
    "OptionBuyingConfig",
    "OptionBuyingEngine",
    "UnderlyingSignal",
    "OptionContractResolver",
    "OptionRejection",
    "ResolvedOption",
]
