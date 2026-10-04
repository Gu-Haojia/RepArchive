"""Project controller. Generated archives are served only as independent previews."""
import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs

from export_replive import ROOT, JST, API, Cancelled, ext, json_write, safe_error, token_from_bytes
from phone_login import PhoneLogin
from sns_login import SNSLogin
from callback_bridge import CallbackBridge
import archive_site as site


class Controller:
    def __init__(self, default_output=None, state_dir=None, phone=None, sns=None):
        self.state_dir = Path(state_dir or os.environ.get('REPLIVE_STATE_DIR', ROOT / '.private')).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.lock = threading.RLock()
        self.phone = phone or PhoneLogin()
        self.sns = sns or SNSLogin()
        self.bridge = CallbackBridge(self.state_dir)
        self.bridge.clear()
        self.csrf = secrets.token_urlsafe(32)
        self.cancel = threading.Event()
        self.worker = None
        self.auth_busy = False
        self.job = None
        self.history = site.read_json(self.state_dir / 'jobs.json', [])
        previous = site.read_json(self.state_dir / 'current-job.json', {})
        if previous and not previous.get('finished'):
            previous.update(state='cancelled', detail='上次任务已停止，可更新资源', finished=datetime.now(JST).isoformat())
            self.history.append(previous)
            json_write(self.state_dir / 'jobs.json', self.history[-15:])
            json_write(self.state_dir / 'current-job.json', previous)
        self.settings = site.read_json(self.state_dir / 'settings.json', {'output': str(default_output or ROOT / 'exports/account'), 'libraries': []})
        self.account = site.read_json(self.state_dir / 'session.json', None)
        self.auth_detail = '已保存登录会话' if self.token_path.exists() else '尚未登录'
        # Previously registered real output becomes a library without network work.
        initial = Path(self.settings['output']).expanduser()
        if (initial / 'archive.sqlite3').is_file() or (initial / '_data/archive.sqlite3').is_file():
            self.register(initial)
        self.persist_settings()

    @property
    def token_path(self):
        return self.state_dir / 'refresh_token.txt'

    def persist_settings(self):
        json_write(self.state_dir / 'settings.json', self.settings)

    def active(self):
        return self.worker is not None and self.worker.is_alive()

    def register(self, root):
        root = site.output_path(root)
        ident = __import__('hashlib').sha256(str(root).encode()).hexdigest()[:20]
        if not any(x['id'] == ident for x in self.settings['libraries']):
            self.settings['libraries'].append({'id': ident, 'path': str(root)})
            self.persist_settings()
        return ident

    def library(self, ident):
        for lib in self.settings['libraries']:
            if lib['id'] == ident:
                return lib
        raise ValueError('备份目录未登记。')

    def status(self):
        with self.lock:
            sns_status = self.sns.status()
            if sns_status['state'] in ('complete', 'failed', 'expired', 'cancelled'):
                self.bridge.clear()
            libs = []
            for lib in self.settings['libraries']:
                root = Path(lib['path'])
                manifest = site.read_json(root / site.MARKER, {})
                report = site.read_json(root / '_data/report.json', site.read_json(root / 'report.json', {}))
                libs.append({**lib, 'name': root.name, 'generated': bool(manifest), 'summary': manifest.get('summary', {}),
                             'report': {'accessible_complete': report.get('accessible_content_complete', False), 'limitations': report.get('limitations', [])},
                             'verified': site.read_json(root / '校验报告.json', None)})
            return {'application': 'reparchive-controller', 'csrf': self.csrf, 'settings': self.settings, 'libraries': libs,
                    'session': {'saved': self.token_path.exists(), 'account': self.account, 'detail': self.auth_detail, 'busy': self.auth_busy, 'sns': sns_status, 'bridge': self.bridge.status()},
                    'job': self.job, 'busy': self.active(), 'history': self.history[-15:]}

    def store_token(self, token, user):
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(token)
        os.chmod(self.token_path, 0o600)
        self.account = {'user_id': user.user_id, 'display_name': user.display_name}
        json_write(self.state_dir / 'session.json', self.account)
        self.auth_detail = '已登录'

    def auth(self, kind, values, port=None):
        with self.lock:
            if self.active() or self.auth_busy:
                raise ValueError('任务或登录正在进行，请等待完成。')
            self.auth_busy = True
        try:
            if kind == 'sns-start':
                self.bridge.clear()
                result = self.sns.start(values.get('provider', ''))
                result['automatic_callback'] = False
                if port:
                    try:
                        result['automatic_callback'] = self.bridge.activate(port, self.csrf, result['attempt'])
                    except (OSError, ValueError, subprocess.SubprocessError):
                        self.bridge.clear()
                return result
            elif kind == 'sns-open':
                import webbrowser
                if not webbrowser.open(self.sns.login_url(values.get('attempt', ''))):
                    raise ValueError('系统浏览器未打开，请使用登录页面链接。')
            elif kind == 'sns-finish':
                result = self.sns.finish(values.get('attempt', ''), values.get('callback', ''))
                self.store_token(result.refresh_token, result.user)
                self.bridge.clear()
            elif kind == 'sns-cancel':
                self.sns.cancel()
                self.bridge.clear()
            elif kind == 'send':
                self.sns.cancel()
                self.phone.send_code(values.get('country', ''), values.get('phone', ''))
                self.auth_detail = '短信已发送，请填写验证码完成登录'
            elif kind == 'login':
                self.sns.cancel()
                result = self.phone.login(values.get('code', ''))
                self.store_token(result.refresh_token, result.user)
            elif kind in ('token', 'check'):
                self.sns.cancel()
                token = token_from_bytes(values.get('token', '').encode()) if kind == 'token' else token_from_bytes(self.token_path.read_bytes())
                user = API(token).rpc('user.v1.UserService/GetUserPrivate', ext.GetUserPrivateRequest(), ext.GetUserPrivateResponse).user
                if not user.user_id:
                    raise ValueError('会话未能确认账号身份。')
                self.store_token(token, user)
            elif kind == 'preflight':
                self.phone.preflight()
                self.auth_detail = '官方手机号登录接口可用（未发送短信）'
            elif kind == 'forget':
                self.sns.cancel()
                self.token_path.unlink(missing_ok=True)
                (self.state_dir / 'session.json').unlink(missing_ok=True)
                self.account = None
                self.auth_detail = '已清除本机保存的会话'
            else:
                raise ValueError('未知登录操作。')
        finally:
            if kind in ('send', 'login', 'token', 'check', 'forget') or self.sns.status()['state'] in ('complete', 'failed', 'expired', 'cancelled'):
                self.bridge.clear()
            with self.lock:
                self.auth_busy = False

    def progress(self, **values):
        # Never forward raw report objects, signed URLs, credentials or OTPs.
        allowed = ('state', 'detail', 'media_done', 'media_total', 'room', 'direction', 'pages', 'account', 'summary')
        with self.lock:
            self.job.update({key: value for key, value in values.items() if key in allowed})
            json_write(self.state_dir / 'current-job.json', self.job)

    def clear_history(self):
        with self.lock:
            self.history = []
            json_write(self.state_dir / 'jobs.json', self.history)
            if not self.active():
                self.job = None
                (self.state_dir / 'current-job.json').unlink(missing_ok=True)

    def start(self, kind, output=None, library=None):
        with self.lock:
            if self.active() or self.auth_busy:
                raise ValueError('已有任务在运行。')
            if kind not in ('export', 'generate', 'verify'):
                raise ValueError('未知任务。')
            root = site.output_path(self.library(library)['path'] if library else output or self.settings['output'])
            if kind == 'export' and not self.token_path.is_file():
                raise ValueError('请先登录或导入会话。')
            # Migration happens inside the worker's exclusive archive lock.
            if kind != 'export' and not (root / '_data/archive.sqlite3').is_file() and not (root / 'archive.sqlite3').is_file():
                raise ValueError('目录中没有备份数据库。')
            ident = self.register(root)
            self.cancel = threading.Event()
            self.job = {'id': secrets.token_hex(8), 'kind': kind, 'library': ident, 'output': str(root), 'state': 'starting', 'detail': '任务准备中', 'started': datetime.now(JST).isoformat()}
            self.worker = threading.Thread(target=self.run_job, args=(kind, root), daemon=True)
            self.worker.start()
            return self.job['id']

    def run_job(self, kind, root):
        try:
            if kind == 'export':
                token = token_from_bytes(self.token_path.read_bytes())
                self.progress(state='authenticating', detail='正在验证会话并读取订阅')
                site.export(token, root, self.progress, self.cancel)
                report = site.read_json(root / '_data/report.json', {})
                self.progress(state='complete' if report.get('accessible_content_complete') else 'partial', detail='导出与独立页面生成完成' if report.get('accessible_content_complete') else '页面已生成，导出存在未完成项，请查看报告')
            elif kind == 'generate':
                result = site.generate(root, self.progress, self.cancel)
                missing = result['summary']['missing']
                self.progress(state='partial' if missing else 'complete', detail=f'页面已生成，{missing} 项附件缺失' if missing else '人物目录与独立页面已重新生成')
            else:
                result = site.verify(root, self.progress, self.cancel)
                self.progress(state='complete' if result['ok'] else 'partial', detail=f'校验完成：{result["media_files"]} 个媒体，{len(result["errors"])} 项异常')
        except Cancelled:
            self.progress(state='cancelled', detail='任务已停止，可更新资源')
        except Exception as exc:
            self.progress(state='failed', detail=safe_error(exc))
        finally:
            with self.lock:
                self.job['finished'] = datetime.now(JST).isoformat()
                self.history.append(dict(self.job))
                self.history = self.history[-15:]
                json_write(self.state_dir / 'jobs.json', self.history)
                json_write(self.state_dir / 'current-job.json', self.job)


