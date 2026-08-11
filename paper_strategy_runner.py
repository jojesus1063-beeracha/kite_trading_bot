#!/usr/bin/env python3
"""Run the paper ADX/EMA/RSI entry model with the paper MFE/time exit overlay."""
import importlib

import config as cfg
from paper_contrarian_launcher import install_two_indicator_patch
from paper_mfe_time_exit import install as install_mfe_time_exit


def main():
    if not bool(getattr(cfg, "PAPER_TRADING", False)):
        raise SystemExit("SAFETY BLOCK: paper_strategy_runner requires PAPER_TRADING=True")

    # Patch strategy + legacy entry gates BEFORE importing main.py because
    # main.py binds several strategy/filter functions at import time.
    install_two_indicator_patch()

    main_module = importlib.import_module("main")
    install_mfe_time_exit(main_module, cfg)
    main_module.run()


if __name__ == "__main__":
    main()
