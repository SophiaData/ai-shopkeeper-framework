"""Bound operations JSON upload memory before FastAPI parses base64 content."""
from starlette.responses import JSONResponse

MAX_REQUEST_BYTES = 8 * 1024 * 1024


class OperationsBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/api/admin/operations') or scope['method'] not in {'POST', 'PATCH', 'PUT'}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                return await JSONResponse({'detail': '请求体超过 8 MiB；图片上限为 5 MiB'}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        return await self.app(scope, replay, send)
