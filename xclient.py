from __future__ import annotations
import gzip
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
BEARER = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'
DOC_URL = 'https://raw.githubusercontent.com/fa0311/TwitterInternalAPIDocument/develop/docs/markdown/GraphQL.md'
DOC_TTL = 12 * 3600

class XError(Exception):

    def __init__(self, message: str, status: int | None=None):
        super().__init__(message)
        self.status = status

class NotLoggedIn(XError):
    pass

def _op_sections(doc: str) -> dict[str, dict]:
    ops: dict[str, dict] = {}
    for sec in re.split('\\n## ', doc):
        m = re.match('([A-Za-z0-9_]+)<br>\\nRequest URL: `https://x\\.com/i/api/graphql/([A-Za-z0-9_-]+)/([A-Za-z0-9_]+)`', sec)
        if not m or m.group(1) != m.group(3):
            continue
        features: dict[str, bool] = {}
        field_toggles: dict[str, bool] = {}
        table = 'features'
        for line in sec.splitlines():
            stripped = line.strip()
            if stripped.startswith('#### '):
                table = stripped[5:].strip()
                continue
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            if len(cells) >= 3 and cells[1] == 'boolean' and re.fullmatch('[01]|true|false', cells[2], re.I):
                value = cells[2].lower() in ('1', 'true')
                if table == 'fieldToggles':
                    field_toggles[cells[0]] = value
                else:
                    features[cells[0]] = value
        ops[m.group(1)] = {'id': m.group(2), 'features': features, 'field_toggles': field_toggles}
    return ops

