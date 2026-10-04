import json
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.clients import Clients, DoubanError, RequestGate, SyncError, parse_subject, parse_wish
from app.db import Store
from app.main import create_app
from app.models import Config
from app.sync import SyncService, scope_for


TODAY = datetime.now(ZoneInfo('Asia/Shanghai')).date()


def wish_html(entries, next_url=None):
    content = ''.join(f'<div class="item"><li class="title"><a href="https://movie.douban.com/subject/{sid}/">{title}</a></li><span class="date">{day}</span></div>' for sid, title, day in entries)
    return '<div class="grid-view">' + content + '</div>' + (f'<div class="paginator"><span class="next"><a href="{next_url}">后页</a></span></div>' if next_url else '')


def subject_html(title='电影', tv=False, imdb='tt1234567'):
    return f'<h1>{title}</h1><div id="info">IMDb: {imdb}' + (' 集数: 10 首播: 2025-01-01' if tv else ' 上映日期: 2025-01-01') + '</div>'


def configuration(**kwargs):
    return Config(**dict({'douban_user':'tester', 'douban_cookie':'dbcl2="123:abc"; ck=xyz', 'seerr_url':'http://seerr:5055', 'seerr_api_key':'secret-seerr'}, **kwargs))  # pragma: allowlist secret (synthetic test credentials)


class NoWait:
    def wait(self, seconds):
        return False

    def is_set(self):
        return False


def client_with(handler, **kwargs):
    return Clients(configuration(**kwargs), NoWait(), httpx.MockTransport(handler))


def test_defaults_and_input_validation():
    assert Config().history_days == 30
    assert Config().interval_minutes == 1440
    assert Config().request_delay == 15
    assert not Config().enabled
    assert Config(douban_user='https://www.douban.com/people/test/').douban_user == 'test'
    assert Config(seerr_url='http://seerr:5055/api/v1').seerr_url == 'http://seerr:5055'
    for data in ({'history_days':0}, {'interval_minutes':1}, {'douban_cookie':'dbcl2=x'}, {'douban_cookie':'dbcl2=x; ck=x\r\nAuthorization: x'}, {'seerr_url':'http://user:pass@host'}, {'douban_user':'../admin'}):  # pragma: allowlist secret (invalid synthetic URL)
        with pytest.raises(ValidationError):
            Config(**data)
    exported = json.dumps([{'name':'dbcl2','value':'x','domain':'.douban.com'}, {'name':'ck','value':'y','domain':'.douban.com'}, {'name':'foreign','value':'secret','domain':'evil.test'}])
    assert Config(douban_cookie=exported).douban_cookie == 'dbcl2=x; ck=y'


def test_cookie_encrypted_and_never_exposed(tmp_path):
    store = Store(tmp_path)
    config = configuration()
    store.save_config(config)
    assert store.config() == config
    raw = store.rows('SELECT value FROM settings')[0]['value']
    for secret in (config.douban_cookie, config.seerr_api_key):
        assert secret not in raw
        assert secret not in json.dumps(store.public_config())
    assert store.public_config()['douban_cookie_saved'] is True
    assert (tmp_path / 'secret.key').stat().st_mode & 0o777 == 0o600


def test_wish_and_subject_parsers():
    entries, next_page = parse_wish(wish_html([('1','电影 / Movie','2026-10-04')], '?start=15'))
    assert entries[0]['subject'] == '1'
    assert entries[0]['marked'] == '2026-10-04'
    assert next_page == '?start=15'
    assert parse_subject(subject_html('某剧 第十二季', True)) == {'imdb':'tt1234567','media_type':'tv','season':12}
    assert parse_subject(subject_html('Some Show Season 2', True))['season'] == 2
    for html in ('<form action="/accounts/login">登录</form>', '<h1>异常请求</h1>', '<p>unknown layout</p>'):
        with pytest.raises(DoubanError):
            parse_wish(html)
    assert parse_wish('<div class="grid-view"></div>')[0] == []


