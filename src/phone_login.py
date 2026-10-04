"""独立的 Replive 现有账号短信登录，不注册手机号账号或恢复已删除账号。"""
import gzip
import json
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

import phonenumbers

sys.path.insert(0, str(Path(__file__).resolve().parent / 'generated'))
import phone_auth_pb2 as pb

BASE = 'https://api.replive.com/user.v1.UserService/'
USER_AGENT = 'v4.8.1 23116PN5BC Android 12'


class LoginError(ValueError):
    def __init__(self, message, *, code='', status=0):
        super().__init__(message)
        self.code = code
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LoginError('登录接口出现重定向，已停止提交。')


def normalize_phone(country_code, phone):
    country_code = unicodedata.normalize('NFKC', country_code).strip().lstrip('+')
    if not re.fullmatch(r'[1-9][0-9]{0,2}', country_code):
        raise LoginError('请填写有效的国际区号，例如日本 81、中国 86。')
    region = phonenumbers.region_code_for_country_code(int(country_code))
    if region == 'ZZ':
        raise LoginError('国际区号无法识别。')
    try:
        number = phonenumbers.parse(unicodedata.normalize('NFKC', phone).strip(), region)
    except phonenumbers.NumberParseException:
        raise LoginError('手机号格式无法识别。') from None
    if str(number.country_code) != country_code or not phonenumbers.is_possible_number(number) or number.extension:
        raise LoginError('手机号与区号不匹配，或长度不正确。')
    return country_code, phonenumbers.national_significant_number(number)


class PhoneLogin:
    def __init__(self, opener=None, clock=None, user_agent=USER_AGENT, platform='android'):
        # 此 transport 不继承系统 HTTP 代理配置，保留 TLS 验证。
        from urllib.request import ProxyHandler
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.clock = clock or time.monotonic
        self.user_agent = user_agent
        self.platform = platform
        self.guest_token = ''
        self.phone = None
        self.sent_at = None
        self.last_attempt_at = None
        self.lock = threading.Lock()

    def _rpc(self, method, request, response_type, guest=True):
        headers = {'Content-Type': 'application/proto', 'Accept-Encoding': 'gzip',
                   'Accept': 'application/json', 'Accept-Charset': 'UTF-8',
                   'User-Agent': self.user_agent, 'X-Replive-Platform': self.platform}
        if guest:
            headers['X-Replive-Guest-Token'] = self.guest_token
        req = Request(BASE + method, data=request.SerializeToString(), headers=headers)
        try:
            # 发送短信和验证码验证均不自动重试，避免重复短信或重复消耗验证码。
            with self.opener.open(req, timeout=30) as response:
                body = response.read()
        except HTTPError as exc:
            body = exc.read()
            try:
                if body.startswith(b'\x1f\x8b'):
                    body = gzip.decompress(body)
                code = json.loads(body).get('code', '')
            except (ValueError, OSError):
                code = ''
            messages = {
                'invalid_argument': '号码或验证码不正确，请核对后重试。',
                'unauthenticated': '登录前的会话失效，请重新发送验证码。',
                'not_found': '未找到对应账号，请使用已绑定原账号的手机号。',
                'resource_exhausted': '请求过于频繁，请稍后再试。',
                'permission_denied': '服务拒绝此次登录，请在原 App 检查账号状态。',
                'failed_precondition': '账号状态不允许此次登录，请在原 App 检查。',
            }
            if exc.code == 429:
                message = '发送过于频繁，请稍后再试。'
            else:
                message = messages.get(code, f'登录服务返回 HTTP {exc.code}，请稍后重试。')
            raise LoginError(message, code=code, status=exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise LoginError('网络请求未完成。如果已经收到短信，请直接填写验证码，避免重复发送。') from None
        if body.startswith(b'\x1f\x8b'):
            body = gzip.decompress(body)
        try:
            return response_type.FromString(body)
        except Exception:
            raise LoginError('登录服务响应格式异常，已停止。') from None

    def _guest(self):
        if not self.guest_token:
            result = self._rpc('SignupAsGuest', pb.SignupAsGuestRequest(country_code='JP', language_tag='ja'), pb.SignupAsGuestResponse, guest=False)
            if not result.guest_token:
                raise LoginError('没有取得登录前的访客会话。')
            self.guest_token = result.guest_token

    def send_code(self, country_code, phone):
        normalized = normalize_phone(country_code, phone)
        with self.lock:
            now = self.clock()
            if self.last_attempt_at is not None and now - self.last_attempt_at < 60:
                raise LoginError('请等待 60 秒后再发送验证码。')
            self._guest()
            # 即使超时，也保留号码和尝试时间，让已收到短信的用户能够登录。
            self.phone = normalized
            self.sent_at = now
            self.last_attempt_at = now
            self._rpc('SendAuthCode', pb.SendAuthCodeRequest(phone_country_code=normalized[0],
                      mobile_phone_number=normalized[1], is_to_register=False), pb.SendAuthCodeResponse)

    def login(self, code):
        code = unicodedata.normalize('NFKC', code).strip()
        if not re.fullmatch(r'[0-9]{4,8}', code):
            raise LoginError('请填写短信中的数字验证码。')
        with self.lock:
            if self.phone is None or not self.guest_token:
                raise LoginError('请先发送验证码，再填写收到的短信验证码。')
            # 登录直接使用验证码；VerifyAuthCode 属于另一条流程，提前调用可能消耗验证码。
            result = self._rpc('LoginBySMS', pb.LoginBySMSRequest(phone_country_code=self.phone[0],
                               mobile_phone_number=self.phone[1], auth_code=code), pb.LoginBySMSResponse)
            if result.is_unretirable:
                raise LoginError('账号处于删除或恢复状态，请先在原 App 确认；程序不会恢复或注册账号。')
            if not result.user.user_id or not result.refresh_token or not result.access_token:
                raise LoginError('没有取得原账号的完整登录会话，已停止。')
            self.phone = None
            return result

    def preflight(self):
        """只用空字段检验登录路由与访客会话，不发送短信。"""
        with self.lock:
            self._guest()
            try:
                self._rpc('LoginBySMS', pb.LoginBySMSRequest(), pb.LoginBySMSResponse)
            except LoginError as exc:
                if exc.status == 400 and exc.code == 'invalid_argument':
                    return {'guest_session': True, 'sms_login_route': True, 'sms_sent': False}
                raise
            raise LoginError('空验证码意外被服务接受，登录检查未通过。')