class XClient:

    def __init__(self, ops_path: str='ops.json', cookies: str='', proxy: str='', timeout: int=30, log=print):
        self.ops_path = ops_path
        self.cookies = (cookies or '').strip()
        self.timeout = timeout
        self.log = log
        self._lock = threading.Lock()
        self._guest: str | None = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}) if proxy else urllib.request.ProxyHandler({}))
        self._ops = {}
        self._ops.update(self._load_ops())

    def set_cookies(self, cookies: str) -> None:
        self.cookies = (cookies or '').strip()

    def _load_ops(self) -> dict[str, dict]:
        try:
            with open(self.ops_path, encoding='utf-8') as fh:
                data = json.load(fh)
            ops = data.get('ops', data)
            age = time.time() - float(data.get('fetched_at', 0) or 0)
            if not ops or age > DOC_TTL:
                threading.Thread(target=self._refresh_ops_quiet, daemon=True).start()
            return ops
        except (OSError, ValueError):
            threading.Thread(target=self._refresh_ops_quiet, daemon=True).start()
            return {}

    def _refresh_ops_quiet(self):
        try:
            self.refresh_ops()
        except Exception as exc:
            self.log(f'[ops] 刷新失败: {exc}')

    def refresh_ops(self) -> int:
        status, body = self._request('GET', DOC_URL)
        if status != 200:
            raise XError(f'下载文档失败 {status}', status)
        ops = _op_sections(body.decode('utf-8', 'replace'))
        if not ops:
            raise XError('文档解析为空')
        with self._lock:
            self._ops.update(ops)
            tmp = f'{self.ops_path}.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump({'fetched_at': time.time(), 'source': DOC_URL, 'ops': self._ops}, fh, indent=1)
            os.replace(tmp, self.ops_path)
        return len(ops)

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies)

    def _headers(self) -> dict[str, str]:
        headers = {'User-Agent': UA, 'Authorization': f'Bearer {BEARER}', 'x-twitter-active-user': 'yes', 'x-twitter-client-language': 'en', 'Accept': '*/*', 'Accept-Encoding': 'gzip', 'Referer': 'https://x.com/', 'Origin': 'https://x.com'}
        if self.cookies:
            headers['Cookie'] = self.cookies
            m = re.search('(?:^|;\\s*)ct0=([^;]+)', self.cookies)
            if m:
                headers['x-csrf-token'] = m.group(1)
        else:
            token = self.guest_token()
            if token:
                headers['x-guest-token'] = token
        return headers

    def _request(self, method: str, url: str, data: bytes | None=None, headers: dict | None=None):
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    if (resp.headers.get('Content-Encoding') or '').lower() == 'gzip':
                        body = gzip.decompress(body)
                    return (resp.status, body)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if (exc.headers.get('Content-Encoding') or '').lower() == 'gzip':
                    body = gzip.decompress(body)
                return (exc.code, body)
            except Exception as exc:
                if attempt == 2:
                    raise XError(f'网络错误: {exc}') from exc
                time.sleep(0.8 * (attempt + 1))
        raise XError('网络错误：重试耗尽')

    def guest_token(self, force: bool=False) -> str | None:
        with self._lock:
            if self._guest and (not force):
                return self._guest
            headers = {'User-Agent': UA, 'Authorization': f'Bearer {BEARER}', 'x-twitter-active-user': 'yes', 'x-twitter-client-language': 'en'}
            status, body = self._request('POST', 'https://api.x.com/1.1/guest/activate.json', b'', headers)
            if status != 200:
                self._guest = None
                raise XError(f'获取 guest token 失败 {status}: {body[:120]!r}', status)
            self._guest = json.loads(body)['guest_token']
            return self._guest
    POST_OPS = {'SearchTimeline', 'CreateRetweet'}

    def _call_op(self, op: str, spec: dict, variables: dict, toggles: dict | None, method: str, features: dict | None=None):
        params = {'variables': json.dumps(variables, separators=(',', ':')), 'features': json.dumps(features if features is not None else spec.get('features', {}), separators=(',', ':'))}
        if toggles:
            params['fieldToggles'] = json.dumps(toggles, separators=(',', ':'))
        headers = self._headers()
        if method == 'POST':
            headers['content-type'] = 'application/json'
            url = f"https://x.com/i/api/graphql/{spec['id']}/{op}"
            body = {'variables': variables, 'features': features if features is not None else spec.get('features', {})}
            if toggles:
                body['fieldToggles'] = toggles
            return self._request('POST', url, json.dumps(body).encode(), headers)
        url = f"https://x.com/i/api/graphql/{spec['id']}/{op}?" + urllib.parse.urlencode(params)
        return self._request('GET', url, headers=headers)

    def graphql(self, op: str, variables: dict, field_toggles: dict | None=None, features: dict | None=None) -> dict:
        if op not in self._ops:
            self.refresh_ops()
        if op not in self._ops:
            raise XError(f'未知操作 {op}')
        toggles = field_toggles or self._ops[op].get('field_toggles') or None
        methods = ['POST'] if op in self.POST_OPS else ['GET', 'POST']
        refreshed = False
        last: tuple[int, bytes] = (0, b'')
        for method in methods:
            for _ in range(2):
                spec = self._ops[op]
                status, body = self._call_op(op, spec, variables, toggles, method, features)
                last = (status, body)
                if status == 200:
                    payload = json.loads(body)
                    if payload.get('data') is not None:
                        return payload
                    errors = payload.get('errors') or []
                    raise XError(errors[0].get('message', '未知错误') if errors else '空响应')
                if status == 404 and (not refreshed):
                    refreshed = True
                    self.log(f'[ops] {op} 返回 404，刷新 queryId 后重试')
                    self.refresh_ops()
                    continue
                break
            if status in (401, 403):
                break
        status, body = last
        if status in (401, 403) and (not self.logged_in):
            self._guest = None
            raise NotLoggedIn('该接口需要登录 cookie（guest 身份不可用）', status)
        raise XError(f'{op} 失败 HTTP {status}: {body[:160]!r}', status)

    def user_info(self, handle: str) -> dict:
        data = self.graphql('UserByScreenName', {'screen_name': handle.lstrip('@')})
        result = ((data.get('data') or {}).get('user') or {}).get('result') or {}
        if not result or result.get('__typename') not in (None, 'User'):
            raise XError('用户不存在或不可见')
        legacy = result.get('legacy') or {}
        core = result.get('core') or {}
        user_id = result.get('rest_id') or ''
        following = legacy.get('following')
        follow_request_sent = legacy.get('follow_request_sent')
        if self.logged_in and user_id:
            rel = self.relationship(user_id)
            if rel is not None:
                following = rel['following']
                follow_request_sent = rel['follow_request_sent']
        return {'id': user_id, 'handle': core.get('screen_name') or handle.lstrip('@'), 'name': core.get('name') or handle.lstrip('@'), 'avatar': (result.get('avatar') or {}).get('image_url') or '', 'following': following, 'follow_request_sent': follow_request_sent, 'protected': bool(legacy.get('protected'))}

    def relationship(self, user_id: str) -> dict | None:
        if not self.logged_in:
            return None
        url = 'https://api.x.com/1.1/friendships/show.json?target_id=' + urllib.parse.quote(str(user_id))
        headers = self._headers()
        headers['x-twitter-auth-type'] = 'OAuth2Session'
        try:
            status, body = self._request('GET', url, headers=headers)
            if status != 200:
                return None
            source = ((json.loads(body) or {}).get('relationship') or {}).get('source') or {}
            return {'following': bool(source.get('following')), 'follow_request_sent': bool(source.get('following_requested') or source.get('follow_request_sent'))}
        except Exception:
            return None

    def user_id(self, handle: str) -> dict:
        info = self.user_info(handle)
        return {k: info[k] for k in ('id', 'handle', 'name', 'avatar')}

    def article(self, article_id: str) -> dict:
        if not article_id.isdigit():
            raise XError('文章 id 不合法')
        redirect = self.graphql('ArticleRedirectScreenQuery', {'articleEntityId': article_id})
        result = ((redirect.get('data') or {}).get('article_result_by_rest_id') or {}).get('result') or {}
        tweet_id = ((result.get('metadata') or {}).get('tweet_results') or {}).get('rest_id')
        if not tweet_id:
            raise XError('文章不存在、已删除或当前账号不可见')
        data = self.graphql('TweetResultByRestId', {'tweetId': str(tweet_id), 'withCommunity': False, 'includePromotedContent': False, 'withVoice': False}, field_toggles={'withArticleRichContentState': True, 'withArticlePlainText': True})
        tweet = ((data.get('data') or {}).get('tweetResult') or {}).get('result') or {}
        if tweet.get('__typename') == 'TweetWithVisibilityResults':
            tweet = tweet.get('tweet') or {}
        article = ((tweet.get('article') or {}).get('article_results') or {}).get('result') or {}
        if not article:
            raise XError('这条推文没有可读取的文章内容')
        return {'tweet_id': str(tweet_id), 'article': article}

    def retweet(self, tweet_id: str) -> dict:
        if not self.logged_in:
            raise NotLoggedIn('转发需要登录 cookie')
        if not tweet_id.isdigit():
            raise XError('推文 id 不合法')
        return self.graphql('CreateRetweet', {'tweet_id': tweet_id, 'dark_request': False})

    def follow(self, user_id: str) -> dict:
        if not self.logged_in:
            raise NotLoggedIn('关注需要登录 cookie')
        if not user_id:
            raise XError('缺少 user_id')
        form = urllib.parse.urlencode({'user_id': user_id, 'include_profile_interstitial_type': '1', 'include_blocking': '1', 'include_blocked_by': '1', 'include_followed_by': '1', 'include_want_retweets': '1', 'include_mute_edge': '1', 'include_can_dm': '1', 'include_can_media_tag': '1', 'skip_status': '1'}).encode()
        headers = self._headers()
        headers['content-type'] = 'application/x-www-form-urlencoded'
        headers['x-twitter-auth-type'] = 'OAuth2Session'
        status, body = self._request('POST', 'https://api.x.com/1.1/friendships/create.json', form, headers)
        text = body.decode('utf-8', 'replace')
        if status == 200:
            try:
                return json.loads(text) if text else {}
            except json.JSONDecodeError:
                return {}
        if status in (403, 409) and ('already' in text.lower() or '"code":160' in text):
            return {}
        if status in (401, 403):
            raise NotLoggedIn('关注失败：cookie 可能已失效', status)
        raise XError(f'关注失败 HTTP {status}: {text[:160]!r}', status)

    def user_tweets(self, user_id: str, count: int=40, tab: str='tweets') -> list[dict]:
        return self.user_tweets_page(user_id, count, tab)[0]

    def user_tweets_page(self, user_id: str, count: int=40, tab: str='tweets', cursor: str='') -> tuple[list[dict], str]:
        op = {'tweets': 'UserTweets', 'replies': 'UserTweetsAndReplies', 'media': 'UserMedia'}[tab]
        variables = {'userId': user_id, 'count': count, 'includePromotedContent': tab != 'media', 'withVoice': True}
        if cursor:
            variables['cursor'] = cursor
        if tab != 'tweets':
            variables.update({'withCommunity': True, 'withClientEventToken': False, 'withBirdwatchNotes': False})
        data = self.graphql(op, variables)
        return parse_timeline_page(data['data'])

    def home(self, count: int=40, latest: bool=True, seen_ids: list[str] | None=None) -> list[dict]:
        if not self.logged_in:
            raise NotLoggedIn('home timeline 需要登录 cookie')
        op = 'HomeLatestTimeline' if latest else 'HomeTimeline'
        variables = {'count': count, 'includePromotedContent': False, 'withCommunity': True}
        if latest:
            variables.update({'latestControlAvailable': True, 'requestContext': 'launch'})
        elif seen_ids:
            variables['seenTweetIds'] = seen_ids
        data = self.graphql(op, variables)
        return parse_timeline(data['data'])

    def search(self, query: str, product: str='Latest', count: int=40) -> list[dict]:
        if not self.logged_in:
            raise NotLoggedIn('搜索需要登录 cookie')
        variables = {'rawQuery': query, 'count': count, 'querySource': 'typed_query', 'product': product}
        data = self.graphql('SearchTimeline', variables)
        return parse_timeline(data['data'])

    def tweet(self, tweet_id: str) -> dict | None:
        data = self.graphql('TweetResultByRestId', {'tweetId': tweet_id, 'withCommunity': False, 'includePromotedContent': False, 'withVoice': False})
        result = ((data.get('data') or {}).get('tweetResult') or {}).get('result')
        return parse_tweet(result) if result else None
