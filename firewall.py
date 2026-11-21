"""Open the application's TCP port without changing unrelated firewall rules.

Only UFW/firewalld rules actually created by this project are recorded and
eligible for removal. Provider security groups and custom nftables/iptables
policies must be managed by the operator.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

STATE_NAME = 'firewall_owned.json'


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('防火墙命令失败：' + args[0]) from exc


def _read(root):
    try:
        entries = json.loads((Path(root) / 'data' / STATE_NAME).read_text())
        if isinstance(entries, list):
            return [r for r in entries if isinstance(r, dict) and r.get('owner') == 'futures-portal']
    except (OSError, ValueError):
        pass
    return []


def _write(root, records):
    path = Path(root) / 'data' / STATE_NAME
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    import os
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix='.futures-fw-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as writer:
            json.dump(records, writer)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _backend():
    if shutil.which('ufw'):
        status = _run(['ufw', 'status'])
        if status.returncode == 0 and 'Status: active' in status.stdout:
            return 'ufw'
    if shutil.which('firewall-cmd') and _run(['firewall-cmd', '--state']).returncode == 0:
        return 'firewalld'
    return ''


def open_port(root, port):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('端口无效')
    records = _read(root)
    backend = _backend()
    if not backend:
        return {'ok': True, 'changed': False, 'detail': '未检测到正在运行的 UFW/firewalld；不修改其他防火墙规则。请核对运营商安全组或自定义防火墙。'}
    if backend == 'ufw':
        status = _run(['ufw', 'status'])
        present = [x for x in status.stdout.splitlines() if x.strip().startswith(f'{port}/tcp')]
        if any('DENY' in x or 'REJECT' in x for x in present):
            raise ValueError(f'UFW 上存在 TCP {port} 的拒绝规则，请先人工检查，避免影响现有规则')
        if present:
            return {'ok': True, 'changed': False, 'detail': f'UFW 已存在 TCP {port} 规则，保持不变'}
        operation = ['ufw', 'allow', f'{port}/tcp', 'comment', 'futures-portal']
    else:
        if _run(['firewall-cmd', '--query-port', f'{port}/tcp']).returncode == 0:
            return {'ok': True, 'changed': False, 'detail': f'firewalld 已放行 TCP {port}，保持不变'}
        operation = ['firewall-cmd', '--permanent', '--add-port', f'{port}/tcp']
    result = _run(operation)
    if result.returncode:
        raise ValueError(f'{backend} 放行 TCP {port} 失败：{(result.stderr or result.stdout)[-300:]}')
    if backend == 'firewalld':
        result = _run(['firewall-cmd', '--reload'])
        if result.returncode:
            raise ValueError('firewalld 已写入规则，但热加载失败，请人工核查')
    records.append({'owner': 'futures-portal', 'backend': backend, 'port': port})
    _write(root, records)
    return {'ok': True, 'changed': True, 'detail': f'已通过 {backend} 放行 TCP {port}（仅该规则由本项目维护）'}


def remove_owned(root, port=None):
    owned = _read(root)
    kept = []
    removed = []
    for record in owned:
        old = record.get('port')
        backend = record.get('backend')
        if port is not None and old != port:
            kept.append(record); continue
        if not isinstance(old, int) or not 1 <= old <= 65535 or backend not in {'ufw', 'firewalld'}:
            kept.append(record); continue
        if backend == 'ufw' and shutil.which('ufw'):
            # Delete by the exact rule spec; never remove another generic port rule.
            status = _run(['ufw', 'status', 'numbered'])
            import re
            numbers = []
            for line in status.stdout.splitlines():
                if f'{old}/tcp' in line and 'futures-portal' in line and 'ALLOW' in line:
                    match = re.match(r'\s*\[\s*(\d+)\]', line)
                    if match:
                        numbers.append(int(match.group(1)))
            # Never delete a same-port rule if its origin cannot be confirmed.
            if len(numbers) != 1:
                kept.append(record); continue
            result = _run(['ufw', '--force', 'delete', str(numbers[0])])
        elif backend == 'firewalld' and shutil.which('firewall-cmd'):
            result = _run(['firewall-cmd', '--permanent', '--remove-port', f'{old}/tcp'])
            if result.returncode == 0:
                _run(['firewall-cmd', '--reload'])
        else:
            kept.append(record); continue
        if result.returncode != 0:
            kept.append(record)
        else:
            removed.append(old)
    _write(root, kept)
    return {'removed': removed, 'kept': len(kept)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('open', 'remove', 'status'))
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int)
    args = parser.parse_args(argv)
    try:
        if args.action == 'open':
            result = open_port(args.root, args.port)
        elif args.action == 'remove':
            result = remove_owned(args.root, args.port)
        else:
            result = {'backend': _backend(), 'owned': _read(args.root)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ValueError as exc:
        print('警告：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
