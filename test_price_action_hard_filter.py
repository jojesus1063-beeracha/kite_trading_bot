from types import SimpleNamespace

from main import price_action_blocks_entry


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("PASS:", name)


paper_pa = SimpleNamespace(
    PAPER_TRADING=True,
    ENABLE_PRICE_ACTION=True,
)

paper_pa_off = SimpleNamespace(
    PAPER_TRADING=True,
    ENABLE_PRICE_ACTION=False,
)

live_pa = SimpleNamespace(
    PAPER_TRADING=False,
    ENABLE_PRICE_ACTION=True,
)


check(
    "PAPER + PA: positive score passes",
    price_action_blocks_entry(5, paper_pa) is False,
)

check(
    "PAPER + PA: score 1 passes",
    price_action_blocks_entry(1, paper_pa) is False,
)

check(
    "PAPER + PA: zero score blocks",
    price_action_blocks_entry(0, paper_pa) is True,
)

check(
    "PAPER + PA: negative score blocks",
    price_action_blocks_entry(-15, paper_pa) is True,
)

check(
    "PAPER with PA disabled does not hard-block",
    price_action_blocks_entry(-25, paper_pa_off) is False,
)

check(
    "LIVE mode is untouched even with PA enabled",
    price_action_blocks_entry(-25, live_pa) is False,
)

check(
    "Invalid PA score fails closed in PAPER hard-gate mode",
    price_action_blocks_entry(None, paper_pa) is True,
)

print()
print("Price Action hard-filter tests passed.")
