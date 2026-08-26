"""Process entry point for the PAPER-only option-buying v1 runner."""
import logging
import os
from pathlib import Path

from fno_bot.audit.event_log import log_event
from .config import OptionBuyingConfig
from .orchestrator import OptionBuyingPaperOrchestrator


def build_kite(api_key: str, access_token: str):
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = OptionBuyingConfig()
    config.validate()
    api_key = os.environ.get("KITE_API_KEY", "").strip()
    if not api_key or api_key == "your_api_key_here":
        raise RuntimeError("KITE_API_KEY is required")
    token_path = Path(config.access_token_file)
    try:
        access_token = token_path.read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read access token file {token_path}: {exc}") from exc
    if not access_token:
        raise RuntimeError("access token file is empty")

    kite = build_kite(api_key, access_token)
    # Read-only REST validation before opening the socket. No order method is
    # passed into the orchestration/engine layer.
    kite.profile()
    runner = OptionBuyingPaperOrchestrator(
        kite=kite, api_key=api_key, access_token=access_token,
        config=config, audit_fn=log_event,
    )
    runner.run_forever()


if __name__ == "__main__":
    main()
