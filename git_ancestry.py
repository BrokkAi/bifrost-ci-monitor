"""Shared full-history Git cache; fetch workers never mutate scheduling state.

Authentication lives only in the environment. Mutable refs are read freshly;
only successful results about immutable objects are cached on disk.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from read_budget import Deferred
import read_budget


_read_only = ContextVar('git_cache_read_only', default=False)


@contextmanager
def read_only():
    token = _read_only.set(True)
    try:
        yield
    finally:
        _read_only.reset(token)


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def authenticated_env(token):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1')
    count = int(env.get('GIT_CONFIG_COUNT', '0'))
    env[f'GIT_CONFIG_KEY_{count}'] = 'credential.helper'
    env[f'GIT_CONFIG_VALUE_{count}'] = ''
    if token:
        count += 1
        env[f'GIT_CONFIG_KEY_{count}'] = 'http.https://github.com/.extraheader'
        encoded = base64.b64encode(('x-access-token:' + token).encode()).decode()
        env[f'GIT_CONFIG_VALUE_{count}'] = 'AUTHORIZATION: basic ' + encoded
    env['GIT_CONFIG_COUNT'] = str(count + 1)
    return env


def repository_url(repository):
    return repository if repository.startswith('/') else 'https://github.com/' + repository + '.git'


def remote_head(repository, branch, token):
    """A fresh Git-protocol read, including at final landing gates; no disk writes."""
    ref = 'refs/heads/' + branch
    if subprocess.run(['git', 'check-ref-format', ref], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, check=False).returncode:
        raise ValueError('invalid branch ref')
    try:
        result = subprocess.run(['git', 'ls-remote', '--exit-code', '--refs',
                                 repository_url(repository), ref], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=authenticated_env(token()), timeout=read_budget.timeout(30), check=False)
    except subprocess.TimeoutExpired as exc:
        if read_budget.expired():
            raise Deferred('remote Git read is unfinished') from exc
        raise OSError('remote Git read timed out') from exc
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 1:
        raise OSError('remote Git branch could not be read')
    sha, name = lines[0].split('\t', 1)
    if name != ref or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('invalid remote Git branch response')
    return sha


def launch_worker(request, env):
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(request)],
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


class Cache:
    def __init__(self, directory, repository, token, *, deadline=read_budget.deadline.get):
        self.directory = Path(directory)
        self.repository = repository
        self.token = token
        self.deadline = deadline
        self.worker = None
        self.git_dir = self.directory / 'objects.git'
        if not _read_only.get():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (self.directory / 'request.lock').open('a') as lock:
                self._lock(lock)
                if not self.git_dir.exists():
                    if self.command('init', '--bare', str(self.git_dir), bare=False).returncode:
                        raise RuntimeError('could not initialize Git object cache')
        self.full_history()

    def _lock(self, lock):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Deferred('another Git cache request is being checkpointed') from exc

    def full_history(self):
        if (self.git_dir / 'shallow').exists():
            raise RuntimeError('Git object cache must contain full history')

    def command(self, *args, bare=True, input=None):
        limit = self.deadline()
        remaining = min(10., limit - time.monotonic()) if limit is not None else 10.
        if remaining <= 0:
            raise Deferred('supervisor read budget exhausted')
        try:
            return subprocess.run(['git', *(['--git-dir', str(self.git_dir)] if bare else []), *args],
                                  input=input, text=True, encoding='utf-8', errors='surrogateescape',
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_NO_LAZY_FETCH='1'),
                                  timeout=remaining, check=False)
        except subprocess.TimeoutExpired as exc:
            raise Deferred('local Git read is unfinished') from exc

    def missing(self, heads, *, kind='commit'):
        heads = list(dict.fromkeys(heads))
        if not heads:
            return []
        if not all(re.fullmatch(r'[0-9a-f]{40}', head) for head in heads):
            raise ValueError('invalid Git object identity')
        if not self.git_dir.exists():
            return heads
        result = self.command('cat-file', '--batch-check=%(objectname) %(objecttype)',
                              input='\n'.join(heads) + '\n')
        if result.returncode:
            raise RuntimeError('could not inspect Git object cache')
        lines = result.stdout.splitlines()
        if len(lines) != len(heads):
            raise RuntimeError('incomplete Git object response')
        return [head for head, line in zip(heads, lines) if line != f'{head} {kind}']

    def prepare(self, heads, *, kind='commit'):
        self.full_history()
        if not self.missing(heads, kind=kind) or _read_only.get():
            return True
        # Serializing the checkpoint as well as the worker prevents service and
        # cron clients from launching duplicate fetches from separate snapshots.
        with (self.directory / 'request.lock').open('a') as lock:
            self._lock(lock)
            missing = self.missing(heads, kind=kind)
            if not missing:
                return True
            job_path = self.directory / 'fetch.json'
            job = json.loads(job_path.read_text()) if job_path.exists() else None
            if job:
                receipt = self.directory / (job['id'] + '.json')
                if not receipt.exists() and time.time() - job['started'] < 360:
                    return False
                if receipt.exists():
                    missing = self.missing(heads, kind=kind)
                    if not missing:
                        return True
                    # Failed historical fetches permit the authoritative API
                    # fallback. Errors themselves never become ancestry facts.
                    outcome = json.loads(receipt.read_text())
                    if kind == job.get('kind', 'commit') and set(missing).issubset(job['heads']):
                        # Older workers only supplied ok. Modern receipts
                        # distinguish unreachable tips from auth/network errors.
                        unavailable = outcome.get('unavailable', job['heads'] if not outcome['ok'] else [])
                        if set(missing).issubset(unavailable):
                            return True
                        if not outcome['ok'] and time.time() - job['started'] < 420:
                            raise Deferred('Git fetch failed; awaiting retry')
            env = authenticated_env(self.token())  # acquire before checkpoint
            job = {'id': uuid.uuid4().hex, 'started': time.time(), 'heads': missing,
                   'repository': self.repository, 'kind': kind}
            request = self.directory / (job['id'] + '.request.json')
            atomic_json(request, job)
            atomic_json(job_path, job)
            try:
                self.worker = launch_worker(request, env)
            except OSError:
                job_path.unlink(missing_ok=True)
                request.unlink(missing_ok=True)
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

    def require(self, heads, *, kind='commit'):
        if not self.prepare(heads, kind=kind):
            raise Deferred('fetching Git objects for local reads')
        if self.missing(heads, kind=kind):
            raise OSError('required immutable Git objects are unavailable')

    def memo(self, key, compute):
        """Only immutable successful values, shared across processes and batches."""
        path = self.directory / ('value-' + hashlib.sha256(json.dumps(key).encode()).hexdigest() + '.json')
        if path.exists():
            value = json.loads(path.read_text())
            if value['key'] == key:
                return value['value']
        value = compute()
        if not _read_only.get():
            atomic_json(path, {'key': key, 'value': value})
        return value

    def ancestor(self, a, b, fallback):
        def compute():
            if not self.prepare([a, b]):
                raise Deferred('fetching commit history for ancestry checks')
            if self.missing([a, b]):
                return fallback(a, b)
            result = self.command('merge-base', '--is-ancestor', a, b)
            if result.returncode not in (0, 1):
                raise RuntimeError('local commit ancestry could not be established')
            return result.returncode == 0
        self.full_history()
        return self.memo(['ancestor', a, b], compute)

    def tree(self, head, fallback=None):
        def compute():
            if not self.prepare([head]):
                raise Deferred('fetching commit history for tree identity')
            if self.missing([head]):
                if fallback is not None:
                    return fallback(head)
                raise OSError('required commit is unavailable')
            result = self.command('rev-parse', head + '^{tree}')
            tree = result.stdout.strip()
            if result.returncode or not re.fullmatch(r'[0-9a-f]{40}', tree):
                raise OSError('commit tree could not be read')
            return tree
        return self.memo(['tree', head], compute)

    def behind(self, head, master):
        def compute():
            self.require([head, master])
            result = self.command('rev-list', '--count', head + '..' + master)
            if result.returncode or not result.stdout.strip().isdigit():
                raise OSError('behind count could not be read')
            return int(result.stdout)
        self.full_history()
        return self.memo(['behind', head, master], compute)

    def changed_paths(self, base, head):
        def compute():
            self.require([base, head])
            # Rename endpoints appear as delete/add. Disabling rename detection
            # needs only trees, avoiding downloads of unrelated file blobs.
            result = self.command('diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                                  '--name-only', '-z', base + '...' + head, '--')
            if result.returncode:
                raise OSError('changed paths could not be read')
            return sorted(set(result.stdout.split('\0')) - {''})
        self.full_history()
        return self.memo(['paths-v1', base, head], compute)

    def file(self, head, path):
        self.require([head])
        result = self.command('rev-parse', '--verify', head + ':' + path)
        blob = result.stdout.strip()
        if result.returncode or not re.fullmatch(r'[0-9a-f]{40}', blob):
            raise OSError('base-pinned file does not exist')
        self.require([blob], kind='blob')
        result = self.command('cat-file', 'blob', blob)
        if result.returncode:
            raise OSError('base-pinned file could not be read')
        return result.stdout


def fetch_worker(request):
    job = json.loads(request.read_text())
    directory = request.parent
    ok = False
    unavailable = []
    try:
        with (directory / 'fetch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            git = ['git', '--git-dir', str(directory / 'objects.git')]
            url = repository_url(job['repository'])
            deadline = time.monotonic() + 300
            kind = job.get('kind', 'commit')  # accepts pre-upgrade requests

            def run(args):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('Git cache fetch', 300)
                return subprocess.run(git + args, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                      timeout=remaining, check=False)

            fetch = ['-c', 'gc.auto=0', 'fetch', '--no-tags', '--no-write-fetch-head', '--filter=blob:none', url]
            # A named promisor remote lets Git's connectivity checks accept
            # omitted blobs. Its saved URL contains no authentication material.
            for name, value in [('url', url), ('promisor', 'true'), ('partialclonefilter', 'blob:none')]:
                if run(['config', 'remote.origin.' + name, value]).returncode:
                    raise OSError('could not configure Git object remote')
            fetch[-1] = 'origin'
            if kind == 'commit':
                # One incremental negotiation downloads all current branch and
                # PR histories. Most comparisons never need another network read.
                run(fetch + ['+refs/heads/*:refs/remotes/origin/*', '+refs/pull/*/head:refs/pull/*/head'])
            missing = []
            for head in job['heads']:
                if run(['cat-file', '-e', head + '^{' + kind + '}']).returncode:
                    missing.append(head)
                else:
                    run(['update-ref', 'refs/' + kind + 's/' + head, head])
            # Detached/force-pushed historical tips and the occasional pinned
            # classifier blob are fetched in batches, not per comparison.
            for offset in range(0, len(missing), 32):
                tips = missing[offset:offset + 32]
                if run(fetch + [h + ':refs/' + kind + 's/' + h for h in tips]).returncode:
                    for head in tips:
                        if run(['cat-file', '-e', head + '^{' + kind + '}']).returncode:
                            result = run(fetch + [head + ':refs/' + kind + 's/' + head])
                            if result.returncode and any(marker in result.stderr.lower() for marker in
                                                         (b'not our ref', b'unadvertised object',
                                                          b"couldn't find remote ref")):
                                unavailable.append(head)
            ok = all(run(['cat-file', '-e', h + '^{' + kind + '}']).returncode == 0 for h in job['heads'])
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        # No stderr (which may contain authentication details) leaves the worker.
        atomic_json(directory / (job['id'] + '.json'), {'ok': ok, 'unavailable': unavailable})
        request.unlink(missing_ok=True)


if __name__ == '__main__':
    fetch_worker(Path(sys.argv[1]))
