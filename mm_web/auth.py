"""GitHub user identity and App-authorized repository permissions."""
from __future__ import annotations
import hashlib
import json
import os
import re
import stat
import threading
import time
from urllib.parse import urlsplit

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import Request, Depends
from fastapi.security import HTTPBearer
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
import monitor
from mm_web import tokens
from mm_web.errors import ApiError
from mm_web.models import Me

TTL = 600
CACHE_MAX = 4096
TOKEN_CACHE: dict[str, tuple[float, str]] = {}
PERMISSION_CACHE: dict[str, tuple[float, str | None]] = {}
CACHE_LOCK = threading.Lock()
LOGIN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}$')
bearer_scheme = HTTPBearer(auto_error=False)


def public_url():
    value = os.environ.get('MM_WEB_PUBLIC_URL', 'http://localhost:8770').rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.path not in {'', '/'} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('MM_WEB_PUBLIC_URL must be an HTTP(S) origin')
    return value


class DashboardSessionMiddleware:
    """Delay key creation until serving requests; OpenAPI export has no effects."""
    def __init__(self, app):
        self.app = app
        self.wrapped = None
        self.lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') == '/api/v1/healthz':
            await self.app(scope, receive, send)
            return
        with self.lock:
            if self.wrapped is None:
                secret = tokens.key(monitor.STATE_DIR / 'mm-web-session.key', create=True)
                self.wrapped = SessionMiddleware(CsrfMiddleware(self.app), secret_key=secret.hex(),
                    session_cookie='mm_web_session', max_age=7 * 86400,
                    same_site='lax', https_only=public_url().startswith('https://'))
        await self.wrapped(scope, receive, send)


class CsrfMiddleware:
    """Reject cookie writes before FastAPI parses an untrusted request body."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            request = Request(scope)
            if scope.get('session', {}).get('login') and not request.headers.get('authorization'):
                try:
                    csrf(request)
                except ApiError as exc:
                    await JSONResponse({'error': exc.detail}, status_code=exc.status_code)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def cached(cache, name):
    with CACHE_LOCK:
        value = cache.get(name)
        if value and value[0] > time.monotonic():
            return True, value[1]
        cache.pop(name, None)
    return False, None


def remember(cache, name, value):
    with CACHE_LOCK:
        now = time.monotonic()
        for expired in [k for k, v in cache.items() if v[0] <= now]:
            cache.pop(expired, None)
        if len(cache) >= CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[name] = (now + TTL, value)
    return value


def github_user(token):
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    hit, login = cached(TOKEN_CACHE, fingerprint)
    if hit:
        return login
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get('https://api.github.com/user', headers={
                'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
        if response.status_code in {401, 403}:
            raise ApiError(401, 'invalid_credentials', 'GitHub credentials were rejected')
        response.raise_for_status()
        login = response.json().get('login')
        if not isinstance(login, str) or not LOGIN.fullmatch(login):
            raise ValueError('invalid login')
    except ApiError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise ApiError(503, 'github_unavailable', 'GitHub identity lookup is unavailable') from None
    return remember(TOKEN_CACHE, fingerprint, login)


def permission(login):
    if not LOGIN.fullmatch(login):
        raise ApiError(401, 'invalid_credentials', 'Invalid login')
    hit, level = cached(PERMISSION_CACHE, login.casefold())
    if hit:
        return level
    try:
        # Explicitly obtain the App token even in dev: no ambient gh fallback.
        token = monitor.github_app_token()
        if not token:
            raise ValueError('App token missing')
        endpoint = f'https://api.github.com/repos/{monitor.REPO_NAME}/collaborators/{login}/permission'
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(endpoint, headers={'Authorization': 'Bearer ' + token,
                'Accept': 'application/vnd.github+json'})
            if response.status_code == 401:
                token = monitor.github_app_token(force_refresh=True)
                if not token:
                    raise ValueError('App token missing')
                response = client.get(endpoint, headers={'Authorization': 'Bearer ' + token,
                    'Accept': 'application/vnd.github+json'})
        if response.status_code == 404:
            level = None
        else:
            response.raise_for_status()
            permissions = response.json().get('user', {}).get('permissions', {})
            level = next((label for label, key in [('admin', 'admin'), ('maintain', 'maintain'), ('write', 'push')]
                          if permissions.get(key) is True), None)
    except (monitor.CommandError, OSError, httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise ApiError(503, 'github_unavailable', 'GitHub permission lookup is unavailable') from None
    return remember(PERMISSION_CACHE, login.casefold(), level)


def csrf(request):
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ApiError(403, 'csrf_rejected', 'Cookie requests require application/json')
        origin = request.headers.get('origin')
        if origin is not None and origin != public_url():
            raise ApiError(403, 'csrf_rejected', 'Origin does not match the dashboard')


def identity(request: Request, _credentials=Depends(bearer_scheme)):
    header = request.headers.get('authorization')
    if header:
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            raise ApiError(401, 'invalid_credentials', 'Expected a bearer token')
        token = parts[1]
        if token.startswith('mmw_'):
            name = tokens.verify(token)
            if name is None:
                raise ApiError(401, 'invalid_credentials', 'Invalid service token')
            return Me(login=name, permission='write', can_release=False, auth='service_token')
        login = github_user(token)
        method = 'github_token'
    else:
        login = request.session.get('login')
        issued = request.session.get('issued_at')
        if (not isinstance(login, str) or not isinstance(issued, (float, int))
                or not 0 <= time.time() - issued < 7 * 86400):
            raise ApiError(401, 'authentication_required', 'Authentication is required')
        csrf(request)
        method = 'session'
    level = permission(login)
    if level is None:
        raise ApiError(403, 'insufficient_permission', 'Repository write permission is required')
    return Me(login=login, permission=level, can_release=level in {'maintain', 'admin'}, auth=method)


def require_release(user: Me = Depends(identity)):
    if not user.can_release or user.auth == 'service_token':
        raise ApiError(403, 'insufficient_permission', 'Release actions require maintain or admin permission')
    return user


def oauth_client():
    path = monitor.CONFIG_DIR / 'mm-web-oauth.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            if stat.S_IMODE(os.fstat(handle.fileno()).st_mode) != 0o600:
                raise ValueError('OAuth configuration must be mode 0600')
            settings = json.load(handle)
        client_id, client_secret = settings['client_id'], settings['client_secret']
        if not client_id or not client_secret:
            raise ValueError('OAuth credentials missing')
    except (OSError, ValueError, KeyError, TypeError):
        raise ApiError(503, 'oauth_unavailable', 'GitHub OAuth is not configured') from None
    oauth = OAuth()
    oauth.register('github', client_id=client_id, client_secret=client_secret,
        authorize_url='https://github.com/login/oauth/authorize',
        access_token_url='https://github.com/login/oauth/access_token',
        api_base_url='https://api.github.com/', client_kwargs={'scope': 'read:user'})
    return oauth.github
