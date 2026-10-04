import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


SECRETS = ('douban_cookie', 'seerr_api_key', 'bark_url')


class Config(BaseModel):
    douban_user: str = ''
    douban_cookie: str = ''
    seerr_url: str = ''
    seerr_api_key: str = ''
    bark_url: str = ''
    enabled: bool = False
    movies: bool = True
    tv: bool = True
    history_days: int = Field(default=30, ge=1, le=36500)
    interval_minutes: int = Field(default=1440, ge=15, le=10080)
    request_delay: float = Field(default=15, ge=2, le=60)
    tv_seasons: Literal['first', 'all'] = 'first'

    @model_validator(mode='after')
    def resolve_cookie_account(self):
        if not self.douban_user and self.douban_cookie:
            parts = dict(part.strip().split('=', 1) for part in self.douban_cookie.split(';') if part.strip())
            account = re.fullmatch(r'([0-9]{1,100}):.+', parts.get('dbcl2', '').strip().strip('"'))
            if account:
                self.douban_user = account[1]
        return self

    @field_validator('douban_user')
    @classmethod
    def user_id(cls, value):
        value = value.strip()
        if value.startswith('https://'):
            match = re.fullmatch(r'https://(?:www|movie)\.douban\.com/people/([\w-]+)/?', value)
            if not match:
                raise ValueError('请输入豆瓣个人主页地址或用户 ID')
            value = match[1]
        if value and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
            raise ValueError('豆瓣 ID 只能包含字母、数字、下划线或短横线')
        return value

    @field_validator('seerr_url')
    @classmethod
    def server_url(cls, value):
        value = value.strip().rstrip('/')
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('请输入 http(s) Seerr 地址，不含用户名、密码或查询参数')
            if value.endswith('/api/v1'):
                value = value[:-7]
        return value

    @field_validator('douban_cookie')
    @classmethod
    def cookie(cls, value):
        value = value.strip()
        if value.startswith('['):
            try:
                cookies = json.loads(value)
                value = '; '.join(f"{c['name']}={c['value']}" for c in cookies
                                  if c.get('domain', '.douban.com').lstrip('.') in ('douban.com', 'movie.douban.com', 'www.douban.com'))
            except (ValueError, TypeError, KeyError):
                raise ValueError('Cookie JSON 格式错误') from None
        value = re.sub(r'^cookie:\s*', '', value, flags=re.I)
        if '\n' in value or '\r' in value or len(value) > 32768:
            raise ValueError('Cookie 需为单行请求头，或浏览器导出的 JSON 数组')
        if value and any('=' not in part for part in value.split(';') if part.strip()):
            raise ValueError('Cookie 格式应为 name=value; name2=value2')
        if value:
            parts = dict(part.strip().split('=', 1) for part in value.split(';') if part.strip())
            if not parts.get('dbcl2') or not parts.get('ck'):
                raise ValueError('Cookie 至少需要 dbcl2 和 ck，请复制完整请求头')
            if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('Cookie 包含非法字符')
        return value

    @field_validator('seerr_api_key')
    @classmethod
    def token(cls, value):
        value = value.strip()
        if any(c.isspace() for c in value) or len(value) > 4096:
            raise ValueError('密钥不可包含空格或换行')
        return value

    @field_validator('bark_url')
    @classmethod
    def notification_url(cls, value):
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment
                    or not parsed.path.strip('/') or len(value) > 4096
                    or any(c.isspace() for c in value)):
                raise ValueError('请输入包含设备密钥的完整 HTTP(S) Bark 推送地址')
            try:
                parsed.port
            except ValueError:
                raise ValueError('Bark 推送地址端口无效') from None
        return value

    def ready(self):
        return all((self.douban_user, self.douban_cookie, self.seerr_url,
                    self.seerr_api_key)) and (self.movies or self.tv)


class Mapping(BaseModel):
    media_type: Literal['movie', 'tv']
    tmdb_id: int = Field(gt=0)
    season: int | None = Field(default=None, ge=1, le=999)