def test_pagination_cutoff_and_dedup():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.params.get('start') == '0':
            return httpx.Response(200, text=wish_html([('1','new','2026-10-04'),('2','boundary','2026-09-05')], '?start=15'))
        return httpx.Response(200, text=wish_html([('2','dup','2026-09-05'),('3','old','2026-09-04')]))
    with_client = client_with(handler)
    assert [e['subject'] for e in with_client.wish(date(2026,9,5))] == ['1','2']
    assert len(calls) == 2
    with_client.close()


def test_cookie_redirects_never_leak_off_douban():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'location':'https://accounts.douban.com/login'})
    client=client_with(handler)
    with pytest.raises(DoubanError):
        client.verify_douban()
    assert len(calls)==1
    client.close()


def test_numeric_user_pagination_uses_canonical_profile_alias():
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        if request.url.params.get('start')=='0':
            header='<div id="db-usr-profile"><a href="https://movie.douban.com/people/custom-name/">profile</a></div>'
            return httpx.Response(200,text=header+wish_html([('1','first','2026-10-04')],'/people/custom-name/wish?start=15'))
        assert request.url.path=='/people/custom-name/wish'
        return httpx.Response(200,text=wish_html([('2','second','2026-10-04')]))
    client=client_with(handler,douban_user='123456')
    assert [e['subject'] for e in client.wish(date(2026,9,5))]==['1','2']
    assert len(calls)==2
    client.close()


@pytest.mark.parametrize('next_url',[
    'https://evil.test/people/custom-name/wish?start=15',
    'http://movie.douban.com/people/custom-name/wish?start=15',
    'https://movie.douban.com:444/people/custom-name/wish?start=15',
    'https://someone@movie.douban.com/people/custom-name/wish?start=15',
    '/people/other-user/wish?start=15',
    '/people/tester/wishlist?start=15',
    '/people/custom-name/collect?start=15',
])
def test_pagination_rejects_unrelated_accounts_and_destinations(next_url):
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        header='<div id="db-usr-profile"><a href="/people/custom-name/">profile</a></div>'
        return httpx.Response(200,text=header+wish_html([('1','first','2026-10-04')],next_url))
    client=client_with(handler)
    with pytest.raises(DoubanError,match='分页地址不正确'):
        list(client.wish(date(2026,9,5)))
    assert len(calls)==1
    client.close()


def test_movie_preview_existing_and_request():
    writes=[]
    status={'value':1}
    def handler(request):
        if request.method=='GET':
            return httpx.Response(200,json={'id':10,'mediaInfo':{'status':status['value']}})
        writes.append(json.loads(request.content))
        return httpx.Response(201,json={'id':7})
    client=client_with(handler)
    match={'media_type':'movie','tmdb_id':10}
    assert client.request(match,True)[0]=='preview'
    assert not writes
    assert client.request(match)[0]=='submitted'
    assert writes==[{'mediaType':'movie','mediaId':10,'is4k':False}]
    status['value']=5
    assert client.request(match)[0]=='existing'
    assert len(writes)==1
    client.close()


def test_tv_subtracts_pending_and_existing_seasons():
    writes=[]
    def handler(request):
        if request.method=='GET':
            return httpx.Response(200,json={'seasons':[{'seasonNumber':s} for s in (0,1,2,3)],'mediaInfo':{'status':4,'seasons':[{'seasonNumber':1,'status':5}],'requests':[{'status':1,'is4k':False,'seasons':[{'seasonNumber':2}]}]}})
        writes.append(json.loads(request.content))
        return httpx.Response(201,json={'id':1})
    client=client_with(handler,tv_seasons='all')
    assert client.request({'media_type':'tv','tmdb_id':20})[0]=='submitted'
    assert writes[0]['seasons']==[3]
    assert client.request({'media_type':'tv','tmdb_id':20,'season':2})[0]=='existing'
    with pytest.raises(SyncError):
        client.request({'media_type':'tv','tmdb_id':20,'season':4})
    client.close()


