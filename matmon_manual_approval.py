from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import config as cfg


RUNTIME = Path("runtime/matmon_manual")
PENDING = RUNTIME / "pending_proposal.json"
AUDIT = RUNTIME / "approval_audit.jsonl"


@dataclass
class TradeProposal:
    proposal_id: str
    created_at: float
    expires_at: float
    symbol: str
    side: str
    quantity: int
    entry: float
    stop_loss: float
    target: float
    expected_risk: float
    status: str = "PENDING"


def _ensure_runtime():
    RUNTIME.mkdir(parents=True, exist_ok=True)


def _audit(event: str, proposal: Optional[TradeProposal], **extra):
    _ensure_runtime()
    row = {
        "timestamp": time.time(),
        "event": event,
        "proposal": asdict(proposal) if proposal else None,
        **extra,
    }
    with AUDIT.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def create_proposal(
    symbol: str,
    side: str,
    quantity: int,
    entry: float,
    stop_loss: float,
    target: float,
    expected_risk: float,
) -> TradeProposal:

    if getattr(cfg, "MATMON_EXECUTION_MODE", "PAPER") != "MANUAL":
        raise RuntimeError("Matmon manual approval mode is not enabled")

    if not getattr(cfg, "MATMON_MANUAL_APPROVAL_REQUIRED", True):
        raise RuntimeError("Manual approval must remain enabled")

    if getattr(cfg, "MATMON_AUTO_EXECUTION_ENABLED", False):
        raise RuntimeError("Automatic execution must remain disabled")

    existing = load_pending()
    if existing and existing.status == "PENDING" and not is_expired(existing):
        raise RuntimeError(
            f"Pending proposal already exists: {existing.proposal_id}"
        )

    now = time.time()
    timeout = int(
        getattr(cfg, "MATMON_APPROVAL_TIMEOUT_SECONDS", 45)
    )

    proposal = TradeProposal(
        proposal_id=str(uuid.uuid4()),
        created_at=now,
        expires_at=now + timeout,
        symbol=str(symbol),
        side=str(side).upper(),
        quantity=int(quantity),
        entry=float(entry),
        stop_loss=float(stop_loss),
        target=float(target),
        expected_risk=float(expected_risk),
    )

    _ensure_runtime()
    PENDING.write_text(json.dumps(asdict(proposal), indent=2))
    _audit("CREATED", proposal)

    return proposal


def load_pending() -> Optional[TradeProposal]:
    if not PENDING.exists():
        return None

    data = json.loads(PENDING.read_text())
    return TradeProposal(**data)


def save(proposal: TradeProposal):
    _ensure_runtime()
    PENDING.write_text(json.dumps(asdict(proposal), indent=2))


def is_expired(proposal: TradeProposal) -> bool:
    return time.time() > proposal.expires_at


def approve(proposal_id: str) -> TradeProposal:
    proposal = load_pending()

    if proposal is None:
        raise RuntimeError("No pending Matmon proposal")

    if proposal.proposal_id != proposal_id:
        raise RuntimeError("Proposal ID mismatch")

    if proposal.status != "PENDING":
        raise RuntimeError(
            f"Proposal is already {proposal.status}"
        )

    if is_expired(proposal):
        proposal.status = "EXPIRED"
        save(proposal)
        _audit("EXPIRED", proposal)
        raise RuntimeError("Proposal expired before approval")

    proposal.status = "APPROVED"
    save(proposal)
    _audit("APPROVED", proposal)

    return proposal


def reject(proposal_id: str) -> TradeProposal:
    proposal = load_pending()

    if proposal is None:
        raise RuntimeError("No pending Matmon proposal")

    if proposal.proposal_id != proposal_id:
        raise RuntimeError("Proposal ID mismatch")

    if proposal.status != "PENDING":
        raise RuntimeError(
            f"Proposal is already {proposal.status}"
        )

    proposal.status = "REJECTED"
    save(proposal)
    _audit("REJECTED", proposal)

    return proposal


def consume_approval(proposal_id: str) -> TradeProposal:
    proposal = load_pending()

    if proposal is None:
        raise RuntimeError("No Matmon proposal")

    if proposal.proposal_id != proposal_id:
        raise RuntimeError("Proposal ID mismatch")

    if proposal.status != "APPROVED":
        raise RuntimeError(
            "Broker execution blocked: proposal not approved"
        )

    if is_expired(proposal):
        proposal.status = "EXPIRED"
        save(proposal)
        _audit("EXPIRED_AFTER_APPROVAL", proposal)
        raise RuntimeError("Approval expired")

    proposal.status = "CONSUMED"
    save(proposal)
    _audit("CONSUMED", proposal)

    return proposal
