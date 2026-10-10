#!/usr/bin/env python3
"""Plan/apply a Matmon-only source cleanup. Never starts a trading service."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path('/home/ubuntu/kite_trading_bot')
PROTECTED = {'.git', 'venv', '.venv', 'runtime', 'logs', 'data', 'node_modules'}
SENSITIVE = ('credential', 'secret', 'token', 'api_key', 'api_secret')
LEGACY_PREFIXES = ('matmon_', 'el_bethel', 'fno_', 'kite_fno', 'phase3_backup',
                   'dashboard_backup', 'stop_scan_backup', 'opening_retest_backup',
                   'risk_rectification_backup', 'execution_backup', 'backups_matmon')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def release_files():
    manifest = json.loads((SOURCE / 'RELEASE_FILES.json').read_text())
    for name, sha in manifest.items():
        p = SOURCE / name
        if p.is_symlink() or not p.is_file() or digest(p) != sha:
            raise RuntimeError('Release hash check failed: ' + name)
    return manifest


def cleanup_candidates(target, release):
    """Only code/backup artifacts in the explicitly selected bot directory."""
    result = []
    for p in target.iterdir():
        n = p.name
        if n in PROTECTED or n.startswith('.') or p.is_symlink():
            continue
        if any(k in n.lower() for k in SENSITIVE):
            continue
        if p.is_file():
            code = p.suffix in {'.py', '.sh', '.service', '.timer', '.md'}
            backup = ('.py.' in n or (n.startswith(LEGACY_PREFIXES) and
                       n.endswith(('.tar.gz', '.zip', '.tgz'))))
            if (code or backup) and n not in release:
                result.append(p)
        elif p.is_dir() and n not in {'tools', 'deploy', 'tests'}:
            if n.startswith(LEGACY_PREFIXES) and not any(
                q.suffix in {'.jsonl', '.csv', '.sqlite', '.db', '.log'} or
                any(k in q.name.lower() for k in SENSITIVE)
                for q in p.rglob('*') if q.is_file()
            ):
                result.append(p)
    return sorted(result)


def inventory(path):
    if path.is_file():
        return {'path': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size}
    files = sorted(p for p in path.rglob('*') if p.is_file() and not p.is_symlink())
    return {'path': str(path), 'files': len(files),
            'bytes': sum(p.stat().st_size for p in files),
            'tree_sha256': hashlib.sha256('\n'.join(
                str(p.relative_to(path)) + ':' + digest(p) for p in files
            ).encode()).hexdigest()}


def bot_units():
    r = subprocess.run(['systemctl', 'list-unit-files', '--no-pager', '--no-legend'],
                       capture_output=True, text=True, check=True)
    return sorted({line.split()[0] for line in r.stdout.splitlines() if line.split()
                   and line.split()[0].startswith(('kitebot', 'kite-', 'kitedashboard',
                                                   'matmon-', 'el-bethel'))
                   and line.split()[0].endswith(('.service', '.timer'))})


def local_flat(target):
    for name, key in [('open_positions.json', 'positions'),
                      ('pending_orders.json', 'orders'), ('protective_stops.json', 'stops')]:
        p = target / name
        if not p.exists():
            continue
        obj = json.loads(p.read_text())
        if not isinstance(obj, dict):
            raise RuntimeError('Unrecognized local state: ' + name)
        rows = obj.get(key, {})
        if key == 'positions' and rows:
            raise RuntimeError('Local positions must be reconciled before cleanup')
        if key != 'positions' and any(not r.get('resolved') for r in rows):
            raise RuntimeError('Unresolved local state: ' + name)


def broker_flat(target):
    # Read-only broker calls. Credentials stay in the existing directory/env.
    code = ('import sys; sys.path.insert(0, '+repr(str(SOURCE))+'); '
            'from auth import get_kite_client; '
            'from matmon_safety import validate_broker_flat; '
            'validate_broker_flat(get_kite_client())')
    python = target / 'venv/bin/python3'
    subprocess.run([str(python), '-c', code], cwd=target, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target', type=Path, default=DEFAULT_TARGET)
    ap.add_argument('--apply-plan', type=Path)
    args = ap.parse_args()
    target = args.target.resolve()
    if target != DEFAULT_TARGET or SOURCE == target or target in SOURCE.parents:
        raise RuntimeError('Run from a separate release directory; target must be '+str(DEFAULT_TARGET))
    release = release_files()
    candidates = cleanup_candidates(target, release)
    plan = {'target': str(target), 'source': str(SOURCE), 'release': release,
            'remove': [inventory(p) for p in candidates],
            'replace': [inventory(target/n) for n in release if (target/n).exists()],
            'units': bot_units()}
    if not args.apply_plan:
        path = Path.home() / ('matmon_cleanup_plan_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
        path.write_text(json.dumps(plan, indent=2)+'\n');path.chmod(0o600)
        print('PLAN:', path)
        print('Files/directories to remove:', len(candidates))
        print('Units to disable:', ', '.join(plan['units']))
        print('Apply from a shell with the existing Kite credentials available:')
        print('python3 '+str(Path(__file__).resolve())+' --apply-plan '+str(path))
        return
    previous = json.loads(args.apply_plan.read_text())
    if previous != plan:
        raise RuntimeError('Files, release or units changed. Generate a new plan.')
    # Validate every destination before the first write (including parents).
    for name in release:
        destination = target / name
        for component in [destination, *destination.parents]:
            if component == target.parent:
                break
            if component.is_symlink():
                raise RuntimeError("Symlink in installation destination: " + str(component))
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            command = (proc/'cmdline').read_bytes().split(b'\0')
            cwd = (proc/'cwd').resolve()
        except (OSError, PermissionError):
            continue
        if command and b'python' in command[0] and (cwd == target or target in cwd.parents):
            raise RuntimeError("Python process still uses bot directory: PID " + proc.name)
    local_flat(target)
    # Require the user to stop scanning before applying. Do not interrupt
    # an unknown live process or race new entries against migration.
    for unit in plan['units']:
        state = subprocess.run(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'],
                               capture_output=True, text=True, check=True).stdout.strip()
        if unit.endswith('.service') and state not in {'inactive', 'failed'}:
            raise RuntimeError('Service is not stopped: '+unit+' ('+state+')')
    broker_flat(target)
    # Disable schedulers first, then verify nothing started during preflight.
    for unit in plan['units']:
        subprocess.run(['sudo', 'systemctl', 'disable', '--now', unit], check=True)
    broker_flat(target)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive = Path.home() / ('matmon_cleanup_rollback_'+stamp+'.tar.gz')
    with archive.open('xb') as raw:
        os.chmod(archive, 0o600)
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            backup = set(candidates)
            backup.update(target/n for n in release if (target/n).exists())
            for p in sorted(backup):
                tar.add(p, arcname=str(p.relative_to(target)), recursive=True)
            tar.add(args.apply_plan, arcname='CLEANUP_PLAN.json')
    # Finish and verify archive readability before touching source files.
    with tarfile.open(archive, 'r:gz') as tar:
        for m in tar:
            if m.isfile():
                f=tar.extractfile(m)
                while f.read(1024*1024):
                    pass
    for name in release:
        dst=target/name
        if dst.is_symlink():
            raise RuntimeError('Refusing to replace symlink: '+str(dst))
        dst.parent.mkdir(parents=True,exist_ok=True)
        tmp=dst.with_name(dst.name+'.matmon-install-tmp')
        shutil.copy2(SOURCE/name,tmp)
        os.replace(tmp,dst)
    for p in candidates:
        if p.is_dir():shutil.rmtree(p)
        else:p.unlink()
    # Old bytecode can retain modules removed above.
    for p in target.glob('__pycache__/*.pyc'):p.unlink()
    for name,sha in release.items():
        if digest(target/name)!=sha:raise RuntimeError('Installed hash mismatch: '+name)
    print('INSTALLED: MATMON_HAELOHIM. Trading services remain stopped.')
    print('ROLLBACK ARCHIVE:',archive)
    print('Protected: credentials, user_config, state, histories, logs, venv, git history.')
    print('External FNO directories and data-containing backup directories require the separate inventory review.')

if __name__=='__main__':
    main()
