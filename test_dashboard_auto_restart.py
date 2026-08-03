from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dashboard_bot_reload as reload_module


def check(name, condition):
    if not condition:
        raise AssertionError(name)

    print("PASS:", name)


source = Path(
    "configure_app.py"
).read_text(encoding="utf-8")

check(
    "Dashboard imports automatic apply function",
    (
        "from dashboard_bot_reload import "
        "apply_saved_config"
    )
    in source,
)

check(
    "Dashboard Save invokes automatic apply",
    (
        "# DASHBOARD_AUTO_APPLY_CONFIG"
        in source
        and "apply_saved_config()" in source
    ),
)

with patch.object(
    reload_module.subprocess,
    "run",
    return_value=SimpleNamespace(
        returncode=0,
        stdout=(
            "Settings saved and applied. "
            "kitebot.service restarted in PAPER mode."
        ),
        stderr="",
    ),
):
    applied, message = (
        reload_module.apply_saved_config()
    )

check(
    "Successful helper result is reported as applied",
    applied is True,
)

check(
    "Successful helper message is preserved",
    "restarted" in message,
)

with patch.object(
    reload_module.subprocess,
    "run",
    return_value=SimpleNamespace(
        returncode=75,
        stdout=(
            "Settings saved, but automatic restart "
            "was deferred."
        ),
        stderr="",
    ),
):
    applied, message = (
        reload_module.apply_saved_config()
    )

check(
    "Deferred live restart is not reported as applied",
    applied is False,
)

check(
    "Deferred message is preserved",
    "deferred" in message,
)

print()
print("All dashboard automatic-restart tests passed.")
