import importlib
import os

import pytest


def _reload_cfg():
    import fno_bot.config as cfg
    importlib.reload(cfg)
    return cfg


def test_live_requires_exact_ack_string(monkeypatch):
    monkeypatch.setenv("FNO_MODE", "LIVE")
    monkeypatch.delenv("FNO_LIVE_ACK", raising=False)
    cfg = _reload_cfg()
    with pytest.raises(RuntimeError, match="REFUSING TO START LIVE TRADING"):
        cfg.validate_mode()


def test_live_rejects_wrong_ack_value(monkeypatch):
    monkeypatch.setenv("FNO_MODE", "LIVE")
    monkeypatch.setenv("FNO_LIVE_ACK", "yes please")
    cfg = _reload_cfg()
    with pytest.raises(RuntimeError):
        cfg.validate_mode()


def test_live_accepts_exact_ack_value(monkeypatch):
    monkeypatch.setenv("FNO_MODE", "LIVE")
    monkeypatch.setenv("FNO_LIVE_ACK", "I_ACCEPT_REAL_FNO_ORDERS")
    cfg = _reload_cfg()
    cfg.validate_mode()  # must not raise


def test_shadow_and_paper_never_require_ack(monkeypatch):
    monkeypatch.delenv("FNO_LIVE_ACK", raising=False)
    for mode in ("SHADOW", "PAPER"):
        monkeypatch.setenv("FNO_MODE", mode)
        cfg = _reload_cfg()
        cfg.validate_mode()  # must not raise


def test_invalid_mode_rejected(monkeypatch):
    monkeypatch.setenv("FNO_MODE", "YOLO")
    cfg = _reload_cfg()
    with pytest.raises(RuntimeError, match="Invalid FNO_MODE"):
        cfg.validate_mode()


def test_default_mode_is_shadow(monkeypatch):
    monkeypatch.delenv("FNO_MODE", raising=False)
    cfg = _reload_cfg()
    assert cfg.MODE == "SHADOW"


def test_authorized_signal_defaults_to_none(monkeypatch):
    monkeypatch.delenv("FNO_MODE", raising=False)
    cfg = _reload_cfg()
    assert cfg.AUTHORIZED_SIGNAL is None
