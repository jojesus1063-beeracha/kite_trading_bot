from pathlib import Path
import re

source = Path("main.py").read_text(
    encoding="utf-8",
)


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


check(
    "Confirmed-fill helper remains active",
    "fixed_levels_from_fill" in source,
)

check(
    "Position stores target_price",
    bool(
        re.search(
            r'["\']target["\']\s*:\s*target_price',
            source,
        )
    ),
)

check(
    "Position does not store signal.target_price",
    not bool(
        re.search(
            r'["\']target["\']\s*:\s*'
            r'signal\.target_price',
            source,
        )
    ),
)

check(
    "Entry log does not show stale signal target",
    "target={signal.target_price:" not in source,
)

print()
print(
    "Confirmed-fill target storage tests passed."
)
