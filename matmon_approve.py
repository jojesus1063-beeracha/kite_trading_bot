import sys
from matmon_manual_approval import load_pending, approve, reject, is_expired


p = load_pending()

if not p:
    print("MATMON: no pending proposal")
    raise SystemExit(1)

print()
print("===== MATMON TRADE PROPOSAL =====")
print("Proposal:", p.proposal_id)
print("Symbol:", p.symbol)
print("Side:", p.side)
print("Quantity:", p.quantity)
print("Entry:", p.entry)
print("Stop:", p.stop_loss)
print("Target:", p.target)
print("Expected risk:", p.expected_risk)
print("Status:", p.status)
print("Expired:", is_expired(p))
print()

if len(sys.argv) < 2:
    print("Usage:")
    print(" python3 matmon_approve.py APPROVE")
    print(" python3 matmon_approve.py REJECT")
    raise SystemExit(0)

action = sys.argv[1].upper()

if action == "APPROVE":
    result = approve(p.proposal_id)
    print("MATMON_APPROVAL=APPROVED")
    print("Proposal:", result.proposal_id)

elif action == "REJECT":
    result = reject(p.proposal_id)
    print("MATMON_APPROVAL=REJECTED")
    print("Proposal:", result.proposal_id)

else:
    raise SystemExit("Use APPROVE or REJECT")
