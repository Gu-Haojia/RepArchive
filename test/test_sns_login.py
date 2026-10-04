import base64
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from phone_login import LoginError
from sns_login import SNSLogin, GOOGLE_CLIENT, GOOGLE_REDIRECT, challenge
import sns_auth_pb2 as pb
from callback_bridge import CallbackBridge, forward


class Response:
    def __init__(self, data):self.data=data
    def __enter__(self):return self
    def __exit__(self, *_):pass
    def read(self, *_):return self.data


class SNSTest(unittest.TestCase):
    def google(self, login, **query):
        result=login.start('google');attempt=dict(login.pending)
        callback=GOOGLE_REDIRECT+'?'+urlencode({'state':attempt['state'],'code':'private-authorization-code',**query})
        return result,attempt,callback

    def test_google_pkce_state_redirect_expiry_and_one_use(self):
        now=[0];login=SNSLogin(clock=lambda:now[0]);result,attempt,callback=self.google(login)
        params=parse_qs(urlsplit(result['auth_url']).query)
        self.assertEqual(params['code_challenge'],[challenge(attempt['verifier'])])
        self.assertEqual(params['redirect_uri'],[GOOGLE_REDIRECT])
        with patch.object(login,'_guest'),patch.object(login,'exchange_google',return_value={'id_token':'id','access_token':'google-access'}),patch.object(login,'_rpc') as rpc:
            with self.assertRaises(LoginError):login.finish(result['attempt'],callback.replace(attempt['state'],'wrong-state'))
            with self.assertRaises(LoginError):login.finish(result['attempt'],callback.replace(':/oauth2callback',':/wrong'))
            rpc.assert_not_called()
            rpc.return_value=pb.UserAuthBySNSResponse(user={'user_id':'account'},access_token='access',refresh_token='refresh')
            self.assertEqual(login.finish(result['attempt'],callback).user.user_id,'account')
            request=rpc.call_args.args[1];self.assertEqual(request.id_provider,5);self.assertEqual(request.id_token,'id')
            with self.assertRaises(LoginError):login.finish(result['attempt'],callback)
            self.assertEqual(rpc.call_count,1)
        result,attempt,callback=self.google(login);now[0]=601
        with self.assertRaises(LoginError):login.finish(result['attempt'],callback)
        self.assertEqual(login.status()['state'],'expired')

    def test_twitter_binds_callback_to_request_token(self):
        login=SNSLogin()
        with patch.object(login,'_guest'),patch.object(login,'_rpc',return_value=pb.GetSNSLoginURLResponse(login_url='https://api.twitter.com/oauth/authorize?oauth_token=request-token')) as rpc:
            result=login.start('twitter');request=rpc.call_args.args[1]
            self.assertTrue(request.option4);self.assertEqual(request.id_provider,4)
            callback='replive-user-auth://user-auth?oauth_token=request-token&oauth_verifier=verification'
            with self.assertRaises(LoginError):login.finish(result['attempt'],callback.replace('request-token','other-token'))
            self.assertEqual(rpc.call_count,1)
            verifier=login.pending['verifier'];rpc.return_value=pb.UserAuthBySNSResponse(user={'user_id':'account'},access_token='access',refresh_token='refresh')
            login.finish(result['attempt'],callback)
            request=rpc.call_args.args[1];self.assertEqual(request.code_verifier,verifier);self.assertEqual(request.oauth_verifier,'verification')

    def test_wrong_provider_host_duplicate_fields_and_signup_are_rejected(self):
        login=SNSLogin()
        with self.assertRaises(LoginError):login.start('apple')
        with patch.object(login,'_guest'),patch.object(login,'_rpc',return_value=pb.GetSNSLoginURLResponse(login_url='https://evil.example/?oauth_token=request-token')):
            with self.assertRaises(LoginError):login.start('twitter')
        result,attempt,callback=self.google(login)
        with patch.object(login,'_guest'),patch.object(login,'exchange_google',return_value={'id_token':'id','access_token':'access'}),patch.object(login,'_rpc',return_value=pb.UserAuthBySNSResponse(need_signup=True)) as rpc:
            with self.assertRaises(LoginError):login.finish(result['attempt'],callback+'&code=duplicate')
            rpc.assert_not_called()
            with self.assertRaises(LoginError):login.finish(result['attempt'],callback)
            self.assertEqual(login.status()['state'],'failed');self.assertIsNone(login.pending)
            self.assertNotIn('private-authorization-code',str(login.status()))

    def test_google_token_nonce_and_audience_are_checked_and_secrets_not_in_status(self):
        login=SNSLogin();result,attempt,callback=self.google(login)
        def token(**override):
            claims={'nonce':attempt['nonce'],'aud':GOOGLE_CLIENT,'iss':'https://accounts.google.com','exp':9999999999,**override}
            payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
            return json.dumps({'id_token':'header.'+payload+'.signature','access_token':'private-google-access'}).encode()
        with patch.object(login.opener,'open',return_value=Response(token())) as opened:
            login.exchange_google('private-code',attempt)
            params=parse_qs(opened.call_args.args[0].data.decode());self.assertEqual(params['code_verifier'],[attempt['verifier']])
        for override in ({'nonce':'bad'},{'aud':'bad'},{'exp':0},{'iss':'evil'}):
            with patch.object(login.opener,'open',return_value=Response(token(**override))):
                with self.assertRaises(LoginError):login.exchange_google('private-code',attempt)
        for secret in (attempt['verifier'],attempt['state'],attempt['nonce'],result['auth_url'],result['attempt']):
            self.assertNotIn(secret,json.dumps(login.status()))

    @unittest.skipUnless(sys.platform=='darwin','macOS app generation')
    def test_mac_callback_bundle_is_generated_without_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge=CallbackBridge(Path(temp));bundle=bridge.install(register=False)
            info=plistlib.loads((bundle/'Contents/Info.plist').read_bytes())
            self.assertEqual(info['CFBundleName'],'RepArchive Login')
            self.assertEqual(len(info['CFBundleURLTypes'][0]['CFBundleURLSchemes']),2)
            self.assertFalse(bridge.status()['enabled'])
            self.assertFalse(bridge.config.exists())

    def test_callback_bridge_does_not_forward_to_remote_hosts(self):
        with tempfile.TemporaryDirectory() as temp:
            config=Path(temp)/'callback.json';config.write_text(json.dumps({'url':'https://evil.example/api/auth/sns-finish','csrf':'csrf','attempt':'attempt'}))
            with self.assertRaises(ValueError):forward(config,GOOGLE_REDIRECT+'?state=state&code=code')

    def test_receiver_automatically_installs_and_expires_only_its_own_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge=CallbackBridge(Path(temp))
            def install():
                bridge.write_private(bridge.marker,{'platform':sys.platform,'path':temp,
                    'runtime':str(Path(sys.executable).absolute()),'script':str(Path(__import__('callback_bridge').__file__).resolve()),'state':str(bridge.config)})
            with patch.object(bridge,'install',side_effect=install) as installed, patch.object(bridge,'status',return_value={'supported':True}):
                self.assertTrue(bridge.activate(8769,'csrf','first'));old=bridge.timer
                self.assertTrue(bridge.activate(8769,'csrf','second'))
                installed.assert_called_once()
            old.function()
            self.assertEqual(json.loads(bridge.config.read_text())['attempt'],'second')
            bridge.timer.function()
            self.assertFalse(bridge.config.exists());self.assertIsNone(bridge.timer)


if __name__=='__main__':unittest.main()
