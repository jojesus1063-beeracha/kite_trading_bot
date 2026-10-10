# MATMON_HAELOHIM

Single supported equity strategy, recovered from the September 10, 2026
09:57 IST deployment archive and checked against its service journal.

The entry sequence is completed REST 3-minute EMA3/EMA15 direction, DI14
agreement, the first three valid post-DI ticks within three seconds,
strict CLEAN or flat-with-pressure, LTP velocity and weighted five-level
book direction/strengthening. This does not require three seconds between
the first and last of those three ticks. Adverse execution-price movement
can trigger a 30-second pullback wait followed by a new confirmation.
The scanner uses 12-symbol batches and three prefetch workers.

## Authoritative policy

`matmon_strategy_config.py` contains the strategy and live risk values.
The launcher applies them after loading operational configuration so old
stop/exit switches cannot silently change the recovered policy.

- Universe: 120 ordinary equities across NSE/BSE, selected before trading.
- Capital: fixed INR 5,000, as logged on September 10; not dynamic balance.
- Planned risk per trade: 2%; maximum positions: 3; daily trades: 10.
- Maximum position-size setting: 50%; broker margin check retained.
- Consecutive loss limit: 3. Daily loss threshold 0.5% is **disabled**, matching
  the historical launcher. These are historical settings, not a new recommendation.
- Initial stop: 1% from confirmed fill, corroborated by all three September 10
  entry logs; the archived Python default was 0.45%, overridden at runtime.
- Exit: half at 1R, remainder at 2R, runner stop to breakeven after the partial
  fill is confirmed. Quantity rounding and broker protection remain in the
  recovered execution modules.
- Session defaults from recovered source: entry 09:15–15:00, local square-off
  15:08. The independent EOD supervisor added on September 10 stops the scanner
  at 15:04 and squares off at 15:05. Its final reconciliation deadline is 15:10.

`config.py` still provides shared execution settings and loads the existing
`user_config.json`. Credentials and local operational state are never committed.
The code retains shared analytics/helpers required by the recovered engine;
it removes other executable strategy launchers and the generic entry evaluator.
Disabled research modules that are still imported remain dependencies, not
additional supported strategies. This is not a rewrite of the broker engine.

## Runtime entry points

- `matmon_live_launcher.py`: the sole trading launcher. Requires live config,
  `KITE_LIVE_COMBINED_ACK=I_ACCEPT_REAL_ORDERS`, and valid broker credentials.
  The historical acknowledgement variable name is retained for compatibility.
- `matmon_live_preflight.py --check-broker-flat`: read-only checks.
- `matmon_preopen_top120.py`: current-day universe selection, no orders.
- `matmon_eod_squareoff.py`: independent protective exit/reconciliation.
- `auth.py`: interactive daily authentication.

`matmon_policy.py` holds shared signal/confirmation functions. Running `main.py`
directly is blocked before broker access. The old `paper_matmon_launcher.py`,
FNO/EL-BETHEL/combined/contrarian launchers and unrelated research scripts have
been removed from the current tree. Git history remains the recovery record.

## Validation

Run `python -m pip install -r requirements-dev.txt` followed by
`python -m pytest -q`. Tests are offline, use simulated broker responses and do
not place orders. See `RECOVERY_MANIFEST.json` for source provenance.
A passing test suite does not establish strategy profitability or broker readiness.

## Server migration

Use a separate checkout of this release, outside `/home/ubuntu/kite_trading_bot`.
Do not pull it over the running server directory or use `git clean -fdx`.

1. Stop all existing bot scanning services during maintenance and verify broker
   exposure is flat. Keep exit protection operational until exposure is cleared.
2. Run `python3 tools/install_matmon.py` from the separate checkout. This only
   writes a plan with exact file hashes and unit names. Review the printed plan.
3. In a shell with the existing Kite credentials available, execute the printed
   `--apply-plan` command. It verifies source hashes, unchanged inventory, stopped
   services, local state and broker flatness. It disables old bot units, makes a
   private rollback archive, installs the release and removes scoped legacy code.
   It does not start or enable the live bot.
4. Preserve `user_config.json`, credentials, histories, runtime data, `.git` and
   the existing venv. Reinstall requirements if needed. Run the tests and preflight
   on the server. Authentication may need refreshing before preflight.
5. New unit templates are in `deploy/systemd`. They reference `.env` and optional
   `matmon-live.env`, do not embed secrets, and use `matmon-live.service` to avoid
   old launcher drop-ins. Install the templates only after inspecting local
   credentials/environment paths. EOD units must be in place before live startup.
6. The installer deliberately leaves FNO deployments outside the target directory,
   unknown data directories and old Git branches intact. Inventory those separately
   before removing their code; export their histories first. Do not confuse this
   scoped source cleanup with erasure of all historical records.

The rollback archive contains files replaced/deleted and the original unit
inventory. It remains on the server with mode 0600. Live startup is a separate
manual step after deployment checks. No automatic live-start timer is supplied.

## Exact deployment commands

Fetch into a separate directory and generate the inventory first:

```bash
cd /home/ubuntu
git clone --branch cleanup/matmon-sept10-only --single-branch https://github.com/jojesus1063-beeracha/kite_trading_bot.git matmon_sept10_release
cd /home/ubuntu/matmon_sept10_release
python3 tools/install_matmon.py
```

The printed plan is read-only. Apply its exact command during a maintenance
window with scanning services stopped, broker exposure reconciled, and existing
Kite environment variables loaded. The installer refuses active services or
unresolved exposure. Do not stop exit protection while positions are open.

After successful installation, install the new unit templates:

```bash
sudo cp /home/ubuntu/kite_trading_bot/deploy/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now matmon-preopen.timer matmon-stop-live-eod.timer matmon-eod-squareoff.timer
cd /home/ubuntu/kite_trading_bot
venv/bin/python3 -m pip install -r requirements-dev.txt
venv/bin/python3 -m pytest -q
```

These commands enable selection and exit schedulers, not live entry. Before
manual live startup, verify the current-day watchlist, refreshed credentials,
`paper_trading=false`, the acknowledgement in the service environment, and a
successful `matmon_live_preflight.py --check-broker-flat` using that same
environment. No live-start command is included in the cleanup procedure.
