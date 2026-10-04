import json
import threading
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.clients import Clients, CookieExpired, DoubanError, RequestGate, SyncError
from app.db import Store
from app.main import create_app
from app.models import Config
from app.notifications import send_bark
from app.sync import SyncService


def config(**changes):
    return Config(**{'douban_user':'123', 'douban_cookie':'dbcl2="123:fake"; ck=old; bid=before', 'seerr_url':'http://seerr:5055', 'seerr_api_key':'test-only', **changes})  # pragma: allowlist secret (synthetic test credentials)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(RequestGate, 'wait', lambda self, maximum, stop: not stop.is_set())


def test_server_cookie_and_csrf_refresh_are_used_by_next_request():
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.host=='www.douban.com':
            return httpx.Response(200, headers=[('Set-Cookie','bid=after; Domain=.douban.com; Path=/'),('Set-Cookie','untrusted=ignore; Domain=evil.test'),('Set-Cookie','dbcl2="123:new"; Domain=.douban.com; Path=/')],text='<div class="nav-user-account"></div><input name="ck" value="fresh">')
        assert 'ck=fresh' in request.headers['cookie']
        assert 'bid=after' in request.headers['cookie']
        assert 'dbcl2="123:new"' in request.headers['cookie']
        assert 'untrusted' not in request.headers['cookie']
        return httpx.Response(200,text='<div class="grid-view"></div>')
    client=Clients(config(),transport=httpx.MockTransport(handler))
    client.verify_douban()
    assert len(calls)==2
    assert client.cookie!=client.config.douban_cookie
    client.close()


def test_sync_refresh_persists_encrypted_without_new_notification_revision(tmp_path):
    store=Store(tmp_path)
    store.save_config(config())
    revision=store.cookie_health()['revision']
    def handler(request):
        if request.url.host=='www.douban.com':
            return httpx.Response(200,headers={'Set-Cookie':'ck=new; Domain=.douban.com; Path=/'},text='<div class="nav-user-account"></div>')
        return httpx.Response(200,text='<div class="grid-view"></div>')
    service=SyncService(store,lambda cfg,stop:Clients(cfg,stop,transport=httpx.MockTransport(handler)))
    for _ in range(2):
        service.start(preview=True);service.thread.join(3)
        assert not service.running
        assert store.rows('SELECT * FROM runs ORDER BY id DESC LIMIT 1')[0]['state']=='complete'
    assert store.config().douban_cookie.endswith('bid=before')
    assert 'ck=new' in store.config().douban_cookie
    assert store.cookie_health()['revision']==revision
    assert store.cookie_health(public=True)['status']=='valid'
    assert store.cookie_health(public=True)['refreshed']
    assert store.config().douban_cookie not in store.rows('SELECT value FROM settings')[0]['value']


def test_refresh_never_overwrites_new_import_delete_or_rule_changes(tmp_path):
    store=Store(tmp_path);store.save_config(config())
    initial=store.config();revision=store.cookie_health()['revision']
    store.update_config(lambda current:current.model_copy(update={'history_days':99}))
    assert store.refresh_cookie(initial.douban_cookie,initial.douban_user,revision,'dbcl2="123:fake"; ck=fresh')
    assert store.config().history_days==99
    store.import_cookie(initial.douban_cookie,initial.douban_user)
    assert not store.refresh_cookie(initial.douban_cookie,initial.douban_user,revision,'dbcl2="123:fake"; ck=stale')
    new_revision=store.cookie_health()['revision']
    store.update_config(lambda current:current.model_copy(update={'douban_cookie':''}))
    assert not store.refresh_cookie(initial.douban_cookie,initial.douban_user,new_revision,initial.douban_cookie)
    assert not store.config().douban_cookie


def test_cookie_expiration_notifies_once_across_restart_and_reimport(tmp_path,monkeypatch):
    sent=[]
    monkeypatch.setattr('app.sync.send_bark',lambda url:sent.append(url))
    store=Store(tmp_path);store.save_config(config(bark_url='https://bark.test/device'))
    def expired(request):return httpx.Response(302,headers={'Location':'https://accounts.douban.com/login'})
    factory=lambda cfg,stop:Clients(cfg,stop,transport=httpx.MockTransport(expired))
    for current_store in (store,store,Store(tmp_path)):
        service=SyncService(current_store,factory)
        service.start(preview=True);service.thread.join(3)
        assert current_store.rows('SELECT * FROM runs ORDER BY id DESC LIMIT 1')[0]['state']=='failed'
    assert len(sent)==1
    assert store.cookie_health(public=True)['notification']=='sent'
    store.import_cookie(config().douban_cookie,'123')
    service=SyncService(store,factory)
    service.start(preview=True);service.thread.join(3)
    assert len(sent)==2


def test_notification_failure_retries_and_stale_revision_is_ignored(tmp_path,monkeypatch):
    store=Store(tmp_path);store.save_config(config(bark_url='https://bark.test/device'))
    service=SyncService(store)
    revision=store.cookie_health()['revision']
    def failed(url):raise SyncError('delivery failed')
    monkeypatch.setattr('app.sync.send_bark',failed)
    service.cookie_invalid(store.config(),revision)
    assert store.cookie_health(public=True)['notification']=='failed'
    sent=[]
    monkeypatch.setattr('app.sync.send_bark',lambda url:sent.append(url))
    service.cookie_invalid(store.config(),revision)
    assert len(sent)==1
    store.import_cookie(config().douban_cookie,'123')
    service.cookie_invalid(store.config(),revision)
    assert len(sent)==1
    assert store.cookie_health(public=True)['status']=='valid'


