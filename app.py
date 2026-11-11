from __future__ import annotations
import functools
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from store import Store
from xclient import NotLoggedIn, XClient, XError
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = {'role': 'source', 'port': 8730, 'token': '', 'proxy': '', 'data_dir': './data', 'cookies': '', 'accounts': [], 'poll_seconds': 3600, 'home_timeline': True, 'fetch_count': 40, 'max_tweets': 50000, 'cache_max_mb': 1024, 'cache_gc_seconds': 300, 'thumb': '360x360', 'ops_path': os.path.join(HERE, 'ops.json')}
MEDIA_HOSTS = ('pbs.twimg.com', 'video.twimg.com', 'abs.twimg.com')
MAX_MEDIA = 120 * 1024 * 1024
MAX_BODY = 1024 * 1024
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'

class BadRequest(Exception):
    pass

def log(msg: str):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)

def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg['_path'] = path
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            cfg.update(json.load(fh))
    env_cookies = os.environ.get('XV_COOKIES')
    if env_cookies:
        cfg['cookies'] = env_cookies
    for env_key, cfg_key, convert in (('XV_TOKEN', 'token', str), ('XV_PORT', 'port', int), ('XV_DATA_DIR', 'data_dir', str), ('XV_CACHE_MAX_MB', 'cache_max_mb', int), ('XV_CACHE_GC_SECONDS', 'cache_gc_seconds', int), ('XV_POLL_SECONDS', 'poll_seconds', int), ('XV_HOME_TIMELINE', 'home_timeline', lambda raw: raw.lower() not in ('0', 'false', 'no', 'off'))):
        raw = os.environ.get(env_key)
        if raw:
            cfg[cfg_key] = convert(raw)
    for key in ('data_dir', 'ops_path'):
        if not os.path.isabs(cfg[key]):
            cfg[key] = os.path.normpath(os.path.join(os.path.dirname(path), cfg[key]))
    return cfg

def parse_cursor(raw: str) -> tuple[int | None, str | None]:
    raw = (raw or '').strip()
    if not raw:
        return (None, None)
    head, sep, tail = raw.partition('_')
    if not sep or not tail:
        raise BadRequest('before 参数应为 <ts>_<id>')
    try:
        ts = int(head)
    except ValueError as exc:
        raise BadRequest('before 参数应为 <ts>_<id>') from exc
    return (ts, tail)

def pbs_variant(src: str, variant: str, thumb: str='360x360') -> str:
    if variant in ('', 'raw', 'video'):
        return src
    parsed = urllib.parse.urlparse(src)
    if parsed.hostname != 'pbs.twimg.com':
        return src
    if '/profile_images/' in parsed.path:
        size = '200x200' if variant == 'thumb' else '400x400'
        path = re.sub('_(normal|bigger|mini|reasonably_small)(\\.\\w+)$', f'_{size}\\2', parsed.path)
        return urllib.parse.urlunparse(parsed._replace(path=path))
    if variant in ('thumb', 'full') and ('/media/' in parsed.path or '/card_img/' in parsed.path):
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if variant == 'thumb':
            query['name'] = thumb
            query['format'] = 'webp'
        else:
            query['name'] = 'orig'
            query.pop('format', None)
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    return src

def _opener(proxy: str):
    handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy else urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)

class _Redirect(Exception):

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url

class _NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirect(urllib.parse.urljoin(req.full_url, newurl))

def _redirect_from_html(body: str) -> str | None:
    match = re.search('URL=([^"\\\'>\\s]+)', body, re.I)
    if not match:
        match = re.search('location\\.replace\\(["\\\']([^"\\\']+)["\\\']\\)', body, re.I)
    if not match:
        return None
    return html.unescape(match.group(1).replace('\\/', '/'))

