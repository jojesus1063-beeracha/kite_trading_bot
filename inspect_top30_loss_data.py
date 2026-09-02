#!/usr/bin/env python3

import json
from pathlib import Path
from collections import Counter, defaultdict

DATE = "2026-08-27"

SYMBOLS = {
    "OAL",
    "LICHSGFIN",
    "BHEL",
    "TDPOWERSYS",
    "DCBBANK",
    "CGCL",
    "VISL",
    "HCC",
}

print("=" * 100)
print("TOP-30 LOSS FORENSIC DATA INVENTORY")
print("=" * 100)

# --------------------------------------------------
# 1. Find likely JSON/JSONL files
# --------------------------------------------------

roots = [
    Path("runtime"),
    Path("."),
]

files = []

for root in roots:
    if not root.exists():
        continue

    for pattern in [
        "**/*.jsonl",
        "**/*.json",
        "**/*.jsonl.gz",
    ]:
        for p in root.glob(pattern):
            if p.is_file():
                files.append(p)

# dedupe
unique = []
seen = set()

for p in files:
    try:
        key = str(p.resolve())
    except Exception:
        key = str(p)

    if key not in seen:
        seen.add(key)
        unique.append(p)

print(f"\nFILES DISCOVERED = {len(unique)}")

# --------------------------------------------------
# 2. Search normal JSONL files for target symbols
# --------------------------------------------------

hits = defaultdict(Counter)
examples = defaultdict(list)

for p in unique:

    if p.suffix == ".gz":
        continue

    # Don't spend forever on unrelated huge files
    try:
        size = p.stat().st_size
    except Exception:
        continue

    if size == 0:
        continue

    if p.suffix == ".jsonl":

        try:
            with p.open(
                "r",
                errors="replace",
            ) as f:

                for line in f:

                    if not any(
                        symbol in line
                        for symbol in SYMBOLS
                    ):
                        continue

                    try:
                        x = json.loads(line)
                    except Exception:
                        continue

                    symbol = (
                        x.get("symbol")
                        or x.get("tradingsymbol")
                    )

                    if symbol not in SYMBOLS:
                        continue

                    event = (
                        x.get("event")
                        or x.get("event_type")
                        or x.get("type")
                        or x.get("status")
                        or "UNKNOWN"
                    )

                    hits[str(p)][
                        f"{symbol}:{event}"
                    ] += 1

                    if len(
                        examples[str(p)]
                    ) < 5:
                        examples[str(p)].append(x)

        except Exception as exc:
            print(
                "READ_ERROR",
                p,
                repr(exc),
            )

print()
print("=" * 100)
print("FILES CONTAINING TARGET TRADE DATA")
print("=" * 100)

if not hits:
    print("NO_TARGET_JSONL_HITS")
else:

    for filename, counts in hits.items():

        print()
        print(filename)

        for key, n in counts.most_common():
            print(
                f"  {key:<45} {n}"
            )

        print("  SAMPLE KEYS:")

        for sample in examples[filename]:
            print(
                "   ",
                sorted(sample.keys()),
            )

# --------------------------------------------------
# 3. Locate today's validation/audit files
# --------------------------------------------------

print()
print("=" * 100)
print("LIKELY FORENSIC FILES")
print("=" * 100)

keywords = [
    "validation",
    "audit",
    "trade",
    "entry",
    "depth",
    "pipeline",
    "position",
    "history",
]

for p in unique:

    name = str(p).lower()

    if DATE not in name and not any(
        k in name
        for k in keywords
    ):
        continue

    if any(
        k in name
        for k in keywords
    ):
        try:
            print(
                f"{p} | "
                f"{p.stat().st_size:,} bytes"
            )
        except Exception:
            pass

print()
print("=" * 100)
print("DONE")
print("=" * 100)

