#!/usr/bin/env python3
"""Install project-local runtime, generate protocol bindings, then launch controller."""
if __name__ == '__main__':
    print('正在启动 RepArchive，请稍候...', flush=True)

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen, build_opener, ProxyHandler
import webbrowser

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parent


def setup():
    if sys.version_info < (3, 10):
        raise SystemExit('需要 Python 3.10 或更新版本。')
    runtime = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not runtime.is_file():
        print('正在创建项目运行环境，请稍候...', flush=True)
        subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
    requirements = (ROOT / 'requirements.txt').read_bytes()
    stamp = ROOT / '.venv/replive-requirements.sha256'
    digest = hashlib.sha256(requirements).hexdigest()
    if not stamp.exists() or stamp.read_text(encoding='ascii') != digest:
        print('正在安装或更新项目依赖，请稍候...', flush=True)
        subprocess.run([str(runtime), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
        stamp.write_text(digest, encoding='ascii')
    generated = SOURCE / 'generated'
    generated.mkdir(exist_ok=True)
    sources = sorted((SOURCE / 'protocols').glob('*.proto'))
    subprocess.run([str(runtime), '-m', 'grpc_tools.protoc', '-I', str(SOURCE / 'protocols'), '--python_out=' + str(generated), *map(str, sources)], check=True)
    return runtime


def running(url):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(url + 'api/status', timeout=1) as response:
            return json.load(response).get('application') in ('reparchive-controller', 'replive-controller')
    except (OSError, ValueError, URLError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup-only', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8769)
    args = parser.parse_args()
    url = f'http://127.0.0.1:{args.port}/'
    if not args.setup_only and running(url):
        print('控制台已运行：' + url)
        if not args.no_browser: webbrowser.open(url)
        return 0
    runtime = setup()
    if args.setup_only:
        print('项目运行环境已准备好。')
        return 0
    if not args.no_browser:
        def open_when_ready():
            for _ in range(30):
                if running(url):
                    webbrowser.open(url);return
                time.sleep(.5)
        threading.Thread(target=open_when_ready, daemon=True).start()
    # Parent remains around for the browser readiness thread. Forward Ctrl+C.
    process = subprocess.Popen([str(runtime), str(SOURCE / 'webui.py'), '--port', str(args.port)], cwd=ROOT)
    try:
        return process.wait()
    except KeyboardInterrupt:
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            return process.wait()


if __name__ == '__main__':
    sys.exit(main())