def make_server(port=8769, controller=None, bind='127.0.0.1'):
    controller = controller or Controller()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                pass
        def log_message(self, *_):
            pass
        def local(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')
        def send(self, code, data, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)
        def do_HEAD(self):
            self.do_GET()
        def do_GET(self):
            if not self.local():
                return self.send(403, {'error': 'Local host required'})
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            try:
                if path == '/api/status':
                    return self.send(200, controller.status())
                if path == '/api/folders':
                    folder = Path(parse_qs(parsed.query).get('path', [str(Path.home())])[0]).expanduser().resolve()
                    if not folder.is_dir():
                        raise ValueError('文件夹不存在。')
                    dirs = sorted(p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith('.') and not p.is_symlink())
                    return self.send(200, {'path': str(folder), 'parent': str(folder.parent), 'directories': dirs})
                if path in ('/', '/control.js', '/control.css', '/logo.svg'):
                    target = ROOT / 'src/web' / ('index.html' if path == '/' else path[1:])
                    return self.send(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] + '; charset=utf-8')
                if path.startswith('/preview/'):
                    _, _, ident, rel = path.split('/', 3)
                    lib = controller.library(ident)
                    root = Path(lib['path']).resolve()
                    manifest = site.read_json(root / site.MARKER, {})
                    if rel not in manifest.get('files', []) and rel not in ('archive-site.json', '校验报告.json'):
                        return self.send(404, {'error': 'Not a generated file'})
                    target = (root / rel).resolve()
                    if root not in target.parents or not target.is_file():
                        return self.send(404, {'error': 'File not found'})
                    return self.file(target)
                self.send(404, {'error': 'Not found'})
            except (ValueError, OSError, KeyError) as exc:
                self.send(400, {'error': safe_error(exc)})
        def file(self, target):
            size = target.stat().st_size
            begin, end, code = 0, size - 1, 200
            value = self.headers.get('Range')
            if value:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
                if match and (match[1] or match[2]):
                    if match[1]:
                        begin = int(match[1]); end = min(int(match[2]) if match[2] else size - 1, size - 1)
                    else:
                        begin = max(0, size - int(match[2]))
                    code = 206
                if not match or not (match[1] or match[2]) or begin > end or begin >= size:
                    self.send_response(416); self.send_header('Content-Range', f'bytes */{size}');self.send_header('Content-Length', '0');self.end_headers();return
            self.send_response(code)
            self.send_header('Content-Type', 'text/vtt; charset=utf-8' if target.suffix == '.vtt' else mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(max(0, end - begin + 1)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            if code == 206:
                self.send_header('Content-Range', f'bytes {begin}-{end}/{size}')
            self.end_headers()
            if self.command != 'HEAD':
                try:
                    with target.open('rb') as stream:
                        stream.seek(begin); remaining = end - begin + 1
                        while remaining > 0:
                            chunk = stream.read(min(1024 * 1024, remaining))
                            if not chunk: break
                            self.wfile.write(chunk); remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        def do_POST(self):
            if not self.local() or self.headers.get('Origin') not in (None, 'http://' + self.headers.get('Host', '')) or self.headers.get('X-CSRF-Token') != controller.csrf:
                return self.send(403, {'error': 'Local origin and CSRF token required'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 32768:
                    return self.send(413, {'error': 'Input too large'})
                values = json.loads(self.rfile.read(length))
                if not isinstance(values, dict):
                    raise ValueError('请求必须为 JSON 对象。')
                if self.path.startswith('/api/auth/'):
                    result = controller.auth(self.path.rsplit('/', 1)[-1], values, self.server.server_port)
                    return self.send(200, {'ok': True, **(result or {})})
                elif self.path == '/api/settings':
                    root = site.output_path(values['output'])
                    with controller.lock:
                        controller.settings['output'] = str(root)
                        controller.persist_settings()
                elif self.path == '/api/libraries/import':
                    root = site.output_path(values['path'])
                    if not (root / 'archive.sqlite3').is_file() and not (root / '_data/archive.sqlite3').is_file():
                        raise ValueError('目录中没有 Replive 备份数据库。')
                    with controller.lock:
                        controller.register(root)
                elif self.path == '/api/jobs':
                    controller.start(values.get('kind'), values.get('output'), values.get('library'))
                    with controller.lock:
                        return self.send(200, {'ok': True, 'job': dict(controller.job)})
                elif self.path == '/api/jobs/cancel':
                    controller.cancel.set()
                    if controller.active(): controller.progress(detail='正在停止，等待当前网络请求退出…')
                elif self.path == '/api/jobs/history/clear':
                    controller.clear_history()
                elif self.path == '/api/shutdown':
                    controller.cancel.set()
                    controller.sns.cancel()
                    controller.bridge.clear()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    return self.send(404, {'error': 'Not found'})
                self.send(200, {'ok': True})
            except (Exception,) as exc:
                self.send(400, {'error': safe_error(exc)})
    server = ThreadingHTTPServer((bind, port), Handler)
    server.controller = controller
    return server


def serve(port=8769, output=None, resume=False, bind=None):
    os.umask(0o077)
    controller = Controller(output)
    server = make_server(port, controller, bind or os.environ.get('REPLIVE_BIND', '127.0.0.1'))
    print(f'RepArchive 控制台：http://127.0.0.1:{server.server_port}/', flush=True)
    if resume:
        controller.start('export')
    shutting_down = threading.Event()
    def terminate(_signal, _frame):
        shutting_down.set()
        controller.cancel.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.cancel.set()
        controller.bridge.clear()
        server.server_close()
        if controller.worker:
            controller.worker.join(timeout=95 if shutting_down.is_set() else 5)
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    serve(args.port, args.output)
