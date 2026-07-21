"""
A curated list of commonly-traded NSE cash-market stocks, used to
populate the watchlist selector in configure_app.py.

This is NOT a live index membership list (e.g. "Nifty 50") — index
constituents change over time and this file isn't kept in sync with
that. It's just a convenient starting set of liquid, well-known
stocks. Anything not listed here can still be added manually via the
"Add another symbol" field in the dashboard.
"""

STOCK_UNIVERSE = sorted([
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN",
    "ULTRACEMCO", "WIPRO", "NESTLEIND", "HCLTECH", "ONGC", "NTPC",
    "POWERGRID", "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "ADANIPORTS",
    "ADANIENT", "COALINDIA", "BAJAJFINSV", "DRREDDY", "DIVISLAB",
    "GRASIM", "HEROMOTOCO", "HINDALCO", "INDUSINDBK", "BRITANNIA",
    "CIPLA", "EICHERMOT", "BPCL", "TECHM", "UPL", "SBILIFE", "HDFCLIFE",
    "SHREECEM", "APOLLOHOSP", "BAJAJ-AUTO", "TATACONSUM", "M&M",
    "VEDL", "ZOMATO", "DMART", "IRCTC", "PIDILITIND", "DABUR",
    "GODREJCP", "SIEMENS", "AMBUJACEM", "BANDHANBNK",
])