_MONTHS = {m: i for i, m in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(), 1)}

def _ts(created_at: str) -> int:
    try:
        return int(parsedate_to_datetime(created_at).timestamp())
    except Exception:
        return int(time.time())

def _expand_text(text: str, entities: dict) -> str:
    for url in (entities or {}).get('urls') or []:
        text = text.replace(url.get('url', ''), url.get('expanded_url') or url.get('url', ''))
    return text

def _media(legacy: dict) -> list[dict]:
    out = []
    for item in (legacy.get('extended_entities') or {}).get('media') or []:
        kind = item.get('type')
        entry = {'type': kind, 'url': item.get('media_url_https') or '', 'alt': item.get('ext_alt_text') or ''}
        info = item.get('original_info') or {}
        entry['w'] = info.get('width')
        entry['h'] = info.get('height')
        if kind in ('video', 'animated_gif'):
            variants = (item.get('video_info') or {}).get('variants') or []
            mp4 = [v for v in variants if v.get('content_type') == 'video/mp4']
            if mp4:
                entry['video'] = max(mp4, key=lambda v: v.get('bitrate') or 0)['url']
                entry['url'] = item.get('media_url_https') or ''
        out.append(entry)
    return out

def parse_tweet(result: dict | None) -> dict | None:
    if not result:
        return None
    if result.get('__typename') == 'TweetWithVisibilityResults':
        result = result.get('tweet') or {}
    if result.get('__typename') not in (None, 'Tweet'):
        return None
    legacy = result.get('legacy') or {}
    core = ((result.get('core') or {}).get('user_results') or {}).get('result') or {}
    ucore = core.get('core') or {}
    text = (((result.get('note_tweet') or {}).get('note_tweet_results') or {}).get('result') or {}).get('text') or legacy.get('full_text') or ''
    for item in (legacy.get('extended_entities') or {}).get('media') or []:
        text = text.replace(item.get('url', ''), '')
    quoted = parse_tweet((result.get('quoted_status_result') or {}).get('result'))
    retweeted = parse_tweet((legacy.get('retweeted_status_result') or {}).get('result'))
    views = None
    if isinstance(result.get('views'), dict) and result['views'].get('count'):
        try:
            views = int(result['views']['count'])
        except (TypeError, ValueError):
            pass
    created = legacy.get('created_at') or ''
    return {'id': result.get('rest_id') or legacy.get('id_str'), 'created_at': created, 'ts': _ts(created) if created else int(time.time()), 'author': {'id': core.get('rest_id') if core else legacy.get('user_id_str'), 'handle': ucore.get('screen_name') or '', 'name': ucore.get('name') or '', 'avatar': (core.get('avatar') or {}).get('image_url') or ''}, 'text': _expand_text(text, legacy.get('entities')).strip(), 'media': _media(legacy), 'metrics': {'reply': legacy.get('reply_count') or 0, 'retweet': legacy.get('retweet_count') or 0, 'like': legacy.get('favorite_count') or 0, 'quote': legacy.get('quote_count') or 0, 'view': views}, 'lang': legacy.get('lang'), 'reply_to': legacy.get('in_reply_to_status_id_str'), 'quoted': quoted, 'retweeted': retweeted, 'url': f"https://x.com/{ucore.get('screen_name') or 'i'}/status/{result.get('rest_id') or legacy.get('id_str')}"}