def test_match_is_conservative():
    client=client_with(lambda request:httpx.Response(200,json={'results':[{'id':1,'mediaType':'movie'},{'id':2,'mediaType':'movie'}]}))
    with pytest.raises(SyncError):
        client.match({'media_type':'movie','imdb':'tt1234567','season':None})
    with pytest.raises(SyncError):
        client.match({'media_type':'movie','imdb':None,'season':None})
    client.close()


def test_safe_error_excludes_upstream_body_and_key():
    client=client_with(lambda request:httpx.Response(401,text='secret-seerr'))
    with pytest.raises(SyncError) as error:
        client.api('Seerr','GET','/auth/me')
    assert 'secret' not in str(error.value)
    client.close()


class FakeClients:
    requests=[]
    cutoffs=[]
    def __init__(self, config, stop):
        self.config=config
    def verify_douban(self):
        return 'ok'
    def wish(self, cutoff):
        self.cutoffs.append(cutoff)
        yield {'subject':'1','title':'Movie','marked':TODAY.isoformat()}
        yield {'subject':'2','title':'Show 第三季','marked':TODAY.isoformat()}
    def douban(self,url):
        return subject_html('Show 第三季' if '/2/' in url else 'Movie','/2/' in url)
    def match(self,details):
        return {'tmdb_id':1 if details['media_type']=='movie' else 2,'media_type':details['media_type'],'season':details['season']}
    def request(self,match,preview):
        if not preview:self.requests.append(match)
        return ('preview' if preview else 'submitted'),'ok'
    def close(self):
        pass


def run_service(service,preview=False):
    service.start(preview)
    service.thread.join(3)
    assert not service.running


def test_preview_real_run_restart_and_media_switches(tmp_path):
    FakeClients.requests=[];FakeClients.cutoffs=[]
    store=Store(tmp_path);store.save_config(configuration(tv=False))
    service=SyncService(store,FakeClients)
    run_service(service,True)
    assert not FakeClients.requests
    assert FakeClients.cutoffs[-1]==TODAY-timedelta(days=29)
    assert {x['state'] for x in store.rows('SELECT * FROM items')}=={'preview','disabled'}
    run_service(service)
    assert len(FakeClients.requests)==1
    # Restart and enable TV: successful movies remain deduplicated, disabled TV is reconsidered.
    store=Store(tmp_path);store.save_config(configuration(tv=True))
    service=SyncService(store,FakeClients)
    run_service(service)
    assert len(FakeClients.requests)==2
    assert FakeClients.requests[1]['season']==3
    run_service(service)
    assert len(FakeClients.requests)==2


def test_failed_items_retry_and_changed_destination(tmp_path):
    class Flaky(FakeClients):
        should_fail=True
        def request(self,match,preview):
            if self.should_fail:raise SyncError('temporary failure')
            return super().request(match,preview)
    FakeClients.requests=[]
    store=Store(tmp_path);store.save_config(configuration())
    service=SyncService(store,Flaky);run_service(service)
    assert store.rows('SELECT state FROM runs')[0]['state']=='partial'
    Flaky.should_fail=False;run_service(service)
    assert len(FakeClients.requests)==2
    store.save_config(configuration(seerr_url='http://new-server:5055'))
    run_service(service)
    assert len(FakeClients.requests)==4


def test_worker_guard_and_scheduler(tmp_path):
    store=Store(tmp_path);store.save_config(configuration(enabled=True))
    service=SyncService(store,FakeClients)
    assert service.next_run
    service.lock.acquire()
    with pytest.raises(SyncError):service.start()
    service.lock.release()
    service.next_run=datetime.now(service.next_run.tzinfo)-timedelta(seconds=1)
    service.tick();service.thread.join(3)
    assert store.rows('SELECT state FROM runs')[0]['state']=='complete'
    assert service.next_run>datetime.now(service.next_run.tzinfo)


@pytest.fixture
def web(tmp_path):
    app=create_app(tmp_path,'test-password-long',scheduler=False)
    with TestClient(app) as client:
        client.auth=('admin','test-password-long')
        client.headers['X-Requested-With']='douban-reel'
        yield client


def test_auth_csrf_and_no_secret_echo(web):
    assert web.get('/api/config',auth=('admin','wrong')).status_code==401
    assert web.get('/healthz',auth=None).status_code==200
    assert web.put('/api/config',json={},headers={'X-Requested-With':''}).status_code==403
    assert web.put('/api/config',json={},headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    result=web.put('/api/config',json={'seerr_api_key':'secret','history_days':14})
    assert result.status_code==200
    assert '"secret"' not in result.text
    assert result.json()['history_days']==14
    web.put('/api/config',json={'seerr_api_key':''})
    assert web.app.state.store.config().seerr_api_key=='secret'  # pragma: allowlist secret (synthetic test credential)
    result=web.put('/api/config',json={'seerr_api_key':'sensitive\r\nvalue'})
    assert result.status_code==422
    assert 'sensitive' not in result.text
    assert web.put('/api/config',json={'enabled':True}).status_code==400


def test_cookie_validate_before_save_and_delete(web,monkeypatch):
    def failure(self):raise DoubanError('invalid cookie')
    monkeypatch.setattr(Clients,'verify_douban',failure)
    response=web.post('/api/cookie',json={'cookie':'dbcl2=x; ck=y','douban_user':'tester'})
    assert response.status_code==400
    assert not web.app.state.store.config().douban_cookie
    monkeypatch.setattr(Clients,'verify_douban',lambda self:'登录有效')
    response=web.post('/api/cookie',json={'cookie':'dbcl2=x; ck=y','douban_user':'tester'})
    assert response.status_code==200
    assert 'dbcl2' not in response.text
    assert web.app.state.store.config().douban_cookie=='dbcl2=x; ck=y'
    # General settings cannot bypass cookie validation.
    web.put('/api/config',json={'douban_cookie':'dbcl2=evil; ck=y'})
    assert web.app.state.store.config().douban_cookie=='dbcl2=x; ck=y'
    assert web.delete('/api/cookie').status_code==200
    assert not web.app.state.store.config().douban_cookie


def test_mapping_and_static(web):
    store=web.app.state.store
    scope=scope_for(store.config())
    store.item(scope,{'subject':'1','title':'Unmatched'},'failed','no IMDb')
    assert web.put('/api/mappings/1',json={'media_type':'tv','tmdb_id':42,'season':3}).status_code==200
    assert web.get('/api/status').json()['items'][0]['state']=='mapped'
    assert web.get('/').status_code==200
    assert web.get('/static/app.js').status_code==200
    assert web.get('/static/secret.key').status_code==404


def test_complete_pipeline_with_real_parsers_and_http_clients(tmp_path):
    requests=[]
    stored=False
    def handler(request):
        nonlocal stored
        host,path=request.url.host,request.url.path
        if host=='www.douban.com':
            assert 'dbcl2' in request.headers['cookie']
            return httpx.Response(200,text='<div class="nav-user-account">Account</div>')
        if host=='movie.douban.com' and '/wish' in path:
            return httpx.Response(200,text=wish_html([('1','Movie',TODAY.isoformat())]))
        if host=='movie.douban.com':
            return httpx.Response(200,text=subject_html())
        assert 'cookie' not in request.headers
        assert host=='seerr'
        assert request.headers['x-api-key']=='secret-seerr'
        if path=='/api/v1/search':
            assert request.url.params['query']=='imdb:tt1234567'
            return httpx.Response(200,json={'results':[{'id':42,'mediaType':'movie'}]})
        if request.method=='POST':
            requests.append(json.loads(request.content))
            stored=True
            # Simulates server acceptance followed by a lost response.
            raise httpx.ReadTimeout('upstream secret',request=request)
        return httpx.Response(200,json={'id':42,'externalIds':{'imdbId':'tt1234567'},'mediaInfo':{'status':2 if stored else 1}})
    def factory(config,stop):
        return Clients(config,NoWait(),httpx.MockTransport(handler))
    store=Store(tmp_path);store.save_config(configuration())
    service=SyncService(store,factory)
    run_service(service,True)
    assert not requests
    run_service(service)
    assert len(requests)==1
    assert store.rows('SELECT state FROM items')[0]['state']=='failed'
    run_service(service)
    assert len(requests)==1
    assert store.rows('SELECT state FROM items')[0]['state']=='existing'


def test_seerr_matching_validates_type_and_imdb():
    external={'id':'tt1234567'}
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.host=='seerr'
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        if request.url.path.endswith('/search'):
            assert request.url.params['query']=='imdb:tt1234567'
            return httpx.Response(200,json={'results':[{'id':42,'mediaType':'movie'},{'id':1,'mediaType':'person'}]})
        return httpx.Response(200,json={'externalIds':{'imdbId':external['id']}})
    client=client_with(handler)
    details={'media_type':'movie','imdb':'tt1234567','season':None}
    assert client.match(details)=={'media_type':'movie','tmdb_id':42,'season':None}
    external['id']='tt7654321'
    with pytest.raises(SyncError,match='IMDb ID 不一致'):
        client.match(details)
    with pytest.raises(SyncError,match='唯一匹配'):
        client.match({**details,'media_type':'tv'})
    client.close()


def test_no_tmdb_token_needed_and_old_settings_migrate(tmp_path):
    store=Store(tmp_path)
    store.save_config(configuration())
    raw=json.loads(store.rows('SELECT value FROM settings')[0]['value'])
    raw['tmdb_token']='obsolete-encrypted-token'
    store.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(raw),))
    assert store.config().ready()
    assert 'tmdb_token_saved' not in store.public_config()
    store.update_config(lambda current: current.model_copy(update={'history_days':7}))
    assert 'tmdb_token' not in json.loads(store.rows('SELECT value FROM settings')[0]['value'])


def test_running_settings_and_mapping_use_snapshot_until_next_task(web):
    started,release=threading.Event(),threading.Event()
    captured=[]
    class Blocked(FakeClients):
        def __init__(self, config, stop):
            super().__init__(config,stop)
            captured.append(config)
        def verify_douban(self):
            started.set()
            assert release.wait(5)
            return 'ok'
    store,service=web.app.state.store,web.app.state.sync
    original=configuration(enabled=True)
    store.save_config(original)
    scope=scope_for(original)
    store.item(scope,{'subject':'1','title':'Movie'},'failed','no match')
    service.clients_factory=Blocked
    service.start(preview=True)
    try:
        assert started.wait(2)
        response=web.put('/api/config',json={'history_days':7,'movies':False,'request_delay':15,'interval_minutes':60,'enabled':False})
        assert response.status_code==200
        assert web.get('/api/status').json()['settings_pending']
        assert web.get('/api/status').json()['next_run'] is None
        assert web.put('/api/mappings/1',json={'media_type':'movie','tmdb_id':99}).status_code==200
        assert captured[0]==original
    finally:
        release.set();service.thread.join(3)
    assert not service.running
    # The active task kept its original media switches and mapping.
    row=store.rows('SELECT * FROM items WHERE subject=? AND scope=?',('1',scope))[0]
    assert row['tmdb_id']==1
    assert row['state']=='preview'
    run_service(service,True)
    assert captured[1].history_days==7
    assert not captured[1].movies
    assert service.next_run is None
    assert store.rows('SELECT state FROM items WHERE subject=? AND scope=?',('1',scope))[0]['state']=='disabled'
    # Enabling the movie switch later uses the saved manual mapping.
    web.put('/api/config',json={'movies':True})
    run_service(service,True)
    assert store.rows('SELECT tmdb_id FROM items WHERE subject=? AND scope=?',('1',scope))[0]['tmdb_id']==99


def test_cookie_validation_does_not_block_sync_or_overwrite_new_settings(web,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    validation_started,validation_release=threading.Event(),threading.Event()
    worker_started,worker_release=threading.Event(),threading.Event()
    class Blocked(FakeClients):
        def verify_douban(self):
            worker_started.set()
            assert worker_release.wait(5)
            return 'ok'
    def verify(self):
        validation_started.set()
        assert validation_release.wait(5)
        return '登录有效'
    store,service=web.app.state.store,web.app.state.sync
    store.save_config(configuration())
    service.clients_factory=Blocked
    service.start(True)
    assert worker_started.wait(2)
    monkeypatch.setattr(Clients,'verify_douban',verify)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(web.post,'/api/cookie',json={'cookie':'dbcl2=new; ck=y','douban_user':'tester'})
        try:
            assert validation_started.wait(2)
            assert web.put('/api/config',json={'history_days':77,'request_delay':15}).status_code==200
            assert service.running
            validation_release.set()
            assert future.result(timeout=3).status_code==200
            assert store.config().history_days==77
            assert store.config().request_delay==15
            assert store.config().douban_cookie=='dbcl2=new; ck=y'
            assert service.active_config.douban_cookie==configuration().douban_cookie
        finally:
            validation_release.set();worker_release.set();service.thread.join(3)
    assert not service.running


def test_cookie_can_be_deleted_while_running(web):
    started,release=threading.Event(),threading.Event()
    class Blocked(FakeClients):
        def verify_douban(self):
            started.set()
            assert release.wait(5)
            return 'ok'
    store,service=web.app.state.store,web.app.state.sync
    store.save_config(configuration(enabled=True))
    service.clients_factory=Blocked
    service.start(True)
    try:
        assert started.wait(2)
        assert web.delete('/api/cookie').status_code==200
        assert service.running
        assert service.active_config.douban_cookie
        assert not store.config().douban_cookie
        assert not store.config().enabled
    finally:
        release.set();service.thread.join(3)
    assert service.next_run is None
    with pytest.raises(SyncError):service.start()


def test_parallel_partial_config_updates_are_merged(web):
    from concurrent.futures import ThreadPoolExecutor
    barrier=threading.Barrier(2)
    def update(data):
        barrier.wait(timeout=3)
        return web.put('/api/config',json=data)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a=executor.submit(update,{'history_days':37})
        b=executor.submit(update,{'interval_minutes':90})
        assert a.result(timeout=3).status_code==200
        assert b.result(timeout=3).status_code==200
    config=web.app.state.store.config()
    assert config.history_days==37
    assert config.interval_minutes==90


def test_cookie_verification_and_sync_share_rate_limit(monkeypatch):
    clock={'time':100.0}
    waits=[]
    class ClockStop(NoWait):
        def wait(self, seconds):
            waits.append(seconds)
            clock['time']+=seconds
            return False
    monkeypatch.setattr('app.clients.time.monotonic',lambda:clock['time'])
    samples=iter([7.5,12.0])
    def sample(low,high):
        assert (low,high)==(2,15)
        return next(samples)
    monkeypatch.setattr('app.clients.random.uniform',sample)
    gate=RequestGate()
    transport=httpx.MockTransport(lambda request:httpx.Response(200,text='<html></html>'))
    sync_client=Clients(configuration(),ClockStop(),transport,gate)
    verification_client=Clients(configuration(),ClockStop(),transport,gate)
    sync_client.douban('https://movie.douban.com/subject/1/')
    verification_client.douban('https://www.douban.com/mine/')
    clock['time']+=3
    sync_client.douban('https://movie.douban.com/subject/2/')
    assert waits==[0,7.5,9]
    sync_client.close();verification_client.close()


def test_random_request_interval_uses_custom_limit_and_stops(monkeypatch):
    monkeypatch.setattr('app.clients.time.monotonic',lambda:100.0)
    def sample(low,high):
        assert (low,high)==(2,30)
        return high
    monkeypatch.setattr('app.clients.random.uniform',sample)
    gate=RequestGate()
    assert gate.wait(30,NoWait())
    class Stopped:
        def wait(self,seconds):
            assert seconds==30
            return True
    assert not gate.wait(30,Stopped())
    assert gate.last_request==100.0
