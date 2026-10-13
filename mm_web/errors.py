"""Safe errors: external provider output is never echoed to callers."""
from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str, *, campaign_id=None):
        detail = {'code': code, 'message': message}
        if campaign_id is not None:
            detail['campaign_id'] = campaign_id
        super().__init__(status, detail=detail)


class SafeErrorMiddleware:
    """Do not let ASGI server tracebacks echo raw provider/validation payloads."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        started = False

        async def safe_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception as exc:
            import logging
            from starlette.responses import JSONResponse
            logging.getLogger('mm_web').error('Dashboard request failed (%s)', type(exc).__name__)
            if not started:
                await JSONResponse(status_code=503, content={'error': {
                    'code': 'service_unavailable', 'message': 'Dashboard data is unavailable'}})(scope, receive, send)