def _find_instructions(node, depth: int=0):
    if depth > 8:
        return None
    if isinstance(node, dict):
        if isinstance(node.get('instructions'), list):
            return node['instructions']
        for value in node.values():
            found = _find_instructions(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_instructions(value, depth + 1)
            if found:
                return found
    return None

def parse_timeline_page(payload: dict) -> tuple[list[dict], str]:
    entries: list[dict] = []
    for inst in _find_instructions(payload) or []:
        kind = inst.get('type')
        if kind in ('TimelineAddEntries', 'TimelineReplaceEntry'):
            entries.extend(inst.get('entries') or [])
        elif kind == 'TimelinePinEntry':
            entries.append(inst.get('entry') or {})
        elif kind == 'TimelineAddToModule':
            entries.extend(inst.get('moduleItems') or [])
    tweets = []
    next_cursor = ''
    for entry in entries:
        content = entry.get('content') or entry
        if content.get('cursorType') == 'Bottom':
            next_cursor = content.get('value') or next_cursor
        items = content.get('items') if content.get('__typename') == 'TimelineTimelineModule' else None
        for item in items or [content]:
            item_content = item.get('itemContent') or item.get('item') or {}
            results = item_content.get('tweet_results') if isinstance(item_content, dict) else None
            result = (results or {}).get('result')
            if result:
                parsed = parse_tweet(result)
                if parsed:
                    tweets.append(parsed)
    return (tweets, next_cursor)

def parse_timeline(payload: dict) -> list[dict]:
    return parse_timeline_page(payload)[0]
