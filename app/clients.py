import json
import re
import threading
import time
from datetime import date
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


class SyncError(Exception):
    pass


class DoubanError(SyncError):
    pass


def soup_checked(html):
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('form[action*="login"], #captcha_image, input[name="captcha-solution"], script[src*="sec.douban.com"]') or any(
        marker in soup.get_text() for marker in ('异常请求', '有异常请求', '检测到有异常', '访问豆瓣的方式有点像机器人', '访问过于频繁')):
        raise DoubanError('豆瓣要求登录或安全验证。请在浏览器完成验证并更新 Cookie，任务已停止')
    return soup


def parse_wish(html):
    soup = soup_checked(html)
    entries = []
    for node in soup.select('.grid-view .item'):
        link = node.select_one('.title a[href*="/subject/"]')
        if not link:
            raise DoubanError('豆瓣条目结构变化，无法读取条目链接')
        subject = re.search(r'/subject/(\d+)', link['href'])
        if not subject:
            raise DoubanError('豆瓣条目 ID 格式不正确')
        stamp = node.select_one('.date')
        marked = re.search(r'\d{4}-\d{2}-\d{2}', stamp.get_text()) if stamp else None
        try:
            day = date.fromisoformat(marked[0]).isoformat() if marked else None
        except ValueError:
            day = None
        entries.append({'subject': subject[1], 'title': link.get_text(' ', strip=True), 'marked': day})
    if not entries and not soup.select_one('.grid-view'):
        raise DoubanError('未找到想看列表；请检查用户 ID、Cookie 或豆瓣验证页面')
    next_link = soup.select_one('.paginator .next a')
    return entries, next_link.get('href') if next_link else None


def chinese_number(text):
    if text.isdigit():
        return int(text)
    digits = dict(zip('零一二三四五六七八九', range(10)))
    if text == '两':
        return 2
    if '十' in text:
        left, right = text.split('十', 1)
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(text)


def parse_subject(html):
    soup = soup_checked(html)
    info = soup.select_one('#info')
    heading = soup.select_one('h1')
    if not info or not heading:
        raise DoubanError('豆瓣详情页面结构异常或不可访问')
    text = info.get_text(' ', strip=True)
    imdb = re.search(r'\btt\d{5,12}\b', text)
    if not imdb:
        link = info.select_one('a[href*="imdb.com/title/tt"]')
        imdb = re.search(r'tt\d{5,12}', link['href']) if link else None
    kind = 'tv' if re.search(r'集数\s*[:：]|首播\s*[:：]|单集片长', text) else 'movie'
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            if json.loads(script.string or '{}').get('@type') == 'TVSeries':
                kind = 'tv'
        except (ValueError, AttributeError):
            pass
    title = heading.get_text(' ', strip=True)
    season_text = re.search(r'第\s*([零一二三四五六七八九十两\d]+)\s*季', title)
    english_season = re.search(r'\bSeason\s+(\d+)\b', title, re.I)
    season = chinese_number(season_text[1]) if season_text else int(english_season[1]) if english_season else None
    return {'imdb': imdb[0] if imdb else None, 'media_type': kind, 'season': season}


