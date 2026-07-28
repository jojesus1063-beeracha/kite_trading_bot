"""
One-time setup for TOTP-based two-factor authentication on the
dashboard login (configure_app.py). Run this once from the terminal:

    python3 setup_2fa.py

It generates:
  - A random TOTP secret
  - A QR code image (totp_qrcode.png) you can view and scan with
    Google Authenticator, Authy, or any standard TOTP app
  - 5 one-time backup codes, printed to the terminal (write these
    down somewhere safe -- they won't be shown again)

Everything gets saved to totp_config.json. Re-running this script
generates a NEW secret and backup codes, invalidating the old ones --
useful if you lose your phone and need to re-enroll.

This script does NOT modify configure_app.py or touch the live
dashboard at all -- it's a standalone setup step. 2FA only becomes
active once configure_app.py is updated separately to check against
totp_config.json (a later step).
"""

import json
import os
import secrets
import string

import pyotp
import qrcode

TOTP_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "totp_config.json")
QR_CODE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "totp_qrcode.png")

ISSUER_NAME = "KiteTradingBot"
ACCOUNT_NAME = "dashboard"


def generate_backup_codes(count=5, length=10):
    alphabet = string.ascii_uppercase + string.digits
    return ["".join(secrets.choice(alphabet) for _ in range(length)) for _ in range(count)]


def main():
    if os.path.exists(TOTP_CONFIG_PATH):
        confirm = input(
            f"{TOTP_CONFIG_PATH} already exists. Re-running will generate a NEW "
            f"secret and backup codes, invalidating the old ones. Continue? [y/N] "
        )
        if confirm.strip().lower() != "y":
            print("Aborted. Existing 2FA setup left unchanged.")
            return

    secret = pyotp.random_base32()
    backup_codes = generate_backup_codes()

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=ACCOUNT_NAME, issuer_name=ISSUER_NAME)

    img = qrcode.make(provisioning_uri)
    img.save(QR_CODE_PATH)

    config = {
        "secret": secret,
        # Backup codes are stored as-is (not hashed) since this is a
        # single-operator personal tool, not a multi-user system --
        # consistent with the existing plaintext CONFIG_UI_PASSWORD
        # approach already used for the primary login.
        "backup_codes": backup_codes,
        "used_backup_codes": [],
    }
    with open(TOTP_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print()
    print("=" * 60)
    print("2FA SETUP COMPLETE")
    print("=" * 60)
    print()
    print(f"QR code saved to: {QR_CODE_PATH}")
    print("Open this image (e.g. via the file browser or scp it to your")
    print("computer) and scan it with Google Authenticator or a similar app.")
    print()
    print(f"Manual entry secret (if you can't scan): {secret}")
    print()
    print("BACKUP CODES -- write these down somewhere safe. Each one can")
    print("be used ONCE if you lose access to your authenticator app:")
    print()
    for code in backup_codes:
        print(f"  {code}")
    print()
    print("=" * 60)
    print(f"Saved to: {TOTP_CONFIG_PATH}")
    print("2FA is NOT yet active on the dashboard -- that's a separate step.")
    print("=" * 60)


if __name__ == "__main__":
    main()
