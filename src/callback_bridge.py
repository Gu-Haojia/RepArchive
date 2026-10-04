"""Automatic login callback receiver for the running local controller."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import threading
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

SCHEMES = ('com.googleusercontent.apps.1046086565638-ndihf1c3r8hjli7l1robniulso85j5k5', 'replive-user-auth')


class CallbackBridge:
    def __init__(self, state_dir):
        self.directory = Path(state_dir)
        self.config = self.directory / 'oauth-callback.json'
        self.marker = self.directory / 'oauth-helper.json'
        self.lock = threading.RLock()
        self.timer = None
        self.attempt = None

    def status(self):
        return {'supported': sys.platform in ('darwin', 'win32', 'linux') and not Path('/.dockerenv').exists(),
                'enabled': self.config.is_file()}

    def activate(self, port, csrf, attempt):
        if not self.status()['supported']:
            return False
        try:
            installed = json.loads(self.marker.read_text())
            ready = (installed.get('platform') == sys.platform and Path(installed.get('path', '')).exists()
                     and installed.get('runtime') == str(Path(sys.executable).absolute())
                     and installed.get('script') == str(Path(__file__).resolve())
                     and installed.get('state') == str(self.config))
        except (OSError, ValueError, TypeError):
            ready = False
        if not ready:
            self.install()
        self.bind(port, csrf, attempt)
        return True

    def bind(self, port, csrf, attempt):
        with self.lock:
            self.clear()
            self.write_private(self.config, {'url': f'http://127.0.0.1:{port}/api/auth/sns-finish', 'csrf': csrf, 'attempt': attempt})
            self.attempt = attempt
            def expire():
                with self.lock:
                    if self.attempt == attempt:
                        self.clear()
            self.timer = threading.Timer(600, expire)
            self.timer.daemon = True
            self.timer.start()

    def clear(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self.attempt = None
            self.config.unlink(missing_ok=True)

    @staticmethod
    def write_private(path, value):
        # These files contain only the temporary local callback capability.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
        os.chmod(path, 0o600)

    def install(self, register=True):
        if not self.status()['supported']:
            raise ValueError('此环境请使用手动登录回调。')
        executable, script = str(Path(sys.executable).absolute()), str(Path(__file__).resolve())
        args = [executable, script, '--state', str(self.config)]
        if sys.platform == 'darwin':
            # Built-in AppleScript application receives the URL as an Apple Event.
            bundle = self.directory / 'RepArchive Login.app'
            source = self.directory / 'oauth-handler.applescript'
            def literal(value):
                return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
            command = ' & " " & '.join('quoted form of ' + literal(a) for a in args)
            source.write_text('on open location callbackURL\n  do shell script ' + command + ' & " --callback " & quoted form of callbackURL\nend open location\n')
            result = subprocess.run(['/usr/bin/osacompile', '-o', str(bundle), str(source)], capture_output=True, timeout=30)
            source.unlink(missing_ok=True)
            if result.returncode:
                raise ValueError('登录回调应用生成失败。')
            plist = bundle / 'Contents/Info.plist'
            info = plistlib.loads(plist.read_bytes())
            info.update(CFBundleIdentifier='local.reparchive.login', CFBundleName='RepArchive Login',
                        CFBundleURLTypes=[{'CFBundleURLName': 'RepArchive Login', 'CFBundleURLSchemes': list(SCHEMES)}])
            plist.write_bytes(plistlib.dumps(info))
            if register:
                tool = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
                subprocess.run([tool, '-f', str(bundle)], check=True, capture_output=True, timeout=15)
        elif sys.platform == 'win32':
            bundle = self.directory
            if register:
                import winreg
                for scheme in SCHEMES:
                    base = 'Software\\Classes\\' + scheme
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
                        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, 'URL:RepArchive Login')
                        winreg.SetValueEx(key, 'URL Protocol', 0, winreg.REG_SZ, '')
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + '\\shell\\open\\command') as key:
                        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, subprocess.list2cmdline(args) + ' --callback "%1"')
        else:
            if not shutil.which('xdg-mime'):
                raise ValueError('缺少 xdg-mime，请使用手动登录回调。')
            bundle = self.directory / 'reparchive-login.desktop'
            def desktop_arg(value):
                return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
            bundle.write_text('[Desktop Entry]\nType=Application\nName=RepArchive Login\nNoDisplay=true\nTerminal=false\nExec=' +
                              ' '.join(map(desktop_arg, args)) + ' --callback %u\nMimeType=' + ''.join('x-scheme-handler/'+s+';' for s in SCHEMES) + '\n')
            if register:
                applications = Path(os.environ.get('XDG_DATA_HOME', Path.home()/'.local/share'))/'applications'
                applications.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bundle, applications / bundle.name)
                for scheme in SCHEMES:
                    subprocess.run(['xdg-mime', 'default', bundle.name, 'x-scheme-handler/'+scheme], check=True, capture_output=True, timeout=15)
        if register:
            self.write_private(self.marker, {'platform': sys.platform, 'path': str(bundle), 'runtime': executable,
                                             'script': script, 'state': str(self.config)})
        return bundle


def forward(config, callback):
    parsed = urlsplit(callback)
    if parsed.scheme not in SCHEMES or len(callback) > 16384:
        raise ValueError('回调地址格式不正确。')
    values = json.loads(Path(config).read_text())
    target = urlsplit(values['url'])
    if target.scheme != 'http' or target.hostname != '127.0.0.1' or target.path != '/api/auth/sns-finish' or target.username or target.password or target.query or target.fragment:
        raise ValueError('回调控制台地址不正确。')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *_):
            raise ValueError('回调控制台地址不正确。')
    request = Request(values['url'], data=json.dumps({'attempt': values['attempt'], 'callback': callback}).encode(),
                      headers={'Content-Type': 'application/json', 'X-CSRF-Token': values['csrf']})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=65) as response:
        if not json.loads(response.read(65536)).get('ok'):
            raise ValueError('登录回调未完成。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--callback', required=True)
    arguments = parser.parse_args()
    try:
        forward(arguments.state, arguments.callback)
    except Exception:
        # Never write authorization codes or callback URLs to OS logs.
        print('RepArchive: 登录回调未完成，请返回控制台重试。', file=sys.stderr)
        sys.exit(1)
