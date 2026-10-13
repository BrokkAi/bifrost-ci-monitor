#!/usr/bin/env python3
"""Build and install the Merge Marshall dashboard user service."""
from __future__ import annotations
import argparse
import ipaddress
import os
from pathlib import Path
import shlex
import subprocess
from urllib.parse import urlsplit


def service_unit(repository, listen, port=8770, public_url='http://localhost:8770', github_cli=None):
    address = ipaddress.ip_address(listen)
    if not address.is_private or address.is_unspecified:
        raise ValueError('service must listen on a specific private address')
    if not 1 <= port <= 65535:
        raise ValueError('port must be between 1 and 65535')
    parsed = urlsplit(public_url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
        raise ValueError('public URL must be an HTTP(S) origin')
    repo = Path(repository).resolve()
    local_gh = Path.home() / '.local/bin/gh'
    configured = github_cli or os.environ.get('BIFROST_GH_BIN')
    gh = Path(configured or (local_gh if os.access(local_gh, os.X_OK) else '/usr/bin/gh')).expanduser().resolve()
    if any(c in str(value) for value in [repo, gh, public_url] for c in ['\n', '\r', '"', '%', '\\']):
        raise ValueError('unsupported service path or URL')
    skill_address = f'[{address}]' if address.version == 6 else str(address)
    return f'''[Unit]
Description=Merge Marshall web dashboard
After=network-online.target

[Service]
Type=simple
WorkingDirectory={repo}
ExecStart="{repo}/.venv/bin/uvicorn" mm_web.app:app --host {address} --port {port} --no-access-log
Environment=PATH=%h/.cargo/bin:%h/.local/bin:/usr/bin:/bin
Environment="BIFROST_GH_BIN={gh}"
Environment="MM_WEB_PUBLIC_URL={public_url.rstrip('/')}"
Environment="MM_WEB_SKILL_SERVICE_URL=http://{skill_address}:8769"
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
'''


def install(args):
    repo = args.repository.resolve()
    unit = Path.home() / '.config/systemd/user/mm-web.service'
    content = service_unit(repo, args.listen, args.port, args.public_url, args.github_cli)
    steps = [(repo, ['uv', 'sync', '--frozen']),
             (repo / 'web', ['npm', 'ci']), (repo / 'web', ['npm', 'run', 'build'])]
    services = [['systemctl', '--user', 'daemon-reload'],
                ['systemctl', '--user', 'enable', 'mm-web.service'],
                ['systemctl', '--user', 'restart', 'mm-web.service']]
    if args.dry_run:
        for cwd, command in steps:
            print(f'cd {shlex.quote(str(cwd))} && {shlex.join(command)}')
        print(f'Write {unit}:\n{content}')
        for command in services:
            print(shlex.join(command))
        return
    for cwd, command in steps:
        subprocess.run(command, cwd=cwd, check=True)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    env = dict(os.environ)
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    for command in services:
        subprocess.run(command, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', required=True, help='specific private host IP')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--public-url', default=os.environ.get('MM_WEB_PUBLIC_URL', 'http://localhost:8770'))
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--github-cli')
    parser.add_argument('--dry-run', action='store_true')
    install(parser.parse_args())


if __name__ == '__main__':
    main()
