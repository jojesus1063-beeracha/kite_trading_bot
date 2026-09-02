from main import market_alignment_blocks_entry

cases = {
    "STRONG_ALIGNMENT": False,
    "ALIGNED": False,
    "NEUTRAL": False,
    "UNKNOWN": False,
    "MISALIGNED": True,
    "STRONG_MISALIGNMENT": True,
}

failed = 0

for alignment, expected in cases.items():
    actual = market_alignment_blocks_entry(alignment)
    if actual == expected:
        print(f"PASS: {alignment} -> blocked={actual}")
    else:
        print(
            f"FAIL: {alignment} -> blocked={actual}, "
            f"expected={expected}"
        )
        failed += 1

print()
print(f"Results: {len(cases)-failed} passed, {failed} failed")

if failed:
    raise SystemExit(1)
