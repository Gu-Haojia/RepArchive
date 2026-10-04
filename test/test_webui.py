import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import webui
import archive_site as site
import export_replive as core
from test.test_archive_site import fixture


class WebUITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'backup';fixture(self.root);site.generate(self.root)
        self.controller = webui.Controller(self.root, Path(self.temp.name) / 'private')
        self.server = webui.make_server(0, self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self, path, method='GET', data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        supplied = {'X-CSRF-Token': self.controller.csrf}
        supplied.update(headers or {})
        body = json.dumps(data or {}) if method == 'POST' else None
        connection.request(method, path, body, supplied)
        response = connection.getresponse();result = response.status, dict(response.getheaders()), response.read();connection.close();return result
    def test_security_and_preview_range_and_no_private_files(self):
        lib = self.controller.settings['libraries'][0]['id']
        self.assertEqual(self.request('/api/status', headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/settings','POST',{'output':str(self.root)}, {'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/settings','POST',{}, {'X-CSRF-Token':'bad'})[0],403)
        preview = '/preview/'+lib+'/'
        self.assertEqual(self.request(preview+'../../.private/refresh_token.txt')[0],404)
        self.assertEqual(self.request(preview+'_data/archive.sqlite3')[0],404)
        manifest = site.read_json(self.root / site.MARKER)
        picture = next(p for p in manifest['files'] if p.endswith('.jpg'))
        from urllib.parse import quote
        path=preview+quote(picture)
        status,headers,body=self.request(path,headers={'Range':'bytes=1-2'})
        self.assertEqual(status,206);self.assertEqual(body,b'pe');self.assertEqual(headers['Content-Range'],'bytes 1-2/4')
        self.assertEqual(self.request(path,headers={'Range':'bytes=10-'})[0],416)
        self.assertEqual(self.request(path,headers={'Range':'bytes=-0'})[0],416)
        self.assertEqual(self.request(path,method='HEAD')[2],b'')
        self.controller.token_path.write_text('should-never-leak')
        status=self.request('/api/status')[2].decode()
        self.assertNotIn('should-never-leak',status)
    def test_settings_offline_generation_and_single_worker_cancel(self):
        custom = Path(self.temp.name)/'custom'
        self.assertEqual(self.request('/api/settings','POST',{'output':str(custom)})[0],200)
        self.assertEqual(self.controller.settings['output'],str(custom.resolve()))
        lib = self.controller.settings['libraries'][0]['id']
        self.assertEqual(self.request('/api/jobs','POST',{'kind':'verify','library':lib})[0],200)
        self.controller.worker.join(5)
        self.assertEqual(self.controller.job['state'],'complete')
        def blocked(root, progress, cancel):
            cancel.wait(5);core.check_cancel(cancel)
        with patch.object(site,'generate',side_effect=blocked):
            self.controller.start('generate',library=lib)
            with self.assertRaises(ValueError):self.controller.start('verify',library=lib)
            self.request('/api/jobs/cancel','POST');self.controller.worker.join(5)
        self.assertEqual(self.controller.job['state'],'cancelled')
        self.assertTrue((self.root/'_data/archive.sqlite3').is_file())
    def test_sms_login_does_not_start_export_or_persist_phone_otp(self):
        class Phone:
            def send_code(self, country, phone):pass
            def login(self, code):
                return type('Response',(),{'refresh_token':'opaque-test-session','user':core.pb.UserPrivate(user_id='user',display_name='name')})()
        self.controller.phone=Phone()
        self.assertEqual(self.request('/api/auth/send','POST',{'country':'81','phone':'090private'})[0],200)
        self.assertEqual(self.request('/api/auth/login','POST',{'code':'123456'})[0],200)
        self.assertFalse(self.controller.active())
        self.assertEqual(self.controller.token_path.stat().st_mode & 0o777,0o600)
        for file in self.controller.state_dir.glob('*.json'):
            self.assertNotIn('123456',file.read_text());self.assertNotIn('090private',file.read_text())

    def test_sns_callback_bridge_saves_existing_account_and_does_not_leak_pending_secrets(self):
        from sns_login import GOOGLE_REDIRECT
        from callback_bridge import forward
        from urllib.parse import urlencode
        def activate(port, csrf, attempt):
            self.controller.bridge.bind(port, csrf, attempt)
            return True
        with patch.object(self.controller.bridge, 'activate', side_effect=activate) as automatic:
            code,_,body=self.request('/api/auth/sns-start','POST',{'provider':'google'})
        automatic.assert_called_once()
        self.assertEqual(code,200);start=json.loads(body)
        self.assertTrue(start['automatic_callback'])
        pending=dict(self.controller.sns.pending)
        callback=GOOGLE_REDIRECT+'?'+urlencode({'state':pending['state'],'code':'secret-auth-code'})
        status=self.request('/api/status')[2].decode()
        self.assertNotIn(start['attempt'],status);self.assertNotIn(pending['verifier'],status)
        self.assertEqual(self.controller.bridge.config.stat().st_mode & 0o777,0o600)
        import sns_auth_pb2 as sns
        response=sns.UserAuthBySNSResponse(user={'user_id':'existing','display_name':'name'},access_token='access',refresh_token='refresh-existing-account')
        with patch.object(self.controller.sns,'_guest'),patch.object(self.controller.sns,'exchange_google',return_value={'id_token':'id','access_token':'access'}),patch.object(self.controller.sns,'_rpc',return_value=response):
            forward(self.controller.bridge.config,callback)
        self.assertEqual(self.controller.account['user_id'],'existing')
        self.assertFalse(self.controller.bridge.config.exists());self.assertFalse(self.controller.active())
        self.assertEqual(self.request('/api/auth/sns-finish','POST',{'attempt':start['attempt'],'callback':callback})[0],400)
        for file in self.controller.state_dir.glob('*.json'):
            self.assertNotIn('secret-auth-code',file.read_text())

    def test_new_jobs_are_returned_immediately(self):
        lib=self.controller.settings['libraries'][0]['id']
        def blocked(root,progress,cancel):cancel.wait(5);core.check_cancel(cancel)
        with patch.object(site,'generate',side_effect=blocked):
            code,_,body=self.request('/api/jobs','POST',{'kind':'generate','library':lib})
            self.assertEqual(code,200);job=json.loads(body)['job'];self.assertEqual(job['library'],lib)
            self.assertTrue(json.loads(self.request('/api/status')[2])['busy'])
            self.request('/api/jobs/cancel','POST');self.controller.worker.join(5)

    def test_clear_history_persists_without_affecting_running_job(self):
        self.controller.start('verify', library=self.controller.settings['libraries'][0]['id'])
        self.controller.worker.join(5)
        self.assertTrue(self.controller.history)
        def blocked(root, progress, cancel):cancel.wait(5);core.check_cancel(cancel)
        with patch.object(site,'generate',side_effect=blocked):
            self.controller.start('generate',library=self.controller.settings['libraries'][0]['id'])
            ident=self.controller.job['id']
            self.assertEqual(self.request('/api/jobs/history/clear','POST')[0],200)
            self.assertEqual(self.controller.history,[])
            self.assertEqual(site.read_json(self.controller.state_dir/'jobs.json'),[])
            self.assertTrue(self.controller.active());self.assertEqual(self.controller.job['id'],ident)
            self.request('/api/jobs/cancel','POST');self.controller.worker.join(5)
        self.assertEqual(len(self.controller.history),1)
        self.request('/api/jobs/history/clear','POST')
        restored=webui.Controller(self.root,self.controller.state_dir)
        self.assertEqual(restored.history,[]);self.assertIsNone(restored.job)

    def test_automatic_callback_fallback_cancel_expiry_and_shutdown(self):
        def activate(port, csrf, attempt):
            self.controller.bridge.bind(port,csrf,attempt)
            return True
        with patch.object(self.controller.bridge,'activate',side_effect=activate):
            self.request('/api/auth/sns-start','POST',{'provider':'google'})
            self.assertTrue(self.controller.bridge.config.exists())
            self.request('/api/auth/sns-cancel','POST')
            self.assertFalse(self.controller.bridge.config.exists())
            self.request('/api/auth/sns-start','POST',{'provider':'google'})
            self.controller.sns.pending['started']-=601
            self.request('/api/status')
            self.assertFalse(self.controller.bridge.config.exists())
            self.request('/api/auth/sns-start','POST',{'provider':'google'})
            self.request('/api/shutdown','POST')
            self.assertFalse(self.controller.bridge.config.exists())
        with patch.object(self.controller.bridge,'activate',side_effect=ValueError('unsupported')):
            result=self.controller.auth('sns-start',{'provider':'google'},self.server.server_port)
            self.assertFalse(result['automatic_callback'])
            self.assertEqual(self.controller.sns.status()['state'],'waiting')
            self.assertFalse(self.controller.bridge.config.exists())
            self.controller.auth('sns-cancel',{})


if __name__ == '__main__':unittest.main()
