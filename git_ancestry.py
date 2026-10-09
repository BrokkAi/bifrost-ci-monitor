"""Local, full-history commit cache; cold fetches never occupy the cron lock.

The worker writes an immutable receipt, not scheduling state. Only the locked
supervisor interprets it. Authentication is inherited in the environment and is
never saved in Git configuration, command arguments, or fetch output.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from read_budget import Deferred


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def launch_worker(request, env):
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(request)],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


class Cache:
    def __init__(self, directory, repository, token, *, deadline=lambda: None):
        self.directory = Path(directory)
        self.repository = repository
        self.token = token
        self.deadline = deadline
        self.worker = None
        self.git_dir = self.directory / 'objects.git'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.git_dir.exists():
            if self.command('init', '--bare', str(self.git_dir), bare=False).returncode:
                raise RuntimeError('could not initialize ancestry object cache')
        if (self.git_dir / 'shallow').exists():
            raise RuntimeError('ancestry object cache must contain full history')

    def command(self, *args, bare=True, input=None):
        limit = self.deadline()
        remaining = min(10., limit - time.monotonic()) if limit is not None else 10.
        if remaining <= 0:
            raise Deferred('supervisor read budget exhausted')
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
        try:
            return subprocess.run(['git', *(['--git-dir', str(self.git_dir)] if bare else []), *args],
                                  input=input, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=env, timeout=remaining, check=False)
        except subprocess.TimeoutExpired as exc:
            raise Deferred('local ancestry read is unfinished') from exc

    def missing(self, heads):
        heads = list(dict.fromkeys(heads))
        if not heads:
            return []
        if not all(re.fullmatch(r'[0-9a-f]{40}', head) for head in heads):
            raise ValueError('invalid ancestry commit')
        result = self.command('cat-file', '--batch-check=%(objectname) %(objecttype)',
                              input='\n'.join(heads) + '\n')
        if result.returncode:
            raise RuntimeError('could not inspect ancestry object cache')
        lines = result.stdout.splitlines()
        if len(lines) != len(heads):
            raise RuntimeError('incomplete ancestry object response')
        return [head for head, line in zip(heads, lines) if line != f'{head} commit']

    def prepare(self, heads):
        missing = self.missing(heads)
        if not missing:
            return True
        job_path = self.directory / 'fetch.json'
        job = json.loads(job_path.read_text()) if job_path.exists() else None
        if job:
            receipt = self.directory / (job['id'] + '.json')
            if not receipt.exists() and time.time() - job['started'] < 360:
                return False
            if receipt.exists():
                missing = self.missing(heads)
                if not missing:
                    return True
                # An unreachable historical SHA uses the authoritative API.
                # Successful objects from a partly failed fetch remain reusable.
                if set(missing).issubset(job['heads']) and not json.loads(receipt.read_text())['ok']:
                    return True
        requested = missing[:32]
        token = self.token()
        job = {'id': uuid.uuid4().hex, 'started': time.time(), 'heads': requested,
               'repository': self.repository}
        request = self.directory / (job['id'] + '.request.json')
        atomic_json(request, job)
        atomic_json(job_path, job)
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        count = int(env.get('GIT_CONFIG_COUNT', '0'))
        env[f'GIT_CONFIG_KEY_{count}'] = 'credential.helper'
        env[f'GIT_CONFIG_VALUE_{count}'] = ''
        if token:
            count += 1
            env[f'GIT_CONFIG_KEY_{count}'] = 'http.https://github.com/.extraheader'
            encoded = base64.b64encode(('x-access-token:' + token).encode()).decode()
            env[f'GIT_CONFIG_VALUE_{count}'] = 'AUTHORIZATION: basic ' + encoded
        env['GIT_CONFIG_COUNT'] = str(count + 1)
        try:
            self.worker = launch_worker(request, env)
        except OSError:
            job_path.unlink(missing_ok=True)
            raise
        return False

    def wait_for_fetch(self, timeout):
        job_path = self.directory / 'fetch.json'
        if not job_path.exists():
            return False
        job = json.loads(job_path.read_text())
        receipt = self.directory / (job['id'] + '.json')
        deadline = time.monotonic() + timeout
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        if receipt.exists() and self.worker is not None:
            self.worker.poll()
        return receipt.exists()

    def ancestor(self, a, b, fallback):
        if not self.prepare([a, b]):
            raise Deferred('fetching commit history for ancestry checks')
        if self.missing([a, b]):
            return fallback(a, b)
        # A partial clone omits blobs, never commit parents. Reject accidental
        # shallow state before trusting a negative merge-base result.
        if (self.git_dir / 'shallow').exists():
            raise RuntimeError('cannot prove ancestry from shallow history')
        result = self.command('merge-base', '--is-ancestor', a, b)
        if result.returncode not in (0, 1):
            raise RuntimeError('local commit ancestry could not be established')
        return result.returncode == 0


def fetch_worker(request):
    job = json.loads(request.read_text())
    directory = request.parent
    ok = False
    try:
        # Serialize writes across a lost launch reply or a stale worker receipt.
        with (directory / 'fetch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            repo = job['repository']
            url = repo if repo.startswith('/') else 'https://github.com/' + repo + '.git'
            deadline = time.monotonic() + 300
            result = subprocess.run(
                ['git', '--git-dir', str(directory / 'objects.git'), '-c', 'gc.auto=0', 'fetch', '--no-tags',
                 '--filter=blob:none', url, *[head + ':refs/commits/' + head for head in job['heads']]],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=300, check=False)
            ok = result.returncode == 0
            if not ok:
                # One unreachable old tip must not prevent caching the master
                # and all other available tips. Keep this worker bounded too.
                for head in job['heads']:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    present = subprocess.run(['git', '--git-dir', str(directory / 'objects.git'),
                                              'cat-file', '-e', head + '^{commit}'],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             env=dict(os.environ, GIT_NO_LAZY_FETCH='1'),
                                             timeout=min(5, remaining), check=False)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if present.returncode:
                        subprocess.run(['git', '--git-dir', str(directory / 'objects.git'), '-c', 'gc.auto=0',
                                        'fetch', '--no-tags', '--filter=blob:none', url,
                                        head + ':refs/commits/' + head],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=min(30, remaining), check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        atomic_json(directory / (job['id'] + '.json'), {'ok': ok})
        request.unlink(missing_ok=True)


if __name__ == '__main__':
    fetch_worker(Path(sys.argv[1]))
