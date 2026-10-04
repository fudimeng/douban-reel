from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .clients import SyncError


def send_bark(url, test=False, transport=None):
    if not url:
        raise SyncError('请先保存 Bark 推送地址')
    title = '豆瓣映单通知测试' if test else '豆瓣映单 Cookie 已失效'
    body = 'Bark 推送配置正常。' if test else '豆瓣登录已失效或需要安全验证，同步任务已停止。请在豆瓣映单中重新验证并保存 Cookie。'
    endpoint = urlsplit(url)
    path = endpoint.path.rstrip('/') + '/' + quote(title, safe='') + '/' + quote(body, safe='')
    query = endpoint.query + ('&' if endpoint.query else '') + 'group=' + quote('豆瓣映单', safe='')
    try:
        with httpx.Client(timeout=10, follow_redirects=False, transport=transport) as client:
            response = client.get(urlunsplit((endpoint.scheme, endpoint.netloc, path, query, '')))
            if not response.is_success:
                raise SyncError(f'Bark 推送失败（HTTP {response.status_code}）')
            data = response.json()
            if data.get('code') != 200:
                raise SyncError('Bark 未确认推送成功，请检查地址与设备密钥')
    except (httpx.HTTPError, ValueError, AttributeError):
        # Exceptions may contain the device key, endpoint or response body.
        raise SyncError('Bark 推送连接失败或响应异常，请检查配置与网络') from None