class Clients:
    def __init__(self, config, stop=None, transport=None):
        self.config = config
        self.stop = stop or threading.Event()
        self.http = httpx.Client(timeout=30, follow_redirects=False, transport=transport)
        self.last_douban = 0.0

    def close(self):
        self.http.close()

    def douban(self, url, redirects=0):
        if urlsplit(url).hostname not in ('movie.douban.com', 'www.douban.com') or urlsplit(url).scheme != 'https':
            raise DoubanError('豆瓣分页地址无效，已停止')
        delay = max(0, self.config.request_delay - (time.monotonic() - self.last_douban))
        if self.stop.wait(delay):
            raise SyncError('任务已停止')
        self.last_douban = time.monotonic()
        try:
            response = self.http.get(url, headers={
                'Cookie': self.config.douban_cookie,
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
                'Referer': 'https://movie.douban.com/',
            })
        except httpx.HTTPError:
            raise DoubanError('无法连接豆瓣，请检查网络；本轮已停止') from None
        if response.is_redirect and redirects < 4:
            destination = urljoin(url, response.headers.get('location', ''))
            if urlsplit(destination).hostname in ('movie.douban.com', 'www.douban.com') and '/login' not in destination:
                return self.douban(destination, redirects + 1)
        if response.status_code != 200:
            raise DoubanError(f'豆瓣返回 HTTP {response.status_code}；请检查 Cookie 或在浏览器完成验证')
        return response.text

    def verify_douban(self):
        soup = soup_checked(self.douban('https://www.douban.com/mine/'))
        if not soup.select_one('a[href*="/accounts/logout"], a[href*="/accounts/loginout"], .nav-user-account'):
            raise DoubanError('无法确认豆瓣登录状态，Cookie 可能已失效；请重新复制完整 Cookie')
        entries, _ = parse_wish(self.douban(self.wish_url()))
        return f'豆瓣登录有效，目标想看列表可读取（首页 {len(entries)} 条）'

    def wish_url(self):
        return f'https://movie.douban.com/people/{self.config.douban_user}/wish?sort=time&start=0&mode=grid'

    def wish(self, cutoff):
        url, visited = self.wish_url(), set()
        seen = set()
        for _ in range(200):
            if url in visited:
                raise DoubanError('豆瓣分页出现循环，任务停止，请稍后重试')
            visited.add(url)
            entries, next_page = parse_wish(self.douban(url))
            for entry in entries:
                if entry['subject'] not in seen:
                    seen.add(entry['subject'])
                    if not entry['marked'] or date.fromisoformat(entry['marked']) >= cutoff:
                        yield entry
            if not next_page or (entries and all(e['marked'] and date.fromisoformat(e['marked']) < cutoff for e in entries)):
                return
            url = urljoin(url, next_page)
            if urlsplit(url).hostname != 'movie.douban.com' or not urlsplit(url).path.startswith(f'/people/{self.config.douban_user}/wish'):
                raise DoubanError('想看列表分页地址不正确')
        raise DoubanError('达到 200 页读取上限；请缩小历史天数后重试')

    def api(self, service, method, path, **kwargs):
        if self.stop.is_set():
            raise SyncError('任务已停止')
        if service == 'Seerr':
            url = self.config.seerr_url + '/api/v1' + path
            headers = {'X-Api-Key': self.config.seerr_api_key}
        else:
            url = 'https://api.themoviedb.org/3' + path
            headers = {'Authorization': 'Bearer ' + self.config.tmdb_token}
        try:
            response = self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise SyncError(f'{service} 连接失败或超时；请检查网络，下一轮会先核对再重试') from None
        if not response.is_success:
            raise SyncError(f'{service} 返回 HTTP {response.status_code}；请检查连接、密钥、权限或请求配额')
        try:
            return response.json()
        except ValueError:
            raise SyncError(f'{service} 未返回 JSON，请检查服务地址') from None

    def match(self, details):
        if not details['imdb']:
            raise SyncError('豆瓣条目没有 IMDb ID，请手动指定 TMDB 映射')
        data = self.api('TMDB', 'GET', '/find/' + details['imdb'], params={'external_source': 'imdb_id', 'language': 'zh-CN'})
        kind = details['media_type']
        results = data.get('movie_results' if kind == 'movie' else 'tv_results', [])
        if len(results) != 1:
            raise SyncError('IMDb 未能唯一匹配对应类型的 TMDB 条目，请手动指定映射')
        return {'media_type': kind, 'tmdb_id': results[0]['id'], 'season': details['season'] if kind == 'tv' else None}

    def request(self, match, preview=False):
        kind, media_id = match['media_type'], match['tmdb_id']
        details = self.api('Seerr', 'GET', f'/{kind}/{media_id}')
        info = details.get('mediaInfo') or {}
        payload = {'mediaType': kind, 'mediaId': media_id, 'is4k': False}
        if kind == 'movie':
            pending = any(not r.get('is4k') and r.get('status') in (1, 2) for r in info.get('requests', []))
            if info.get('status') in (2, 3, 4, 5) or pending:
                return 'existing', 'Seerr 中已有请求或媒体，跳过'
        else:
            available_seasons = {s['seasonNumber'] for s in details.get('seasons', []) if s.get('seasonNumber', 0) > 0}
            wanted = {match['season']} if match.get('season') else available_seasons if self.config.tv_seasons == 'all' else {1}
            if not wanted or not wanted.issubset(available_seasons):
                raise SyncError('目标季不存在于 Seerr/TMDB，请核对季号和映射')
            existing = {s['seasonNumber'] for s in info.get('seasons', []) if s.get('status') in (2, 3, 4, 5)}
            for req in info.get('requests', []):
                if not req.get('is4k') and req.get('status') in (1, 2):
                    existing.update(s['seasonNumber'] for s in req.get('seasons', []))
            remaining = sorted(wanted - existing)
            if not remaining:
                return 'existing', '目标季已有请求或媒体，跳过'
            payload['seasons'] = remaining
        if preview:
            return 'preview', '预览通过，尚未提交' + (f"；季号 {payload['seasons']}" if kind == 'tv' else '')
        result = self.api('Seerr', 'POST', '/request', json=payload)
        return 'submitted', f"已提交 Seerr 请求 #{result.get('id', '—')}" + (f"；季号 {payload['seasons']}" if kind == 'tv' else '')