@functools.lru_cache(maxsize=512)
def resolve_short(url: str, proxy: str='') -> str:
    current = url
    headers = {'User-Agent': UA, 'Referer': 'https://x.com/'}
    for _ in range(3):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('t.co', 'www.t.co'):
            return current
        handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy else urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler, _NoRedirect())
        req = urllib.request.Request(current, headers=headers)
        try:
            with opener.open(req, timeout=10) as resp:
                final = resp.geturl() or current
                if urllib.parse.urlparse(final).hostname in ('t.co', 'www.t.co'):
                    body = resp.read(16384).decode('utf-8', 'replace')
                    target = _redirect_from_html(body)
                    if target:
                        target_parsed = urllib.parse.urlparse(target)
                        if target_parsed.scheme in ('http', 'https') and target_parsed.hostname:
                            return target
                    raise ValueError('t.co 没有返回跳转地址')
                return final
        except _Redirect as redir:
            target = redir.url
            target_parsed = urllib.parse.urlparse(target)
            if target_parsed.scheme not in ('http', 'https'):
                raise ValueError(f'短链目标不是 http(s): {target}') from redir
            if target_parsed.hostname in ('t.co', 'www.t.co'):
                current = target
                continue
            return target
        except urllib.error.HTTPError as exc:
            location = exc.headers.get('Location')
            if exc.code in (301, 302, 303, 307, 308) and location:
                target = urllib.parse.urljoin(current, location)
                if urllib.parse.urlparse(target).hostname in ('t.co', 'www.t.co'):
                    current = target
                    continue
                return target
            raise
    raise ValueError('t.co 重定向次数过多')

def _article_blocks(state: dict, media_by_id: dict) -> list[dict]:
    blocks = (state or {}).get('blocks') or []
    raw_entities = (state or {}).get('entityMap') or []
    entity_map: dict[str, dict] = {}
    if isinstance(raw_entities, dict):
        entity_map = {str(k): v or {} for k, v in raw_entities.items()}
    else:
        for entry in raw_entities:
            if isinstance(entry, dict) and entry.get('key') is not None:
                entity_map[str(entry['key'])] = entry.get('value') or {}
    out: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get('type') or 'unstyled'
        text = (block.get('text') or '').strip()
        if kind == 'atomic':
            for rng in block.get('entityRanges') or []:
                entity = entity_map.get(str(rng.get('key'))) or {}
                if entity.get('type') != 'MEDIA':
                    continue
                data = entity.get('data') or {}
                caption = data.get('caption') or ''
                for item in data.get('mediaItems') or []:
                    info = media_by_id.get(str(item.get('mediaId')))
                    if info:
                        entry = {'type': 'image', 'url': info['url'], 'alt': caption, 'w': info.get('w'), 'h': info.get('h')}
                        if info.get('video'):
                            entry['video'] = info['video']
                        out.append(entry)
            continue
        if kind.startswith('header'):
            level = 3 if kind.endswith('three') else 2
            if text:
                out.append({'type': 'heading', 'level': level, 'text': text})
        elif kind == 'blockquote':
            if text:
                out.append({'type': 'quote', 'text': text})
        elif kind in ('unordered-list-item', 'ordered-list-item'):
            if text:
                out.append({'type': 'list', 'text': text, 'ordered': kind.startswith('ordered')})
        elif text:
            out.append({'type': 'paragraph', 'text': text})
    return out

def normalize_article(data: dict, article_id: str) -> dict:
    article = data.get('article') or {}
    title = article.get('title') or ''
    plain = article.get('plain_text') or ''
    media_by_id: dict[str, dict] = {}
    media: list[str] = []
    for item in article.get('media_entities') or []:
        if not isinstance(item, dict):
            continue
        info = item.get('media_info') or {}
        url = info.get('original_img_url') or (info.get('preview_image') or {}).get('original_img_url')
        if not url:
            continue
        media.append(url)
        mid = str(item.get('media_id') or '')
        if mid:
            variants = info.get('variants') or []
            mp4 = [v for v in variants if v.get('content_type') == 'video/mp4']
            video = max(mp4, key=lambda v: v.get('bit_rate') or 0).get('url') or '' if mp4 else ''
            media_by_id[mid] = {'url': url, 'w': info.get('original_img_width'), 'h': info.get('original_img_height'), 'video': video}
    cover = ((article.get('cover_media') or {}).get('media_info') or {}).get('original_img_url') or ''
    content = article.get('content_state') or article.get('content') or {}
    blocks = _article_blocks(content, media_by_id) if isinstance(content, dict) else []
    return {'id': article_id, 'tweet_id': data.get('tweet_id', ''), 'url': f'https://x.com/i/article/{article_id}', 'title': title.strip() if isinstance(title, str) else '', 'plain_text': plain if isinstance(plain, str) else '', 'cover': cover if isinstance(cover, str) else '', 'media': media, 'blocks': blocks}

