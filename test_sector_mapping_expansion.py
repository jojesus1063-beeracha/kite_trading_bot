"""Regression checks for the conservative August 2026 sector-map expansion."""

from market_trend import (
    SECTOR_INDEX_TOKENS,
    SECTOR_MAP,
    sector_for_symbol,
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


CURRENT_WATCHLIST = [
    "SCI", "GODFRYPHLP", "SIEMENS", "PREMIERENE", "ZENTEC", "DOMS",
    "ACUTAAS", "ENRIN", "CYIENT", "GMDCLTD", "KIRLOSENG", "TARIL",
    "TATAINVEST", "MMTC", "TRIDENT", "SWANCORP", "ANANDRATHI",
    "SPLPETRO", "PINELABS", "LICI", "SAIL", "UPL", "NATIONALUM",
    "KALYANKJIL", "ZEEL", "ATHERENERG", "URBANCO", "IREDA",
    "REDINGTON", "CGPOWER", "PAYTM", "DLF", "GESHIP", "POWERGRID",
    "CHENNPETRO", "JAINREC", "TITAN", "HINDZINC", "NETWEB",
    "FEDERALBNK", "DATAPATTNS", "RKFORGE", "DABUR", "DEEPAKFERT",
    "EXIDEIND", "GROWW", "BALRAMCHIN", "SUZLON", "HFCL", "CAMS",
    "GRASIM", "CDSL", "NAM-INDIA", "M&MFIN", "NYKAA", "KIMS",
    "BATAINDIA", "TRITURBINE", "INFY", "GABRIEL", "VEDL",
    "HBLENGINE", "INDGN", "PNBHOUSING", "360ONE", "MCX", "PRESTIGE",
    "SWIGGY", "LTFOODS", "JSWINFRA", "MEESHO", "MANAPPURAM",
    "DRREDDY", "BLUEJET", "CEMPRO", "GRAVITA", "JWL", "TORNTPOWER",
    "PARADEEP", "LALPATHLAB",
]

EXPECTED_ADDITIONS = {
    "FEDERALBNK": "NIFTY BANK",
    "CYIENT": "NIFTY IT",
    "NETWEB": "NIFTY IT",
    "ATHERENERG": "NIFTY AUTO",
    "RKFORGE": "NIFTY AUTO",
    "EXIDEIND": "NIFTY AUTO",
    "GABRIEL": "NIFTY AUTO",
    "GMDCLTD": "NIFTY METAL",
    "SAIL": "NIFTY METAL",
    "NATIONALUM": "NIFTY METAL",
    "JAINREC": "NIFTY METAL",
    "HINDZINC": "NIFTY METAL",
    "VEDL": "NIFTY METAL",
    "GRAVITA": "NIFTY METAL",
    "CHENNPETRO": "NIFTY ENERGY",
    "TORNTPOWER": "NIFTY ENERGY",
    "GODFRYPHLP": "NIFTY FMCG",
    "DOMS": "NIFTY FMCG",
    "DABUR": "NIFTY FMCG",
    "BALRAMCHIN": "NIFTY FMCG",
    "LTFOODS": "NIFTY FMCG",
    "DLF": "NIFTY REALTY",
    "PRESTIGE": "NIFTY REALTY",
    "ZEEL": "NIFTY MEDIA",
    "TATAINVEST": "NIFTY FIN SERVICE",
    "ANANDRATHI": "NIFTY FIN SERVICE",
    "PINELABS": "NIFTY FIN SERVICE",
    "LICI": "NIFTY FIN SERVICE",
    "PAYTM": "NIFTY FIN SERVICE",
    "GROWW": "NIFTY FIN SERVICE",
    "CAMS": "NIFTY FIN SERVICE",
    "CDSL": "NIFTY FIN SERVICE",
    "NAM-INDIA": "NIFTY FIN SERVICE",
    "M&MFIN": "NIFTY FIN SERVICE",
    "PNBHOUSING": "NIFTY FIN SERVICE",
    "360ONE": "NIFTY FIN SERVICE",
    "MCX": "NIFTY FIN SERVICE",
    "MANAPPURAM": "NIFTY FIN SERVICE",
    "SCI": "NIFTY INFRA",
    "GESHIP": "NIFTY INFRA",
    "JSWINFRA": "NIFTY INFRA",
    "HFCL": "NIFTY INFRA",
    "JWL": "NIFTY INFRA",
}

DELIBERATELY_UNMAPPED = {
    # No broker-verified Capital Goods token in the bot.
    "SIEMENS", "PREMIERENE", "ZENTEC", "ENRIN", "KIRLOSENG", "TARIL",
    "CGPOWER", "DATAPATTNS", "SUZLON", "TRITURBINE", "HBLENGINE",
    # Healthcare is not automatically treated as Pharma.
    "ACUTAAS", "KIMS", "INDGN", "BLUEJET", "LALPATHLAB",
    # No matching token for these official Nifty 500 industry groups.
    "MMTC", "TRIDENT", "SWANCORP", "SPLPETRO", "UPL", "KALYANKJIL",
    "URBANCO", "REDINGTON", "DEEPAKFERT", "GRASIM", "NYKAA",
    "BATAINDIA", "SWIGGY", "MEESHO", "CEMPRO", "PARADEEP", "TITAN",
}


check(
    "All 43 conservative additions map to the intended sector",
    all(
        sector_for_symbol(symbol) == sector
        for symbol, sector in EXPECTED_ADDITIONS.items()
    ),
)

check(
    "Every mapped sector has a broker-verified index token",
    all(
        sector in SECTOR_INDEX_TOKENS
        for sector in SECTOR_MAP.values()
    ),
)

mapped = {
    symbol
    for symbol in CURRENT_WATCHLIST
    if sector_for_symbol(symbol) is not None
}

check(
    "Current watchlist coverage expands from 4/80 to 47/80",
    len(CURRENT_WATCHLIST) == 80
    and len(mapped) == 47,
)

check(
    "Current watchlist coverage is 58.75 percent",
    round(100 * len(mapped) / len(CURRENT_WATCHLIST), 2) == 58.75,
)

check(
    "Unsupported industries remain explicitly unmapped",
    len(DELIBERATELY_UNMAPPED) == 33
    and mapped.isdisjoint(DELIBERATELY_UNMAPPED)
    and all(
        sector_for_symbol(symbol) is None
        for symbol in DELIBERATELY_UNMAPPED
    ),
)

check(
    "The watchlist partitions cleanly into 47 mapped and 33 unmapped symbols",
    mapped | DELIBERATELY_UNMAPPED == set(CURRENT_WATCHLIST),
)

print()
print("SECTOR MAPPING EXPANSION TESTS PASSED")
