"""Original Google / Twitter login flows, with one-use, expiring callbacks."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request
from urllib.error import HTTPError, URLError

from phone_login import PhoneLogin, LoginError
import sns_auth_pb2 as pb

GOOGLE_CLIENT = '1046086565638-ndihf1c3r8hjli7l1robniulso85j5k5.apps.googleusercontent.com'
GOOGLE_SCHEME = 'com.googleusercontent.apps.1046086565638-ndihf1c3r8hjli7l1robniulso85j5k5'
GOOGLE_REDIRECT = GOOGLE_SCHEME + ':/oauth2callback'
TWITTER_SCHEME = 'replive-user-auth'
PROVIDERS = {'google': 5, 'twitter': 4}
TWITTER_HOSTS = {'api.twitter.com', 'api.x.com', 'twitter.com', 'x.com', 'replive.com', 'www.replive.com'}


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


def query_value(query, name):
    values = query.get(name, [])
    if len(values) != 1 or not values[0]:
        raise LoginError('回调地址缺少或重复了登录参数，请粘贴完整地址。')
    return values[0]


class SNSLogin(PhoneLogin):
    def __init__(self, opener=None, clock=None):
        super().__init__(opener, clock, user_agent='v4.7.3 iPad11,3 iPadOS 16.4', platform='ios')
        self.pending = None
        self.result = {'state': 'idle', 'provider': '', 'detail': ''}

    def status(self):
        if not self.lock.acquire(blocking=False):
            return dict(self.result)
        try:
            if self.pending and self.clock() - self.pending['started'] > 600:
                self.pending = None
                self.result.update(state='expired', detail='登录已超时，请重新打开登录页面。')
            return dict(self.result)
        finally:
            self.lock.release()

    def cancel(self):
        with self.lock:
            self.pending = None
            self.result.update(state='cancelled', detail='登录已取消')

    def start(self, provider):
        if provider not in PROVIDERS:
            raise LoginError('请选择 Google 或 X / Twitter。')
        with self.lock:
            self.pending = None
            verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            attempt = {'id': secrets.token_urlsafe(32), 'provider': provider, 'verifier': verifier,
                       'state': state, 'nonce': nonce, 'started': self.clock()}
            if provider == 'google':
                auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
                    'client_id': GOOGLE_CLIENT, 'redirect_uri': GOOGLE_REDIRECT, 'response_type': 'code',
                    'scope': 'openid email profile', 'state': state, 'nonce': nonce,
                    'code_challenge': challenge(verifier), 'code_challenge_method': 'S256',
                    'include_granted_scopes': 'true', 'gpsdk': 'gid-7.1.0', 'gidenv': 'ios', 'device_os': 'Windows'})
            else:
                self._guest()
                response = self._rpc('GetSNSLoginURL', pb.GetSNSLoginURLRequest(id_provider=4, state=state,
                                     code_challenge=challenge(verifier), option4=True), pb.GetSNSLoginURLResponse)
                auth_url = response.login_url
                parsed = urlsplit(auth_url)
                if parsed.scheme != 'https' or parsed.hostname not in TWITTER_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
                    raise LoginError('登录服务没有返回有效的 X / Twitter 登录地址。')
                # Bind OAuth 1 callback to the request token returned for this attempt.
                attempt['oauth_token'] = query_value(parse_qs(parsed.query), 'oauth_token')
            attempt['auth_url'] = auth_url
            self.pending = attempt
            self.result = {'state': 'waiting', 'provider': provider, 'detail': '等待浏览器完成登录'}
            return {'attempt': attempt['id'], 'provider': provider, 'auth_url': auth_url}

    def login_url(self, ident):
        with self.lock:
            if not self.pending or not isinstance(ident, str) or not secrets.compare_digest(ident, self.pending['id']) or self.clock() - self.pending['started'] > 600:
                raise LoginError('登录请求已结束，请重新打开登录页面。')
            return self.pending['auth_url']

    def finish(self, ident, callback):
        if not isinstance(callback, str) or len(callback) > 16384:
            raise LoginError('请填写完整的登录回调地址。')
        with self.lock:
            attempt = self.pending
            if not attempt or not isinstance(ident, str) or not secrets.compare_digest(ident, attempt['id']):
                raise LoginError('登录请求已结束，请重新打开登录页面。')
            if self.clock() - attempt['started'] > 600:
                self.pending = None
                self.result.update(state='expired', detail='登录已超时，请重新打开登录页面。')
                raise LoginError(self.result['detail'])
            parsed = urlsplit(callback.strip())
            if parsed.fragment or parsed.username or parsed.password:
                raise LoginError('登录回调地址格式不正确。')
            query = parse_qs(parsed.query, keep_blank_values=True)
            if attempt['provider'] == 'google':
                if parsed.scheme != GOOGLE_SCHEME or parsed.netloc or parsed.path != '/oauth2callback':
                    raise LoginError('请粘贴 Google 登录的回调地址。')
                if not secrets.compare_digest(query_value(query, 'state'), attempt['state']):
                    raise LoginError('回调不属于这次登录，请重新打开登录页面。')
                if 'error' in query:
                    raise LoginError('Google 登录未完成，请重新登录。')
                code = query_value(query, 'code')
                request = None
            else:
                if parsed.scheme != TWITTER_SCHEME or parsed.netloc != 'user-auth' or parsed.path not in ('', '/'):
                    raise LoginError('请粘贴 X / Twitter 登录的回调地址。')
                token = query_value(query, 'oauth_token')
                if not secrets.compare_digest(token, attempt['oauth_token']):
                    raise LoginError('回调不属于这次登录，请重新打开登录页面。')
                if 'state' in query and not secrets.compare_digest(query_value(query, 'state'), attempt['state']):
                    raise LoginError('回调不属于这次登录，请重新打开登录页面。')
                request = pb.UserAuthBySNSRequest(id_provider=4, oauth_token=token,
                          oauth_verifier=query_value(query, 'oauth_verifier'), code_verifier=attempt['verifier'])
            # Consume before network exchange. A valid callback cannot be replayed.
            self.pending = None
            self.result.update(state='exchanging', detail='正在完成登录')
            try:
                if request is None:
                    tokens = self.exchange_google(code, attempt)
                    request = pb.UserAuthBySNSRequest(id_provider=5, id_token=tokens['id_token'], access_token=tokens['access_token'])
                self._guest()
                response = self._rpc('UserAuthBySNS', request, pb.UserAuthBySNSResponse)
                if response.need_signup:
                    raise LoginError('该登录方式未关联 Replive 账号，请使用原账号的登录方式。')
                if response.is_unretirable:
                    raise LoginError('账号已删除，请先在原 App 处理账号状态。')
                if not response.user.user_id or not response.refresh_token or not response.access_token:
                    raise LoginError('登录服务未返回完整账号会话，请重新登录。')
            except Exception:
                self.result.update(state='failed', detail='登录未完成，请重新打开登录页面。')
                raise
            self.result.update(state='complete', detail='登录成功')
            return response

    def exchange_google(self, code, attempt):
        form = {'client_id': GOOGLE_CLIENT, 'code': code, 'code_verifier': attempt['verifier'],
                'grant_type': 'authorization_code', 'redirect_uri': GOOGLE_REDIRECT,
                'gidenv': 'ios', 'gpsdk': 'gid-7.1.0', 'device_os': 'Windows'}
        request = Request('https://oauth2.googleapis.com/token', data=urlencode(form).encode(), headers={
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'User-Agent': 'Replive/5601 CFNetwork/1406.0.4 Darwin/22.4.0'})
        try:
            with self.opener.open(request, timeout=30) as response:
                tokens = json.loads(response.read(1024 * 1024))
        except HTTPError:
            raise LoginError('Google 授权码已失效或被拒绝，请重新打开登录页面。') from None
        except (URLError, OSError, ValueError):
            raise LoginError('Google 登录请求失败，请重新打开登录页面。') from None
        if not isinstance(tokens, dict) or not tokens.get('id_token') or not tokens.get('access_token'):
            raise LoginError('Google 未返回完整登录信息。')
        # Tokens arrive directly over authenticated TLS from Google's token endpoint.
        # Additionally verify nonce, audience, issuer and expiry for this attempt.
        try:
            payload = tokens['id_token'].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            if (claims.get('aud') != GOOGLE_CLIENT or claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com')
                    or claims.get('nonce') != attempt['nonce'] or float(claims.get('exp', 0)) <= time.time()):
                raise ValueError()
        except (ValueError, IndexError, TypeError, AttributeError):
            raise LoginError('Google 登录信息与本次请求不匹配，请重新登录。') from None
        return tokens