def fetch_json(url: str, headers: dict | None=None, proxy: str='', timeout: int=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip', **(headers or {})})
    with _opener(proxy).open(req, timeout=timeout) as resp:
        body = resp.read()
        if (resp.headers.get('Content-Encoding') or '').lower() == 'gzip':
            body = gzip.decompress(body)
        return json.loads(body)

class MediaCache:
    NEG_TTL = 6 * 3600

    def __init__(self, directory: str, max_bytes: int=1024 ** 3, gc_seconds: int=300):
        self.dir = directory
        self.max_bytes = max(0, int(max_bytes))
        self.gc_seconds = max(1, int(gc_seconds))
        self._last_gc = time.monotonic()
        self._lock = threading.RLock()

    def open_for_send(self, key: str):
        path = os.path.join(self.dir, key)
        meta = path + '.json'
        with self._lock:
            if not (os.path.exists(path) and os.path.exists(meta)):
                return None
            try:
                with open(meta, encoding='utf-8') as fh:
                    ctype = json.load(fh).get('type', 'application/octet-stream')
                fh = open(path, 'rb')
                try:
                    size = os.fstat(fh.fileno()).st_size
                    os.utime(path, None)
                except Exception:
                    fh.close()
                    return None
                return (fh, ctype, size)
            except Exception:
                return None

    def get(self, key: str):
        hit = self.open_for_send(key)
        if not hit:
            return None
        fh, ctype, _ = hit
        with fh:
            try:
                return (fh.read(), ctype)
            except OSError:
                return None

    def put(self, key: str, body: bytes, ctype: str):
        path = os.path.join(self.dir, key)
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'wb') as fh:
            fh.write(body)
        self.finish(tmp, key, ctype)

    def finish(self, tmp: str, key: str, ctype: str):
        path = os.path.join(self.dir, key)
        with self._lock:
            os.replace(tmp, path)
            meta_tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.json.tmp'
            with open(meta_tmp, 'w', encoding='utf-8') as fh:
                json.dump({'type': ctype}, fh)
            os.replace(meta_tmp, path + '.json')
        return self.maybe_gc()

    def get_neg(self, key: str):
        with self._lock:
            try:
                with open(os.path.join(self.dir, key + '.neg'), encoding='utf-8') as fh:
                    rec = json.load(fh)
                if rec.get('until', 0) > time.time():
                    return rec.get('error') or 'cached failure'
            except Exception:
                pass
            return None

    def put_neg(self, key: str, error: str):
        with self._lock:
            try:
                with open(os.path.join(self.dir, key + '.neg'), 'w', encoding='utf-8') as fh:
                    json.dump({'until': time.time() + self.NEG_TTL, 'error': error[:300]}, fh)
            except Exception:
                pass

    def maybe_gc(self):
        if time.monotonic() - self._last_gc < self.gc_seconds:
            return None
        return self.gc()

    def gc(self):
        with self._lock:
            self._last_gc = time.monotonic()
            now = time.time()
            for name in os.listdir(self.dir):
                if not name.endswith('.neg'):
                    continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding='utf-8') as fh:
                        expired = json.load(fh).get('until', 0) <= now
                    if expired:
                        os.unlink(path)
                except (OSError, ValueError):
                    pass
            entries = []
            total = 0
            for name in os.listdir(self.dir):
                if not re.fullmatch('[0-9a-f]{40}', name):
                    continue
                path = os.path.join(self.dir, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                entries.append((stat.st_mtime, stat.st_size, name))
                total += stat.st_size
            removed = 0
            freed = 0
            if self.max_bytes:
                entries.sort()
                while total > self.max_bytes and entries:
                    _, size, name = entries.pop(0)
                    for suffix in ('', '.json', '.neg'):
                        try:
                            os.unlink(os.path.join(self.dir, name + suffix))
                        except OSError:
                            pass
                    removed += 1
                    freed += size
                    total -= size
            return (removed, freed, total)

class Poller(threading.Thread):

    def __init__(self, ctx: 'App'):
        super().__init__(daemon=True, name='poller')
        self.app = ctx
        self.store = ctx.store
        self.client = ctx.client
        self.cfg = ctx.cfg
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            try:
                self.cycle()
            except Exception as exc:
                log(f'[poll] 失败: {exc}')
                self.store.meta_set('last_error', str(exc))
            time.sleep(self.cfg['poll_seconds'])

    def cycle(self):
        with self.app.refresh_lock:
            self._cycle()

    def _cycle(self):
        started = time.time()
        last_error = ''
        for account in self.store.accounts(only_enabled=True):
            handle = account['handle']
            try:
                if not account['user_id']:
                    info = self.client.user_id(handle)
                    account.update(user_id=info['id'])
                    self.store.upsert_account(handle, user_id=info['id'], name=info['name'], avatar=info['avatar'])
                tweets = self.client.user_tweets(account['user_id'], count=self.cfg['fetch_count'])
                added = self.store.upsert(tweets, f'user:{handle}')
                self.store.upsert_account(handle, last_fetch=int(time.time()), last_error='')
                log(f'[poll] @{handle} {len(tweets)} 条（新增 {added}）')
            except Exception as exc:
                last_error = f'@{handle}: {exc}'
                self.store.upsert_account(handle, last_error=str(exc))
                log(f'[poll] @{handle} 失败: {exc}')
            time.sleep(1.2)
        if self.cfg.get('home_timeline', True) and self.client and self.client.logged_in:
            try:
                tweets = self.client.home(count=self.cfg['fetch_count'])
                added = self.store.upsert(tweets, 'home:following')
                log(f'[poll] home {len(tweets)} 条（新增 {added}）')
            except Exception as exc:
                last_error = f'home: {exc}'
                log(f'[poll] home 失败: {exc}')
        self.store.meta_set('last_poll_ts', int(started))
        self.store.meta_set('last_poll', time.strftime('%m-%d %H:%M', time.localtime(started)))
        self.store.meta_set('last_error', last_error)
        last_trim = float(self.store.meta_get('last_trim', 0) or 0)
        if time.time() - last_trim > 86400:
            removed = self.store.trim(int(self.cfg.get('max_tweets', 50000)))
            self.store.meta_set('last_trim', time.time())
            if removed:
                log(f'[poll] 裁剪最旧的 {removed} 条推文')

class App:

    def __init__(self, cfg: dict):
        self.cfg = cfg
        role = cfg.get('role') or 'source'
        if role == 'local':
            raise SystemExit('role=local 已迁移到 Rust 的 xviewer-local，Python 端只保留 role=source')
        self.role = 'source'
        os.makedirs(cfg['data_dir'], exist_ok=True)
        self.cache_dir = os.path.join(cfg['data_dir'], 'cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        for name in os.listdir(self.cache_dir):
            if name.endswith('.tmp'):
                try:
                    os.unlink(os.path.join(self.cache_dir, name))
                except OSError:
                    pass
        self.store = Store(os.path.join(cfg['data_dir'], 'xviewer.sqlite3'))
        self.cache = MediaCache(self.cache_dir, max_bytes=int(cfg.get('cache_max_mb', 1024)) * 1024 * 1024, gc_seconds=int(cfg.get('cache_gc_seconds', 300)))
        removed, freed, _ = self.cache.gc()
        if removed:
            log(f'[cache] 启动 GC 删除 {removed} 个旧媒体，释放 {freed / 1024 ** 2:.1f} MiB')
        self.refresh_lock = threading.RLock()
        self.client = XClient(ops_path=cfg['ops_path'], cookies=cfg.get('cookies', ''), proxy=cfg.get('proxy', ''), log=log)
        for handle in cfg.get('accounts') or []:
            self.store.upsert_account(handle)
        self.poller = Poller(self)

    def refresh_now(self, timeout: float=5.0) -> str | None:
        if not self.refresh_lock.acquire(timeout=timeout):
            return '已有刷新在进行中'
        try:
            self.poller._cycle()
            return self.store.meta_get('last_error') or None
        except Exception as exc:
            return str(exc)
        finally:
            self.refresh_lock.release()

    def status(self) -> dict:
        return {'role': self.role, 'logged_in': bool(self.client.logged_in), 'cookies_set': bool(self.client.cookies), 'tweets': self.store.count(), 'accounts': len(self.store.accounts()), 'last_poll': self.store.meta_get('last_poll', ''), 'last_error': self.store.meta_get('last_error', '')}

    def set_cookies(self, cookies: str):
        self.client.set_cookies(cookies)
        return bool(self.client.cookies)

class Handler(BaseHTTPRequestHandler):
    server_version = 'xviewer/1.3'
    protocol_version = 'HTTP/1.1'

    @property
    def app(self) -> App:
        return self.server.app

    def _cookies(self) -> dict:
        raw = self.headers.get('Cookie') or ''
        out = {}
        for part in raw.split(';'):
            if '=' in part:
                key, value = part.split('=', 1)
                out[key.strip()] = value.strip()
        return out

    def _authorized(self, query: dict) -> bool:
        token = self.app.cfg.get('token') or ''
        if not token:
            return True
        authorization = self.headers.get('Authorization') or ''
        bearer = authorization[7:].strip() if authorization[:7].lower() == 'bearer ' else ''
        given = self.headers.get('X-XV-Token') or bearer or query.get('t', [''])[0] or self._cookies().get('xv', '')
        return given == token

    def _int_query(self, query: dict, key: str, default: int, minimum: int, maximum: int | None=None) -> int:
        raw = (query.get(key, [''])[0] or '').strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise BadRequest(f'{key} 必须是整数') from exc
        if value < minimum:
            raise BadRequest(f'{key} 不能小于 {minimum}')
        return min(value, maximum) if maximum is not None else value

    def _send(self, status: int, body: bytes, ctype: str='text/plain; charset=utf-8', extra: dict | None=None):
        headers = dict(extra or {})
        cache_control = headers.pop('Cache-Control', None)
        if len(body) > 1024 and 'gzip' in (self.headers.get('Accept-Encoding') or '') and (ctype.startswith('application/json') or ctype.startswith('text/html') or ctype.startswith('text/')):
            body = gzip.compress(body, 5)
            headers['Content-Encoding'] = 'gzip'
            headers.setdefault('Vary', 'Accept-Encoding')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache_control or 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, status: int=200):
        if status == 404:
            return self._send(404, b'')
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == '/healthz':
                return self._send(200, b'ok')
            if path in ('/login', '/login.html'):
                return self._login_page(query)
            if path == '/':
                return self._send(404, b'')
            if not self._authorized(query):
                return self._send(401, '未授权：请访问 /?t=<token> 或在 /login 输入 token'.encode())
            if path == '/api/status':
                return self._json(self.app.status())
            if path == '/api/feed':
                return self._feed(query)
            if path == '/api/search':
                return self._search(query)
            if path == '/api/accounts':
                return self._json({'items': self.app.store.accounts()})
            if path == '/api/user':
                return self._user(query)
            if path == '/api/resolve':
                return self._resolve(query)
            if path == '/api/article':
                return self._article(query)
            if path == '/api/pull':
                return self._pull(query)
            if path == '/img':
                return self._img(query)
            if path.startswith('/api/tweet/'):
                tw = self.app.store.get(path.rsplit('/', 1)[-1])
                return self._json({'item': tw}) if tw else self._json({'error': 'not found'}, 404)
            return self._send(404, b'')
        except BadRequest as exc:
            try:
                self._json({'error': str(exc)}, 400)
            except Exception:
                pass
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            try:
                self._json({'error': f'{type(exc).__name__}: {exc}'}, 500)
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = (parsed.path, urllib.parse.parse_qs(parsed.query))
        if not self._authorized(query):
            return self._json({'error': '未授权'}, 401)
        try:
            if path == '/api/auth':
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    return self._json({'error': 'Content-Length 不合法'}, 400)
                if length > MAX_BODY:
                    self.close_connection = True
                    return self._json({'error': '请求体过大'}, 400)
                raw = self._read_body()
                try:
                    body = json.loads(raw or b'{}')
                except (ValueError, UnicodeDecodeError):
                    return self._json({'error': 'JSON 格式错误'}, 400)
                if not isinstance(body, dict) or not isinstance(body.get('cookies'), str):
                    return self._json({'error': '需要字符串字段 cookies'}, 400)
                cookies = body['cookies'].strip()
                ok = self.app.set_cookies(cookies)
                log(f"[auth] 收到 cookie 推送（{('已设置' if ok else '已清空')}，仅内存）")
                return self._json({'ok': True, 'logged_in': ok, 'cookies_set': ok})
            if path == '/api/refresh':
                err = self.app.refresh_now()
                return self._json({'ok': err is None, 'error': err or '', 'status': self.app.status()})
            if path == '/api/user/pull':
                return self._user_pull(query)
            if path == '/api/follow':
                return self._follow(query)
            if path == '/api/retweet':
                return self._retweet(query)
            if path == '/api/foryou':
                if not self.app.client.logged_in:
                    return self._json({'error': '推荐时间线需要登录 cookie', 'items': []})
                try:
                    seen = [t['id'] for t in self.app.store.timeline(limit=300, tab='foryou') if t.get('id')]
                    tweets = self.app.client.home(count=self.app.cfg['fetch_count'], latest=False, seen_ids=seen)
                    self.app.store.upsert(tweets, 'home:foryou')
                    for tw in tweets:
                        tw['source'] = 'home:foryou'
                    log(f'[foryou] 抓到 {len(tweets)} 条')
                    return self._json({'items': tweets})
                except Exception as exc:
                    return self._json({'error': str(exc), 'items': []})
            if path == '/api/accounts':
                handle = (query.get('handle', [''])[0] or '').lstrip('@').lower()
                if not handle:
                    return self._json({'error': '缺少 handle'}, 400)
                try:
                    info = self.app.client.user_id(handle)
                    self.app.store.upsert_account(handle, user_id=info['id'], name=info['name'], avatar=info['avatar'])
                    threading.Thread(target=self._fetch_one, args=(handle,), daemon=True).start()
                    return self._json({'ok': True, **info})
                except XError as exc:
                    return self._json({'error': str(exc)}, 400)
            return self._json({'error': 'not found'}, 404)
        except Exception as exc:
            traceback.print_exc()
            return self._json({'error': str(exc)}, 500)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorized(query):
            return self._json({'error': '未授权'}, 401)
        if parsed.path == '/api/accounts':
            handle = (query.get('handle', [''])[0] or '').lstrip('@').lower()
            self.app.store.delete_account(handle)
            return self._json({'ok': True})
        return self._json({'error': 'not found'}, 404)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return b''
        return self.rfile.read(min(max(length, 0), MAX_BODY)) if length > 0 else b''

    def _login_page(self, query: dict):
        token = self.app.cfg.get('token') or ''
        if not token:
            return self._send(302, b'', extra={'Location': '/'})
        html = '<!DOCTYPE html><html><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1"><title>xviewer 登录</title>\n<style>body{background:#0d1117;color:#e6edf3;font:15px -apple-system,sans-serif;display:grid;place-items:center;height:100vh;margin:0}\nform{display:flex;gap:8px}input{background:#161b22;border:1px solid #232b36;color:#e6edf3;padding:10px;border-radius:8px}\nbutton{background:#1d9bf0;border:0;color:#fff;padding:10px 16px;border-radius:8px;font-weight:600}</style></head>\n<body><form method="get" action="/"> <input name="t" placeholder="访问 token" autofocus> <button>进入</button></form></body></html>'
        self._send(200, html.encode(), 'text/html; charset=utf-8')

    def _feed(self, query: dict):
        limit = self._int_query(query, 'limit', 40, 1, 200)
        before_ts, before_id = parse_cursor(query.get('before', [''])[0])
        handle = (query.get('handle', [''])[0] or '').lstrip('@') or None
        text = (query.get('q', [''])[0] or '').strip()
        tab = (query.get('tab', [''])[0] or '').strip() or None
        if text:
            items = self.app.store.search(text, limit)
        else:
            items = self.app.store.timeline(limit=limit, before_ts=before_ts, before_id=before_id, handle=handle, tab=tab)
        self._json({'items': items})

    def _resolve(self, query: dict):
        url = (query.get('url', [''])[0] or '').strip()
        if not url:
            raise BadRequest('缺少 url')
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('t.co', 'www.t.co'):
            return self._json({'error': '只解析 t.co 短链'}, 403)
        try:
            final = resolve_short(url, self.app.cfg.get('proxy', ''))
            return self._json({'url': final, 'short': url})
        except Exception as exc:
            return self._json({'error': f'短链解析失败: {exc}'}, 502)

    def _article(self, query: dict):
        article_id = (query.get('id', [''])[0] or '').strip()
        if not article_id.isdigit():
            raise BadRequest('文章 id 不合法')
        try:
            data = self.app.client.article(article_id)
        except XError as exc:
            return self._json({'error': str(exc)}, 502)
        if query.get('debug'):
            return self._json(data)
        try:
            return self._json(normalize_article(data, article_id))
        except Exception as exc:
            traceback.print_exc()
            return self._json({'error': f'文章解析失败: {exc}'}, 502)

    def _user(self, query: dict):
        handle = (query.get('handle', [''])[0] or '').strip().lstrip('@')
        if not handle:
            raise BadRequest('缺少 handle')
        logged_in = self.app.client.logged_in
        try:
            info = self.app.client.user_info(handle)
            if not logged_in:
                info['following'] = None
                info['follow_request_sent'] = None
            return self._json(info)
        except XError as exc:
            cached = self.app.store.find_user(handle)
            if cached:
                cached.update({'following': None, 'follow_request_sent': None, 'cached': True})
                return self._json(cached)
            msg = str(exc)
            code = 404 if '用户不存在' in msg or 'not found' in msg.lower() else 502
            return self._json({'error': msg}, code)
        except Exception as exc:
            cached = self.app.store.find_user(handle)
            if cached:
                cached.update({'following': None, 'follow_request_sent': None, 'cached': True})
                return self._json(cached)
            return self._json({'error': f'{type(exc).__name__}: {exc}'}, 500)

    def _user_pull(self, query: dict):
        handle = (query.get('handle', [''])[0] or '').strip().lstrip('@')
        if not handle:
            return self._json({'error': '缺少 handle', 'items': []}, 400)
        cursor = (query.get('cursor', [''])[0] or '').strip()
        try:
            info = self.app.client.user_id(handle)
            tweets, next_cursor = self.app.client.user_tweets_page(info['id'], count=self.app.cfg['fetch_count'], cursor=cursor)
            for tw in tweets:
                tw['source'] = 'user:lookup'
            added = self.app.store.upsert(tweets, 'user:lookup')
            log(f'[user] @{handle} 抓到 {len(tweets)} 条（新增 {added}）')
            return self._json({'ok': True, 'added': added, 'items': tweets, 'next_cursor': next_cursor})
        except (XError, NotLoggedIn) as exc:
            return self._json({'error': f'拉取用户推文失败: {exc}', 'items': []}, 502)
        except Exception as exc:
            return self._json({'error': f'{type(exc).__name__}: {exc}', 'items': []}, 500)

    def _follow(self, query: dict):
        if not self.app.client.logged_in:
            return self._json({'error': '关注需要登录 cookie（由本机 local 推送）'}, 401)
        handle = (query.get('handle', [''])[0] or '').strip().lstrip('@')
        if not handle:
            return self._json({'error': '缺少 handle'}, 400)
        try:
            info = self.app.client.user_info(handle)
            self.app.client.follow(info['id'])
            after = self.app.client.user_info(handle)
            log(f'[follow] @{handle}')
            return self._json({'ok': True, 'user': after})
        except XError as exc:
            msg = str(exc)
            code = 404 if '用户不存在' in msg else 502
            return self._json({'error': msg}, code)
        except Exception as exc:
            return self._json({'error': f'{type(exc).__name__}: {exc}'}, 500)

    def _retweet(self, query: dict):
        if not self.app.client.logged_in:
            return self._json({'error': '转发需要登录 cookie（由本机 local 推送）'}, 401)
        tweet_id = (query.get('id', [''])[0] or '').strip()
        if not tweet_id.isdigit():
            return self._json({'error': '推文 id 不合法'}, 400)
        try:
            self.app.client.retweet(tweet_id)
            log(f'[retweet] {tweet_id}')
            return self._json({'ok': True, 'id': tweet_id})
        except XError as exc:
            return self._json({'error': f'转发失败: {exc}'}, 502)
        except Exception as exc:
            return self._json({'error': f'{type(exc).__name__}: {exc}'}, 500)

    def _search(self, query: dict):
        text = (query.get('q', [''])[0] or '').strip()
        if not text:
            return self._json({'items': []})
        if query.get('live'):
            if not self.app.client.logged_in:
                return self._json({'error': 'X 全站搜索需要登录 cookie（由本机 local 推送）', 'items': []}, 401)
            try:
                tweets = self.app.client.search(text, count=40)
                self.app.store.upsert(tweets, 'search')
            except NotLoggedIn as exc:
                return self._json({'error': f'搜索失败: {exc}', 'items': []}, 401)
            except XError as exc:
                return self._json({'error': f'搜索失败: {exc}', 'items': []})
        self._json({'items': self.app.store.search(text, 60)})

    def _pull(self, query: dict):
        limit = self._int_query(query, 'limit', 500, 1, 2000)
        since_id = (query.get('since_id', [''])[0] or '').strip()
        before_id = (query.get('before_id', [''])[0] or '').strip()
        if since_id and before_id:
            raise BadRequest('since_id 与 before_id 不能同时使用')
        items, has_more = self.app.store.pull(since_id=since_id, before_id=before_id, limit=limit)
        self._json({'items': items, 'has_more': has_more})

    def _img(self, query: dict):
        src = (query.get('src', [''])[0] or '').strip()
        if not src:
            return self._send(400, b'missing src')
        parsed = urllib.parse.urlparse(src)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in MEDIA_HOSTS:
            return self._send(403, b'host not allowed')
        variant = (query.get('v', ['thumb'])[0] or 'thumb').strip()
        return self._img_direct(src, variant)

    def _img_direct(self, src: str, variant: str):
        app = self.app
        upstream = pbs_variant(src, variant, app.cfg.get('thumb', '360x360'))
        key = hashlib.sha1(upstream.encode()).hexdigest()
        expected_type = 'video/' if variant == 'video' else 'image/'
        hit = app.cache.open_for_send(key)
        if hit:
            fh, ctype, size = hit
            if not ctype.lower().startswith(expected_type) or ctype.lower().startswith('image/svg'):
                fh.close()
                return self._send(502, b'upstream error: invalid media Content-Type')
            with fh:
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(size))
                self.send_header('Cache-Control', 'max-age=604800')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                if self.command != 'HEAD':
                    shutil.copyfileobj(fh, self.wfile, 65536)
            return
        neg = app.cache.get_neg(key)
        if neg:
            return self._send(502, f'upstream error: {neg}'.encode())
        resp = None
        candidates = [upstream]
        if 'format=webp' in upstream:
            candidates.append(re.sub('&?format=webp', '', upstream))
        for attempt_url in candidates:
            req = urllib.request.Request(attempt_url, headers={'User-Agent': UA, 'Referer': 'https://x.com/'})
            try:
                resp = _opener(app.cfg.get('proxy', '')).open(req, timeout=60)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and 'format=webp' in attempt_url:
                    continue
                if exc.code == 404:
                    app.cache.put_neg(key, 'HTTP 404')
                return self._send(502, f'upstream error: HTTP {exc.code}'.encode())
            except Exception as exc:
                return self._send(502, f'upstream error: {exc}'.encode())
        if resp is None:
            app.cache.put_neg(key, 'HTTP 404')
            return self._send(502, b'upstream error: HTTP 404')
        ctype = resp.headers.get('Content-Type') or 'application/octet-stream'
        if not ctype.lower().startswith(expected_type) or ctype.lower().startswith('image/svg'):
            resp.close()
            return self._send(502, b'upstream error: invalid media Content-Type')
        try:
            total = int(resp.headers.get('Content-Length') or 0)
        except ValueError:
            total = 0
        if total > MAX_MEDIA:
            app.cache.put_neg(key, 'media too large')
            return self._send(413, b'media too large')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'max-age=604800')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if total:
            self.send_header('Content-Length', str(total))
        else:
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        tmp = os.path.join(app.cache_dir, f'{key}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp')
        sent = 0
        try:
            with open(tmp, 'wb') as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if sent > MAX_MEDIA:
                        raise RuntimeError('media too large')
                    fh.write(chunk)
                    if self.command != 'HEAD':
                        self.wfile.write(chunk)
            collected = app.cache.finish(tmp, key, ctype)
            if collected and collected[0]:
                removed, freed, total_bytes = collected
                log(f'[cache] GC 删除 {removed} 个旧媒体，释放 {freed / 1024 ** 2:.1f} MiB，剩余 {total_bytes / 1024 ** 2:.1f} MiB')
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _fetch_one(self, handle: str):
        try:
            account = next((a for a in self.app.store.accounts() if a['handle'] == handle), None)
            if not account:
                return
            uid = account['user_id']
            if not uid:
                uid = self.app.client.user_id(handle)['id']
                self.app.store.upsert_account(handle, user_id=uid)
            tweets = self.app.client.user_tweets(uid, count=self.app.cfg['fetch_count'])
            self.app.store.upsert(tweets, f'user:{handle}')
            self.app.store.upsert_account(handle, last_fetch=int(time.time()), last_error='')
        except Exception as exc:
            self.app.store.upsert_account(handle, last_error=str(exc))

def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'config.json')
    cfg = load_config(cfg_path)
    if not os.path.exists(cfg_path):
        log(f'未找到 {cfg_path}，使用默认配置')
    app = App(cfg)
    app.poller.start()
    server = ThreadingHTTPServer(('0.0.0.0', int(cfg['port'])), Handler)
    server.daemon_threads = True
    server.app = app
    detail = '已登录' if app.client.logged_in else 'guest 模式（无 home timeline / 搜索）'
    log(f"xviewer[{app.role}] 监听 :{cfg['port']}  {detail}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.poller.stop()
        server.shutdown()
if __name__ == '__main__':
    main()
