"""Dedicated Nginx front end for Futures. Never rewrite an unrelated vhost.

The only managed Nginx file is futures-portal.conf with an exact ownership
marker. Gunicorn uses the loopback backend recorded in data/nginx_proxy.json.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile

CONFIG_FILENAME = "futures-portal.conf"
STATE_NAME = "nginx_proxy.json"
LABEL = "# FUTURES-PORTAL MANAGED CONFIG"
DOMAIN = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
LISTEN = re.compile(r"\blisten\s+(?:[^\s;]*:)?(\d{1,5})(?=[;\s])")


def proxy_state(root):
    path = Path(root) / 'data' / STATE_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data.get('enabled') is True and isinstance(data.get('port'), int)
            and isinstance(data.get('backend_port'), int)
            and 1 <= data['port'] <= 65535 and 1 <= data['backend_port'] <= 65535
            and data['backend_port'] != data['port']):
            return data
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return {'enabled': False, 'https': False, 'domain': ''}


def backend_web(root, web):
    state = proxy_state(root)
    if state.get('enabled') and state['port'] == int(web['port']):
        return {'host': '127.0.0.1', 'port': state['backend_port']}
    return web


def validate_domain(value):
    value = str(value or '').strip().lower().rstrip('.')
    if value and not DOMAIN.fullmatch(value):
        raise ValueError('域名须是标准英文域名，不包含 https://、端口、路径或空格')
    return value


def _run(command, timeout=12):
    try:
        return subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'系统命令未完成：{command[0]}') from exc


def _nginx_dump():
    result = _run(['nginx', '-T'])
    if result.returncode:
        raise ValueError('现有 Nginx 配置检查失败，拒绝修改任何站点配置：' + result.stderr[-700:])
    return result.stdout + '\n' + result.stderr


def _nginx_config_dir(dump):
    # Read the *active* includes; never edit nginx.conf or another site's file.
    if re.search(r'include\s+/etc/nginx/conf\.d/\*\.conf\s*;', dump):
        return Path('/etc/nginx/conf.d')
    if re.search(r'include\s+/etc/nginx/sites-enabled/\*\s*;', dump):
        return Path('/etc/nginx/sites-enabled')
    raise ValueError('nginx.conf 未包含可用的独立 conf.d/sites-enabled 目录，拒绝修改全局 Nginx 配置')


def _cert_pair_ok(cert, key, domain):
    cert, key = Path(cert), Path(key)
    if not (cert.is_file() and key.is_file()):
        return False
    if any(c.isspace() for c in str(cert) + str(key)):
        return False
    verification = _run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkhost', domain], 5)
    if verification.returncode or 'does match' not in verification.stdout:
        return False
    # The SSL listener must not activate with an already expired or not-yet
    # valid certificate. -checkhost checks only the identity, not dates.
    validity = _run(['openssl', 'x509', '-in', str(cert), '-noout', '-dates', '-checkend', '0'], 5)
    if validity.returncode:
        return False
    try:
        from datetime import datetime, timezone
        import email.utils
        dates = dict(line.split('=', 1) for line in validity.stdout.splitlines() if '=' in line)
        start = email.utils.parsedate_to_datetime(dates['notBefore'])
        end = email.utils.parsedate_to_datetime(dates['notAfter'])
        now = datetime.now(timezone.utc)
        if not (start <= now < end):
            return False
    except (KeyError, ValueError, TypeError):
        return False
    a = _run(['openssl', 'x509', '-in', str(cert), '-pubkey', '-noout'], 5)
    b = _run(['openssl', 'pkey', '-in', str(key), '-pubout'], 5)
    return a.returncode == 0 and b.returncode == 0 and a.stdout.strip() == b.stdout.strip()


def find_certificate(domain, dump='', explicit_cert='', explicit_key=''):
    if not domain or not shutil.which('openssl'):
        return None
    if explicit_cert or explicit_key:
        if not explicit_cert or not explicit_key or not _cert_pair_ok(explicit_cert, explicit_key, domain):
            raise ValueError('手动指定的证书/私钥不存在、不匹配，或证书不包含该域名')
        return (explicit_cert, explicit_key)
    pairs = []
    # Standard certbot/acme.sh layouts. Only explicit trusted directories.
    for base in [Path('/etc/letsencrypt/live'), Path('/etc/nginx/ssl'),
                 Path('/etc/nginx/certs'), Path('/root/.acme.sh')]:
        if not base.is_dir():
            continue
        if base.name == 'live':
            for folder in list(base.glob(domain + '*'))[:20]:
                pairs.append((folder / 'fullchain.pem', folder / 'privkey.pem'))
        elif base.name == '.acme.sh':
            for folder in [base / domain, base / (domain + '_ecc')]:
                pairs.extend([(folder / 'fullchain.cer', folder / (domain + '.key')),
                              (folder / (domain + '.cer'), folder / (domain + '.key'))])
        else:
            for suffix in ('pem', 'crt', 'cer'):
                pairs.extend([(base / (domain + '.' + suffix), base / (domain + '.key')),
                              (base / (domain + '.fullchain.' + suffix), base / (domain + '.key'))])
    # Existing Nginx certificates can live anywhere; only accept paths Nginx
    # already uses, and still verify hostname AND corresponding private key.
    certs = re.findall(r'(?<!\w)ssl_certificate\s+([^\s;]+)\s*;', dump)
    keys = re.findall(r'(?<!\w)ssl_certificate_key\s+([^\s;]+)\s*;', dump)
    pairs.extend((Path(c), Path(k)) for c, k in zip(certs, keys) if c.startswith('/') and k.startswith('/'))
    for cert, key in pairs:
        if _cert_pair_ok(cert, key, domain):
            return str(cert), str(key)
    return None


def _ownership(path, root):
    if path.is_symlink():
        raise ValueError('Nginx 配置目标是符号链接，拒绝操作')
    if path.exists():
        if not path.is_file():
            raise ValueError('Nginx 配置路径已被其他对象占用')
        with path.open(encoding='utf-8') as f:
            first = f.readline().strip()
            second = f.readline().strip()
        if (first != LABEL or second != f'# futures.sh project-root: {root}'):
            raise ValueError('Nginx 同名配置不属于 Futures，拒绝覆盖或删除')


def _used_elsewhere(dump, port, own_path):
    current = ''
    for line in dump.splitlines():
        match = re.match(r'# configuration file (.*):\s*$', line)
        if match:
            current = match.group(1)
        if current and current != str(own_path):
            code = line.split('#', 1)[0]
            if any(int(p) == port for p in LISTEN.findall(code)):
                return current
    return ''


def _port_free(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('0.0.0.0', port))
    except OSError as exc:
        raise ValueError(f'TCP {port} 已被占用，拒绝改动其他程序：{exc}') from None


def _backend_port(public_port, previous):
    old = previous.get('backend_port')
    if previous.get('enabled') and isinstance(old, int) and old != public_port:
        return old
    value = public_port + 10000 if public_port <= 55535 else public_port - 10000
    if value == public_port:
        value = 18097
    return value


def _ipv6_available():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(('::1', 0))
        return True
    except OSError:
        return False


def _vhost(root, port, domain, backend_port, pair):
    ssl_lines = ''
    if pair:
        ssl_lines = (f'    ssl_certificate {pair[0]};\n'
                     f'    ssl_certificate_key {pair[1]};\n'
                     '    ssl_protocols TLSv1.2 TLSv1.3;\n')
    ipv6_enabled = socket.has_ipv6 and _ipv6_available()
    suffix = ' ssl' if pair else ''
    return (f'{LABEL}\n# futures.sh project-root: {root}\n'
            'server {\n'
            f'    listen {port}{suffix};\n'
            + (f'    listen [::]:{port}{suffix};\n' if ipv6_enabled else '')
            + f"    server_name {domain if domain else '_'};\n"
            + ssl_lines +
            '    client_max_body_size 8m;\n'
            '    add_header X-Content-Type-Options nosniff always;\n'
            '    location / {\n'
            f'        proxy_pass http://127.0.0.1:{backend_port};\n'
            '        proxy_http_version 1.1;\n'
            '        proxy_set_header Host $http_host;\n'
            '        proxy_set_header X-Real-IP $remote_addr;\n'
            '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
            '        proxy_set_header X-Forwarded-Proto $scheme;\n'
            '        proxy_connect_timeout 10s;\n'
            '        proxy_read_timeout 180s;\n'
            '        proxy_send_timeout 180s;\n'
            '    }\n'
            '}\n')


def _atomic_file(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.futures-tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _reload_nginx():
    tested = _run(['nginx', '-t'])
    if tested.returncode:
        raise ValueError('Nginx 配置校验失败：' + tested.stderr[-900:])
    reloaded = _run(['nginx', '-s', 'reload'])
    if reloaded.returncode:
        raise ValueError('Nginx 无法热重载（不执行重启）：' + reloaded.stderr[-800:])


def configure(root, port, domain, cert='', key=''):
    root = Path(root).resolve()
    if not (1 <= port <= 65535):
        raise ValueError('外部端口无效')
    domain = validate_domain(domain)
    state = proxy_state(root)
    marker = root / 'data' / STATE_NAME
    if not shutil.which('nginx'):
        if state.get('enabled'):
            raise ValueError('之前配置了专用 Nginx，但 nginx 命令已丢失；拒绝意外切换监听')
        return {'enabled': False, 'https': False, 'url': f'http://{domain or "服务器IP"}:{port}/',
                'detail': '系统未安装 Nginx，直接 HTTP 访问。需 HTTPS 请先安装并启动 Nginx。'}
    dump = _nginx_dump()
    conf = _nginx_config_dir(dump) / CONFIG_FILENAME
    _ownership(conf, root)
    previous_conf = conf.read_text(encoding='utf-8') if conf.exists() else None
    previous_marker = marker.read_text(encoding='utf-8') if marker.exists() else None
    used = _used_elsewhere(dump, port, conf)
    if used:
        raise ValueError(f'Nginx 的 TCP {port} 已用于其他站点：{used}，不会覆盖')
    if not state.get('enabled') or state.get('port') != port:
        _port_free(port)
    backend_port = _backend_port(port, state)
    if not state.get('enabled') or state.get('backend_port') != backend_port:
        _port_free(backend_port)
    if cert or key:
        pair = find_certificate(domain, dump, cert, key)
    else:
        pair = find_certificate(domain, dump)
    text = _vhost(root, port, domain, backend_port, pair)
    new_state = {'enabled': True, 'https': bool(pair), 'domain': domain,
                 'port': port, 'backend_port': backend_port, 'config_path': str(conf),
                 'certificate': pair[0] if pair else ''}
    if previous_conf == text and state == new_state:
        # A certificate may have been renewed in place.
        _reload_nginx()
        return {**new_state, 'url': f'{"https" if pair else "http"}://{domain or "服务器IP"}:{port}/',
                'detail': '专用 Nginx 配置未变化'}
    try:
        _atomic_file(conf, text, 0o644)
        # Verify the chosen Nginx include is actually loaded.
        loaded = _nginx_dump()
        if f'# configuration file {conf}:' not in loaded:
            raise ValueError('Nginx 没有加载独立的 Futures 配置文件')
        _atomic_file(marker, json.dumps(new_state, ensure_ascii=False, indent=2) + '\n', 0o600)
        _reload_nginx()
    except Exception:
        if previous_conf is None:
            conf.unlink(missing_ok=True)
        else:
            _atomic_file(conf, previous_conf, 0o644)
        if previous_marker is None:
            marker.unlink(missing_ok=True)
        else:
            _atomic_file(marker, previous_marker, 0o600)
        if _run(['nginx', '-t']).returncode == 0:
            _run(['nginx', '-s', 'reload'])
        raise
    return {**new_state, 'url': f'{"https" if pair else "http"}://{domain or "服务器IP"}:{port}/',
            'detail': ('有效域名证书已加载，使用 HTTPS' if pair else '未发现对应有效证书，暂以 HTTP 提供访问')}


def remove(root):
    root = Path(root).resolve()
    state = proxy_state(root)
    marker = root / 'data' / STATE_NAME
    if not state.get('enabled'):
        return {'removed': False, 'detail': '没有本项目管理的 Nginx 配置'}
    conf = Path(state['config_path'])
    if not (conf.name == CONFIG_FILENAME and str(conf.parent) in ['/etc/nginx/conf.d', '/etc/nginx/sites-enabled']):
        raise ValueError('Nginx 配置路径与本项目预期不符，拒绝删除')
    _ownership(conf, root)
    if not conf.is_file():
        raise ValueError('专属 Nginx 配置已不在原路径，请先人工核对')
    previous = conf.read_text(encoding='utf-8')
    conf.unlink()
    try:
        _reload_nginx()
    except Exception:
        _atomic_file(conf, previous, 0o644)
        _reload_nginx()
        raise
    marker.unlink(missing_ok=True)
    return {'removed': True, 'detail': '只移除归属于 Futures 的 Nginx 配置并热重载'}


def probe_url(root):
    state = proxy_state(root)
    if not state.get('enabled'):
        return {'ok': False, 'detail': '未启用 Nginx 代理，访问由 Gunicorn 直接提供 HTTP'}
    port = state['port']
    domain = state['domain']
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=4) as conn:
            if state.get('https'):
                # Local availability check, not certificate trust validation.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                connection = context.wrap_socket(conn, server_hostname=domain or None)
            else:
                connection = conn
            with connection:
                connection.sendall(f'GET /health HTTP/1.1\r\nHost: {domain or "localhost"}:{port}\r\nConnection: close\r\n\r\n'.encode())
                head = connection.recv(2048)
                if b'HTTP/1.1 200' not in head and b'HTTP/1.0 200' not in head:
                    raise ValueError('HTTPS/HTTP 代理尚未返回 200')
        return {'ok': True, 'url': f'{"https" if state["https"] else "http"}://{domain or "服务器IP"}:{port}/'}
    except (OSError, ssl.SSLError, ValueError) as exc:
        return {'ok': False, 'detail': str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Futures 专属 Nginx 配置管理')
    parser.add_argument('action', choices=['configure', 'probe', 'remove', 'status', 'health'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--domain', default='')
    parser.add_argument('--port', type=int, default=8197)
    parser.add_argument('--cert', default='')
    parser.add_argument('--key', default='')
    args = parser.parse_args(argv)
    try:
        if args.action == 'probe':
            domain = validate_domain(args.domain)
            found = find_certificate(domain, _nginx_dump() if shutil.which('nginx') else '')
            result = {'domain': domain, 'found': bool(found), 'certificate': found[0] if found else ''}
        elif args.action == 'configure':
            result = configure(args.root, args.port, args.domain, args.cert, args.key)
        elif args.action == 'remove':
            result = remove(args.root)
        elif args.action == 'health':
            result = probe_url(args.root)
            if not result['ok']:
                print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
                return 1
        else:
            result = proxy_state(args.root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
