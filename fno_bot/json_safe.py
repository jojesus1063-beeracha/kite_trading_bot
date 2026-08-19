"""
Shared helper to make dicts safe for json.dumps() when they may
contain numpy scalar types (numpy.int64, numpy.float64, numpy.bool_)
-- which frequently leak in from pandas/numpy operations and are NOT
natively JSON serializable, unlike Python's built-in int/float/bool.

Identical in behavior to the equity bot's json_safe.py. Duplicated
here (not imported cross-repo) so the two bots never share a runtime
dependency that could break one while changing the other -- see the
architecture review, Section B, on why these small modules are
copied rather than imported live.
"""
import numpy as np


def json_safe(obj):
    """Recursively converts numpy scalar types to native Python types
    within dicts/lists. Leaves everything else untouched."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj
