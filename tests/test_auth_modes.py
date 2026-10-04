import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Config


def test_no_password_mode_allows_ui_and_api_but_keeps_source_checks(tmp_path):
    app=create_app(tmp_path,admin_password='',auth_enabled=False,scheduler=False)
    with TestClient(app) as web:
        for path in ('/','/static/app.js','/static/style.css','/api/config','/api/status'):
            response=web.get(path)
            assert response.status_code==200
            assert 'www-authenticate' not in response.headers
        # A stale or malformed Authorization header must not enable authentication.
        assert web.get('/api/config',headers={'Authorization':'Basic invalid'}).status_code==200
        assert web.put('/api/config',json={'history_days':14}).status_code==403
        headers={'X-Requested-With':'douban-reel'}
        response=web.put('/api/config',json={'history_days':14},headers=headers)
        assert response.status_code==200
        assert response.json()['history_days']==14
        assert web.put('/api/config',json={'history_days':7},headers={**headers,'Sec-Fetch-Site':'cross-site'}).status_code==403
        app.state.store.save_config(Config(douban_cookie='dbcl2="123:fake"; ck=test',seerr_api_key='fake-key',bark_url='https://bark.test/device'))  # pragma: allowlist secret (synthetic test credentials)
        public=web.get('/api/config').json()
        assert public['douban_cookie_saved'] and public['seerr_api_key_saved'] and public['bark_url_saved']
        assert not {'douban_cookie','seerr_api_key','bark_url'} & public.keys()


def test_authentication_stays_enabled_by_default(tmp_path,monkeypatch):
    monkeypatch.delenv('AUTH_ENABLED',raising=False)
    app=create_app(tmp_path,admin_password='authentication-test-password',scheduler=False)
    with TestClient(app) as web:
        for path in ('/','/static/app.js','/api/config','/api/status'):
            assert web.get(path).status_code==401
        web.auth=('admin','authentication-test-password')
        assert web.get('/api/config').status_code==200


def test_environment_disables_password_requirement(tmp_path,monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED','false')
    monkeypatch.delenv('ADMIN_PASSWORD',raising=False)
    with TestClient(create_app(tmp_path,scheduler=False)) as web:
        assert web.get('/').status_code==200


def test_enabled_authentication_requires_strong_password(tmp_path):
    with pytest.raises(RuntimeError,match='ADMIN_PASSWORD'):
        with TestClient(create_app(tmp_path,admin_password='',auth_enabled=True,scheduler=False)):
            pass


@pytest.mark.parametrize('value',['','flase','0'])
def test_invalid_auth_mode_does_not_disable_authentication(tmp_path,monkeypatch,value):
    monkeypatch.setenv('AUTH_ENABLED',value)
    with pytest.raises(RuntimeError,match='AUTH_ENABLED'):
        create_app(tmp_path,scheduler=False)
