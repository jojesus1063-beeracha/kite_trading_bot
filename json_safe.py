"""
Shared helper to make dicts safe for json.dumps() when they may
contain numpy scalar types (numpy.int64, numpy.float64, numpy.bool_)
-- which frequently leak in from pandas/numpy operations (e.g.
df["high"].values, df.iloc[-1]["close"]) and are NOT natively JSON
serializable, unlike Python's built-in int/float/bool.

Real incident this caught: price_action.py's swing-point detection
stores raw numpy scalars (from df["high"].values) in its returned
support/resistance levels; these flowed into signal.price_action_detail
and then into record_trade()/log_signal(), crashing json.dumps() with
'Object of type int64 is not JSON serializable' -- and because the
crash happened mid-function, the position removal code after it never
ran, leaving open_positions.json in a stuck, partially-updated state.
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
