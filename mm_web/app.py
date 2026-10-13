"""ASGI application: authenticated dashboard reads and release campaigns."""
from __future__ import annotations
from pathlib import Path
import sqlite3
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from mm_web import auth, data, db, models as m, releases
from mm_web.errors import ApiError, SafeErrorMiddleware

ROOT = Path(__file__).resolve().parents[1]
Limit = Annotated[int, Query(ge=1, le=500)]
User = Annotated[m.Me, Depends(auth.identity)]
Maintainer = Annotated[m.Me, Depends(auth.require_release)]
Connection = Annotated[sqlite3.Connection | None, Depends(db.connection)]
ERRORS = {code: {'model': m.ErrorResponse} for code in [400, 401, 403, 404, 408, 422, 429, 502, 503]}


def create_app():
    app = FastAPI(title='Merge Marshall', version='1.0.0', openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(auth.DashboardSessionMiddleware)
    app.add_middleware(SafeErrorMiddleware)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        detail = exc.detail if isinstance(exc, ApiError) else {'code': 'request_failed', 'message': 'Request failed'}
        return JSONResponse(status_code=exc.status_code, content={'error': detail})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Validation errors may contain submitted tokens, SQL or other input.
        return JSONResponse(status_code=422, content={'error': {'code': 'invalid_request', 'message': 'Request parameters are invalid'}})

    @app.get('/api/v1/healthz', response_model=m.Liveness, tags=['health'])
    def healthz():
        return m.Liveness()

    api = APIRouter(prefix='/api/v1', responses=ERRORS)

    @api.get('/me', response_model=m.Me, tags=['identity'])
    def me(user: User):
        return user

    @api.get('/overview', response_model=m.Overview, tags=['dashboard'])
    def overview(user: User, conn: Connection):
        return data.overview(conn)

    @api.get('/batches', response_model=m.BatchesPage, tags=['batches'])
    def batches(user: User, conn: Connection, state: Literal['active', 'recent', 'all'] = 'all',
                kind: str | None = None, limit: Limit = 50, before: str | None = None):
        return data.batches(conn, state, kind, limit, before)

    @api.get('/batches/{batch_id}', response_model=m.BatchDetail, tags=['batches'])
    def batch(batch_id: str, user: User, conn: Connection):
        return data.detail(conn, batch_id)

    @api.get('/queue', response_model=m.Queue, tags=['dashboard'])
    def queue(user: User, conn: Connection):
        return data.queue(conn)

    @api.get('/outbox', response_model=m.OutboxPage, tags=['dashboard'])
    def outbox(user: User, conn: Connection, pending: bool = True, limit: Limit = 50, before: str | None = None):
        return data.outbox(conn, pending, limit, before)

    @api.get('/ledger', response_model=m.LedgerPage, tags=['agents'])
    def ledger(user: User, conn: Connection, status: str = 'open', limit: Limit = 50, before: str | None = None):
        return data.ledger(conn, status, limit, before)

    @api.get('/triage', response_model=m.TriagePage, tags=['agents'])
    def triage(user: User, conn: Connection, limit: Limit = 50, before: str | None = None):
        return data.triage(conn, limit, before)

    @api.get('/repairs', response_model=m.Repairs, tags=['agents'])
    def repairs(user: User, conn: Connection, limit: Limit = 50, before: str | None = None):
        return data.repairs(conn, limit, before)

    @api.get('/health', response_model=m.Health, tags=['health'])
    def health(user: User, conn: Connection):
        return data.health(conn)

    @api.get('/stats', response_model=m.Stats, tags=['dashboard'])
    def stats(user: User, conn: Connection, days: Annotated[int, Query(ge=1, le=366)] = 30):
        return data.stats(conn, days)

    @api.get('/schema', response_model=m.Schema, tags=['queries'])
    def schema(user: User, conn: Connection):
        return db.schema(conn)

    @api.post('/sql', response_model=m.SqlResult, tags=['queries'])
    def sql(body: m.SqlRequest, user: User, conn: Connection):
        return db.query(conn, body)

    @api.get('/releases/resolve', response_model=m.Resolution, tags=['releases'])
    def resolve(user: User, commitish: str | None = None):
        return releases.resolve(commitish)

    @api.post('/releases', response_model=m.Campaign, tags=['releases'], responses={409: {'model': m.ErrorResponse}})
    def create_release(body: m.CreateRelease, user: Maintainer):
        return releases.create(body.sha, user.login, body.idempotency_key)

    @api.get('/releases', response_model=list[m.Campaign], tags=['releases'])
    def release_list(user: User, limit: Limit = 50):
        return releases.list_campaigns(limit)

    @api.get('/releases/{id}', response_model=m.Campaign, tags=['releases'])
    def release_detail(id: str, user: User):
        return releases.get(id)

    @api.post('/releases/{id}/abort', response_model=m.Campaign, tags=['releases'])
    def release_abort(id: str, user: Maintainer):
        return releases.abort(id, user.login)

    @api.get('/openapi.json', response_model=dict, include_in_schema=False)
    def openapi(user: User):
        return app.openapi()

    @api.get('/docs', include_in_schema=False)
    def docs(user: User):
        return get_swagger_ui_html(openapi_url='/api/v1/openapi.json', title='Merge Marshall API')

    app.include_router(api)

    @app.get('/auth/login', include_in_schema=False)
    async def login(request: Request):
        return await auth.oauth_client().authorize_redirect(request, auth.public_url() + '/auth/callback')

    @app.get('/auth/callback', include_in_schema=False)
    async def callback(request: Request):
        try:
            token = await auth.oauth_client().authorize_access_token(request)
            login = await run_in_threadpool(auth.github_user, token['access_token'])
            # Do not put the OAuth token, scopes or provider state in the session.
            del token
            request.session.clear()
            permission = await run_in_threadpool(auth.permission, login)
            if permission is None:
                raise ApiError(403, 'insufficient_permission', 'Repository write permission is required')
            request.session.update(login=login, issued_at=time.time())
            return RedirectResponse('/', status_code=303)
        except ApiError:
            request.session.clear()
            raise
        except Exception:
            request.session.clear()
            raise ApiError(401, 'oauth_failed', 'GitHub sign-in could not be completed') from None

    @app.post('/auth/logout', response_model=m.Logout, responses=ERRORS)
    def logout(request: Request, user: User):
        request.session.clear()
        return m.Logout()

    @app.get('/{path:path}', include_in_schema=False)
    def static(request: Request, path: str):
        if path.startswith(('api/', 'auth/')):
            raise ApiError(404, 'not_found', 'Route was not found')
        try:
            auth.identity(request)
        except ApiError as exc:
            if exc.status_code == 401:
                return RedirectResponse('/auth/login', status_code=303)
            raise
        root = (ROOT / 'web' / 'dist').resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ApiError(404, 'not_found', 'Page was not found')
        if target.is_file():
            return FileResponse(target)
        index = (root / 'index.html').resolve()
        if not index.is_relative_to(root):
            raise ApiError(404, 'not_found', 'Page was not found')
        if index.is_file():
            return FileResponse(index)
        return PlainTextResponse('Merge Marshall dashboard API is running. Build web/ to enable the UI.\n')

    return app


app = create_app()
