import asyncio
import fcntl
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ValidationError

from .clients import Clients, CookieExpired, SyncError
from .db import Store
from .models import Config, Mapping
from .sync import SyncService, scope_for
from .notifications import send_bark


STATIC = Path(__file__).parent / 'static'


def create_app(data_dir=None, admin_password=None, scheduler=True):
    password = admin_password or os.environ.get('ADMIN_PASSWORD', '')
    username = os.environ.get('ADMIN_USERNAME', 'admin')
    directory = data_dir or os.environ.get('DATA_DIR', './data')

    @asynccontextmanager
    async def lifespan(app):
        if len(password) < 12:
            raise RuntimeError('ADMIN_PASSWORD 至少需要 12 个字符；先运行 python3 scripts/init_env.py')
        Path(directory).mkdir(parents=True, exist_ok=True)
        lock_file = open(Path(directory) / 'instance.lock', 'w')
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError('数据目录已由另一实例使用；请仅运行一个 worker') from None
        app.state.store = Store(directory)
        app.state.sync = SyncService(app.state.store)
        async def loop():
            while True:
                await asyncio.to_thread(app.state.sync.tick)
                await asyncio.sleep(5)
        task = asyncio.create_task(loop()) if scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            app.state.sync.stop.set()
            if app.state.sync.thread:
                await asyncio.to_thread(app.state.sync.thread.join, 40)
            lock_file.close()

    app = FastAPI(title='豆瓣映单 · Douban Reel', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)

    def auth(credentials: HTTPBasicCredentials | None = Depends(basic)):
        if not credentials or not (secrets.compare_digest(credentials.username.encode(), username.encode()) and secrets.compare_digest(credentials.password.encode(), password.encode())):
            raise HTTPException(401, '请使用管理员账号登录', headers={'WWW-Authenticate': 'Basic realm="Douban Reel", charset="UTF-8"'})

    def store():
        return app.state.store

    @app.middleware('http')
    async def security(request: Request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('x-requested-with') != 'douban-reel' or request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '请求来源校验失败'}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default error includes the submitted value; strip all credential inputs.
        return JSONResponse({'detail': '；'.join(e['msg'] for e in error.errors())}, status_code=422)

    @app.exception_handler(SyncError)
    async def sync_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    def parse_config(data):
        try:
            return Config(**data)
        except ValidationError as error:
            raise HTTPException(422, '；'.join(e['msg'] for e in error.errors())) from None

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/', dependencies=[Depends(auth)])
    def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/static/{filename}', dependencies=[Depends(auth)])
    def static(filename: str):
        if filename not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(STATIC / filename)

    @app.get('/api/config', dependencies=[Depends(auth)])
    def config():
        return store().public_config()

    @app.put('/api/config', dependencies=[Depends(auth)])
    def save_config(data: dict):
        service = app.state.sync
        def update(current):
            old = current.model_dump()
            allowed = set(old) - {'douban_cookie', 'douban_user'}
            old.update({k: v for k, v in data.items() if k in allowed and not (k in ('seerr_api_key', 'bark_url') and v == '')})
            updated = parse_config(old)
            if updated.enabled and not updated.ready():
                raise HTTPException(400, '开启定时同步前，请填写全部连接信息并验证保存 Cookie')
            return updated
        updated = store().update_config(update)
        service.reschedule()
        return store().public_config(updated)

    class CookieInput(BaseModel):
        cookie: str

    @app.post('/api/cookie', dependencies=[Depends(auth)])
    def save_cookie(data: CookieInput):
        service = app.state.sync
        if not service.validation_lock.acquire(blocking=False):
            raise HTTPException(409, '另一个连接或 Cookie 验证正在进行，请稍后重试')
        client = None
        try:
            config = parse_config({**store().config().model_dump(), 'douban_cookie': data.cookie, 'douban_user': ''})
            if not config.douban_cookie:
                raise HTTPException(400, '请填写豆瓣 Cookie')
            if not config.douban_user:
                raise HTTPException(400, 'Cookie 中未找到账号信息，请重新复制完整 Cookie')
            client = Clients(config, request_gate=service.douban_gate)
            message = client.verify_douban()
            # Verification may take time: merge only the verified credentials into the latest settings.
            store().import_cookie(client.cookie, config.douban_user)
            service.reschedule()
            return {'message': message + '；Cookie 已加密保存', 'douban_user': config.douban_user}
        finally:
            if client:
                client.close()
            service.validation_lock.release()

    @app.delete('/api/cookie', dependencies=[Depends(auth)])
    def delete_cookie():
        service = app.state.sync
        if not service.validation_lock.acquire(blocking=False):
            raise HTTPException(409, 'Cookie 正在验证，请稍后删除')
        try:
            store().update_config(lambda current: current.model_copy(update={'douban_cookie': '', 'enabled': False}))
            service.reschedule()
            return {'message': 'Cookie 已删除，定时同步已关闭；当前任务继续使用启动时的 Cookie，可手动停止'}
        finally:
            service.validation_lock.release()

    @app.post('/api/test/{target}', dependencies=[Depends(auth)])
    def test_connection(target: str):
        if target not in ('douban', 'seerr', 'bark'):
            raise HTTPException(404)
        service = app.state.sync
        if not service.validation_lock.acquire(blocking=False):
            raise HTTPException(409, '另一个连接或 Cookie 验证正在进行，请稍后重试')
        client = None
        with store().config_lock:
            config = store().config()
            revision = store().cookie_health()['revision']
        try:
            if target == 'bark':
                send_bark(config.bark_url, test=True)
                return {'message': 'Bark 测试通知已发送'}
            client = Clients(config, request_gate=service.douban_gate)
            if target == 'douban':
                message = client.verify_douban()
                store().refresh_cookie(config.douban_cookie, config.douban_user, revision, client.cookie)
            else:
                client.api('Seerr', 'GET', '/auth/me')
                message = target.upper() + ' 连接成功'
            return {'message': message}
        except CookieExpired:
            service.cookie_invalid(config, revision)
            raise
        finally:
            if client:
                client.close()
            service.validation_lock.release()

    @app.delete('/api/bark', dependencies=[Depends(auth)])
    def delete_bark():
        store().update_config(lambda current: current.model_copy(update={'bark_url': ''}))
        return {'message': 'Bark 推送地址已删除；运行中的任务继续使用启动时的配置'}

    @app.get('/api/status', dependencies=[Depends(auth)])
    def status():
        db, service = store(), app.state.sync
        scope = scope_for(db.config())
        return {'running': service.running, 'next_run': service.next_run.isoformat() if service.next_run else None,
                'settings_pending': service.active_config is not None and service.active_config != db.config(),
                'cookie_health': db.cookie_health(public=True),
                'runs': db.rows('SELECT * FROM runs ORDER BY id DESC LIMIT 20'),
                'items': db.rows('SELECT * FROM items WHERE scope=? ORDER BY updated DESC LIMIT 200', (scope,)),
                'counts': {r['state']: r['n'] for r in db.rows('SELECT state,COUNT(*) n FROM items WHERE scope=? GROUP BY state', (scope,))}}

    @app.post('/api/sync', dependencies=[Depends(auth)])
    def sync(preview: bool = False):
        try:
            return {'run_id': app.state.sync.start(preview)}
        except SyncError as error:
            raise HTTPException(409, str(error)) from None

    @app.post('/api/stop', dependencies=[Depends(auth)])
    def stop():
        app.state.sync.stop.set()
        return {'message': '已请求停止，当前网络请求结束后退出；已提交的请求不撤销'}

    @app.put('/api/mappings/{subject}', dependencies=[Depends(auth)])
    def mapping(subject: str, data: Mapping):
        if not subject.isdigit():
            raise HTTPException(400, '豆瓣 ID 必须为数字')
        with store().config_lock, store().connect() as db:
            db.execute('BEGIN IMMEDIATE')
            scope = scope_for(store().config())
            rows = db.execute('SELECT state FROM items WHERE scope=? AND subject=?', (scope, subject)).fetchall()
            if not rows or rows[0]['state'] in ('submitted', 'existing'):
                raise HTTPException(400, '仅支持为尚未同步的列表条目设置映射')
            db.execute('INSERT OR REPLACE INTO mappings VALUES (?,?)', (subject, data.model_dump_json()))
            db.execute("UPDATE items SET state='mapped',message='映射已保存，下次预览或同步生效' WHERE scope=? AND subject=?", (scope, subject))
            return {'message': '映射已保存，请先运行预览核对'}

    return app


app = create_app()
