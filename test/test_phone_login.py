import gzip
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

import export_replive as app
import phone_login as login


class FakeTransport:
    def __init__(self, fail_send=False, retired=False):
        self.calls = []
        self.fail_send = fail_send
        self.retired = retired

    def open(self, request, timeout):
        self.calls.append(request)
        method = request.full_url.rsplit('/', 1)[-1]
        if method == 'SignupAsGuest':
            response = login.pb.SignupAsGuestResponse(user_id='guest', guest_token='guest-test-secret')
        elif method == 'SendAuthCode':
            if self.fail_send:
                raise URLError('test timeout')
            response = login.pb.SendAuthCodeResponse()
        elif method == 'LoginBySMS':
            response = login.pb.LoginBySMSResponse(user=app.pb.UserPrivate(user_id='original-account'),
                                                  access_token='test-access', refresh_token='test-refresh',
                                                  is_unretirable=self.retired)
        else:
            raise AssertionError('Unexpected login route: ' + method)
        return io.BytesIO(gzip.compress(response.SerializeToString()))


class PhoneLoginTest(unittest.TestCase):
    def test_phone_country_and_domestic_format(self):
        self.assertEqual(login.normalize_phone('+81', '０９０－１２３４－５６７８'), ('81', '9012345678'))
        self.assertEqual(login.normalize_phone('81', '+81 90 1234 5678'), ('81', '9012345678'))
        self.assertEqual(login.normalize_phone('86', '13800138000'), ('86', '13800138000'))
        with self.assertRaises(login.LoginError):
            login.normalize_phone('86', '+81 90 1234 5678')
        with self.assertRaises(login.LoginError):
            login.normalize_phone('81', '123')

    def test_existing_account_sms_flow_and_headers(self):
        transport = FakeTransport()
        client = login.PhoneLogin(opener=transport, clock=lambda: 0)
        client.send_code('81', '09012345678')
        response = client.login('１２３４５６')
        self.assertEqual(response.user.user_id, 'original-account')
        self.assertEqual([x.full_url.rsplit('/', 1)[-1] for x in transport.calls],
                         ['SignupAsGuest', 'SendAuthCode', 'LoginBySMS'])
        sent = login.pb.SendAuthCodeRequest.FromString(transport.calls[1].data)
        self.assertEqual((sent.phone_country_code, sent.mobile_phone_number), ('81', '9012345678'))
        self.assertFalse(sent.is_to_register)
        self.assertEqual(login.pb.LoginBySMSRequest.FromString(transport.calls[2].data).auth_code, '123456')
        for request in transport.calls[1:]:
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers['x-replive-guest-token'], 'guest-test-secret')
            self.assertEqual(headers['x-replive-platform'], 'android')
            self.assertNotIn('authorization', headers)
            self.assertEqual(request.get_method(), 'POST')
        self.assertIsNone(client.phone)

    def test_sms_timeout_is_not_retried_and_received_code_can_still_login(self):
        transport = FakeTransport(fail_send=True)
        client = login.PhoneLogin(opener=transport, clock=lambda: 10)
        with self.assertRaisesRegex(login.LoginError, '已经收到短信'):
            client.send_code('81', '09012345678')
        with self.assertRaisesRegex(login.LoginError, '60 秒'):
            client.send_code('81', '09012345678')
        self.assertEqual(len(transport.calls), 2)
        client.login('123456')
        self.assertEqual(len(transport.calls), 3)

    def test_bad_code_and_missing_send_do_not_call_api(self):
        transport = FakeTransport()
        client = login.PhoneLogin(opener=transport)
        for code in ('abc', '123456'):
            with self.assertRaises(login.LoginError):
                client.login(code)
        self.assertEqual(transport.calls, [])

    def test_server_error_never_echoes_sensitive_response(self):
        class ErrorTransport:
            def open(self, request, timeout):
                body = json.dumps({'code': 'invalid_argument', 'message': 'private-phone-and-code'}).encode()
                raise HTTPError(request.full_url, 400, 'bad request', {}, io.BytesIO(body))
        client = login.PhoneLogin(opener=ErrorTransport())
        with self.assertRaises(login.LoginError) as caught:
            client.send_code('81', '09012345678')
        self.assertNotIn('private-phone-and-code', str(caught.exception))
        self.assertEqual(caught.exception.code, 'invalid_argument')

    def test_account_restore_is_blocked(self):
        transport = FakeTransport(retired=True)
        client = login.PhoneLogin(opener=transport)
        client.send_code('81', '09012345678')
        with self.assertRaisesRegex(login.LoginError, '删除或恢复'):
            client.login('123456')
        self.assertEqual(len(transport.calls), 3)


class LocalLoginTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / 'exports/account'
        self.client = login.PhoneLogin(opener=FakeTransport())
        self.exports = []
        self.started = threading.Event()

        def export(token, output):
            self.exports.append((token, output))
            self.started.set()

        self.root_patch = patch.object(app, 'ROOT', self.root)
        self.root_patch.start()
        with app.LOCK:
            app.STATE.clear()
            app.STATE.update(state='waiting_for_phone', detail='waiting')
        self.server = app.make_server(0, self.output, self.client, export)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.opener = build_opener(ProxyHandler({}))
        with self.opener.open(self.base + '/') as response:
            self.csrf = re.search(r'name="csrf" value="([^"]+)"', response.read().decode()).group(1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.root_patch.stop()
        self.temporary.cleanup()

    def post(self, path, values, origin=None):
        data = urlencode({'csrf': self.csrf, **values}).encode()
        headers = {'Origin': origin or self.base}
        return self.opener.open(Request(self.base + path, data=data, headers=headers))

    def test_success_saves_only_session_and_starts_export(self):
        with self.post('/phone/send', {'country_code': '81', 'phone': '09012345678'}):
            pass
        with self.post('/phone/login', {'code': '123456'}) as response:
            self.assertEqual(json.loads(response.read()), {'ok': True})
        self.assertTrue(self.started.wait(2))
        self.assertEqual(self.exports, [('test-refresh', self.output)])
        secret = self.root / '.private/refresh_token.txt'
        self.assertEqual(secret.read_text(), 'test-refresh')
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(secret.parent.stat().st_mode & 0o777, 0o700)
        progress = (self.root / 'exports/progress.json').read_text()
        self.assertNotIn('09012345678', progress)
        self.assertNotIn('123456', progress)
        self.assertNotIn('test-refresh', progress)

    def test_cross_origin_and_bad_csrf_make_no_api_calls(self):
        for origin, values in [('https://example.com', {}), (self.base, {'csrf': 'wrong'})]:
            with self.assertRaises(HTTPError) as caught:
                self.post('/phone/send', values, origin=origin)
            self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.client.opener.calls, [])

    def test_other_account_does_not_overwrite_existing_session(self):
        self.output.mkdir(parents=True)
        (self.output / 'account_identity.json').write_text(json.dumps({'user_id': 'different-account'}))
        app.save_session('original-session')
        with self.post('/phone/send', {'country_code': '81', 'phone': '09012345678'}):
            pass
        with self.assertRaises(HTTPError) as caught:
            self.post('/phone/login', {'code': '123456'})
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual((self.root / '.private/refresh_token.txt').read_text(), 'original-session')
        self.assertFalse(self.started.is_set())

    def test_login_error_is_visible_and_can_be_corrected(self):
        with self.post('/phone/send', {'country_code': '81', 'phone': '09012345678'}):
            pass
        with self.assertRaises(HTTPError):
            self.post('/phone/login', {'code': 'abc'})
        with self.opener.open(self.base + '/status') as response:
            self.assertEqual(json.loads(response.read())['state'], 'login_error')
        with self.post('/phone/login', {'code': '123456'}):
            pass
        self.assertTrue(self.started.wait(2))


if __name__ == '__main__':
    unittest.main()