@pytest.mark.parametrize('response',[httpx.Response(429),httpx.Response(500),httpx.Response(200,text='<div>unknown layout</div>')])
def test_ordinary_errors_do_not_send_cookie_invalid_notification(tmp_path,monkeypatch,response):
    sent=[]
    monkeypatch.setattr('app.sync.send_bark',lambda url:sent.append(url))
    store=Store(tmp_path);store.save_config(config(bark_url='https://bark.test/device'))
    factory=lambda cfg,stop:Clients(cfg,stop,transport=httpx.MockTransport(lambda req:response))
    service=SyncService(store,factory)
    # An unrecognizable login page means authentication cannot be verified, so
    # use a valid login header then exercise the unknown wish-page layout.
    if response.status_code==200:
        def handler(req):
            return httpx.Response(200,text='<div class="nav-user-account"></div>') if req.url.host=='www.douban.com' else response
        service=SyncService(store,lambda cfg,stop:Clients(cfg,stop,transport=httpx.MockTransport(handler)))
    service.start(preview=True);service.thread.join(3)
    assert not sent
    assert store.cookie_health(public=True)['status']!='invalid'


def test_bark_url_encryption_blank_preserves_deletion_and_api_redaction(tmp_path,monkeypatch):
    app=create_app(tmp_path,admin_password='notification-test-password',scheduler=False)
    sent=[]
    monkeypatch.setattr('app.main.send_bark',lambda url,test:sent.append((url,test)))
    endpoint='https://bark.test/device?level=active'
    with TestClient(app) as web:
        web.auth=('admin','notification-test-password')
        web.headers['X-Requested-With']='douban-reel'
        response=web.put('/api/config',json={'bark_url':endpoint})
        assert response.status_code==200
        assert response.json()['bark_url_saved']
        assert endpoint not in response.text
        assert endpoint not in app.state.store.rows('SELECT value FROM settings')[0]['value']
        web.put('/api/config',json={'bark_url':''})
        assert app.state.store.config().bark_url==endpoint
        assert web.post('/api/test/bark').status_code==200
        assert sent==[(endpoint,True)]
        assert endpoint not in web.get('/api/status').text
        assert 'revision' not in web.get('/api/status').json()['cookie_health']
        assert web.delete('/api/bark').status_code==200
        assert not web.get('/api/config').json()['bark_url_saved']


def test_bark_delivery_encoding_query_and_secret_safe_errors():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'code':200})
    send_bark('https://bark.test/device?level=active',transport=httpx.MockTransport(handler))
    assert requests[0].url.params['level']=='active'
    assert requests[0].url.params['group']=='豆瓣映单'
    assert 'Cookie 已失效' in unquote(requests[0].url.path)
    assert 'dbcl2' not in unquote(requests[0].url.path)
    for response in [httpx.Response(500,text='private-device'),httpx.Response(200,json={'code':400,'message':'private-device'}),httpx.Response(200,text='private-device'),httpx.Response(302,headers={'Location':'https://evil.test'})]:
        with pytest.raises(SyncError) as error:
            send_bark('https://bark.test/private-device',transport=httpx.MockTransport(lambda req:response))
        assert 'private-device' not in str(error.value)


@pytest.mark.parametrize('url',['ftp://bark.test/device','https://user:pass@bark.test/device','https://bark.test','https://bark.test/device#secret','https://bark.test:bad/device'])  # pragma: allowlist secret (invalid synthetic URL)
def test_invalid_bark_url(url):
    with pytest.raises(ValidationError):Config(bark_url=url)


@pytest.mark.parametrize('response',[
    httpx.Response(401),httpx.Response(403),
    httpx.Response(200,text='<form action="/accounts/login">login</form>'),
    httpx.Response(200,text='<input name="captcha-solution">'),
    httpx.Response(200,headers={'Set-Cookie':'dbcl2=deleted; Max-Age=0; Domain=.douban.com'}),
])
def test_authentication_failures_are_distinct_from_network_errors(response):
    client=Clients(config(),transport=httpx.MockTransport(lambda request:response))
    with pytest.raises(CookieExpired):client.verify_douban()
    client.close()


def test_network_error_is_not_cookie_expired():
    def offline(request):raise httpx.ConnectError('private endpoint',request=request)
    client=Clients(config(),transport=httpx.MockTransport(offline))
    with pytest.raises(DoubanError) as error:client.verify_douban()
    assert not isinstance(error.value,CookieExpired)
    assert 'private endpoint' not in str(error.value)
    client.close()


def test_concurrent_failure_sends_one_notification(tmp_path,monkeypatch):
    store=Store(tmp_path);store.save_config(config(bark_url='https://bark.test/device'))
    service=SyncService(store)
    revision=store.cookie_health()['revision']
    sent=[]
    monkeypatch.setattr('app.sync.send_bark',lambda url:sent.append(url))
    threads=[threading.Thread(target=service.cookie_invalid,args=(store.config(),revision)) for _ in range(3)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(3)
    assert len(sent)==1


def test_stale_automatic_cookie_failure_does_not_invalidate_latest_cookie(tmp_path,monkeypatch):
    store=Store(tmp_path);store.save_config(config(bark_url='https://bark.test/device'))
    original=store.config();revision=store.cookie_health()['revision']
    assert store.refresh_cookie(original.douban_cookie,original.douban_user,revision,'dbcl2="123:fake"; ck=fresh')
    sent=[]
    monkeypatch.setattr('app.sync.send_bark',lambda url:sent.append(url))
    SyncService(store).cookie_invalid(original,revision)
    assert not sent
    assert store.cookie_health(public=True)['status']=='valid'
