#!/usr/bin/env python3
"""一次性、可续传的本地聊天备份。基础协议参考 nsy_chat_live；来源见 docs/ATTRIBUTION.md。"""
import argparse
import base64
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import tempfile
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qs, parse_qsl
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parent
sys.path.insert(0, str(SOURCE / 'generated'))
import http_pb2 as pb
import external_pb2 as ext
from subscription_content_pb2 import ChatMessage
from google.protobuf.json_format import MessageToDict

API_HOST = 'https://api.replive.com/'
JST = ZoneInfo('Asia/Tokyo')
STATE = {'state': 'waiting_for_phone', 'detail': '等待手机号登录；尚未导出账号内容。'}
LOCK = threading.Lock()


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_error(exc):
    if isinstance(exc, HTTPError):
        return f'HTTP {exc.code}'
    if isinstance(exc, (URLError, TimeoutError, OSError)):
        return f'网络或文件错误 ({type(exc).__name__})'
    # 不输出请求 URL、请求体、认证响应或登录会话。
    return str(exc) if isinstance(exc, ValueError) else type(exc).__name__


def update(**values):
    with LOCK:
        STATE.update(values)
        snapshot = dict(STATE)
    json_write(ROOT / 'exports' / 'progress.json', snapshot)


def to_dict(message):
    return MessageToDict(message, preserving_proto_field_name=True)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def media_identity(url):
    """只忽略已确认的腾讯云点播鉴权参数，保留试看和转码等内容参数。"""
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.hostname == 'vod.replive.com' and {'t', 'us', 'sign'} <= {key for key, _ in query}:
        query = [(key, value) for key, value in query if key not in ('t', 'us', 'sign')]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return url


def token_from_bytes(data):
    """接受单个 refresh_token 或 RefreshAccessToken 请求体，不猜测删除首字符。"""
    token_pattern = r'[A-Za-z0-9._~+/-]{16,}={0,2}'
    # 先识别 protobuf；长度字节可能恰好是字母，不能把它误当 token 首字符。
    try:
        message = pb.RefreshAccessTokenRequest.FromString(data)
        if re.fullmatch(token_pattern, message.refresh_token):
            return message.refresh_token
    except Exception:
        pass
    try:
        text = data.decode('utf-8').strip()
    except UnicodeDecodeError:
        text = ''
    if text.startswith('{'):
        try:
            obj = json.loads(text)
            token = obj.get('refresh_token', obj.get('refreshToken', ''))
            if isinstance(token, str) and token:
                return token
            entries = obj.get('log', {}).get('entries', [])
            # HAR 仅处理 Replive 的指定请求，不保存或显示其他网站的条目。
            for entry in reversed(entries):
                request = entry.get('request', {})
                url = urlsplit(request.get('url', ''))
                if url.hostname != 'api.replive.com' or not url.path.endswith('/RefreshAccessToken'):
                    continue
                post = request.get('postData', {})
                body = post.get('text', '')
                if post.get('encoding') == 'base64' or post.get('_encoding') == 'base64':
                    return token_from_bytes(base64.b64decode(body))
                if body:
                    return token_from_bytes(body.encode())
                encoded = parse_qs(url.query).get('message', [])
                if encoded:
                    return token_from_bytes(base64.b64decode(encoded[0]))
        except (ValueError, KeyError, TypeError):
            pass
    match = re.search(r'(?:refresh_token|refreshToken)\s*[:=]\s*[\"\']?(' + token_pattern + ')', text)
    if match:
        return match.group(1)
    if re.fullmatch(token_pattern, text):
        return text
    raise ValueError('无法识别会话。请使用单个 refresh_token，或指定请求的原始 protobuf 请求体。')


class Cancelled(BaseException):
    """Stop work without treating cancellation as a recoverable per-item failure."""


def check_cancel(event):
    if event is not None and event.is_set():
        raise Cancelled()


class API:
    def __init__(self, refresh_token, cancel=None):
        self.refresh_token = refresh_token
        self.access_token = ''
        self.expires = 0
        self.cancel = cancel

    def rpc(self, method, request, response_type, raw_path=None, auth=True):
        check_cancel(self.cancel)
        if auth and self.expires < time.time() + 180:
            refreshed = self.rpc('user.v1.UserService/RefreshAccessToken',
                                 pb.RefreshAccessTokenRequest(refresh_token=self.refresh_token),
                                 pb.RefreshAccessTokenResponse, auth=False)
            if not refreshed.accessToken:
                raise ValueError('服务没有返回有效的访问会话。')
            self.access_token = refreshed.accessToken
            self.expires = refreshed.accessTokenExpireTime.seconds
        data = request.SerializeToString()
        headers = {'Content-Type': 'application/proto', 'Accept-Encoding': 'gzip',
                   'Accept': 'application/json', 'User-Agent': 'v4.8.1 23116PN5BC Android 12',
                   'X-Replive-Platform': 'android'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.access_token
            query = urlencode({'encoding': 'proto', 'base64': '1', 'message': base64.b64encode(data).decode()})
            req = Request(API_HOST + method + '?' + query, headers=headers)
        else:
            req = Request(API_HOST + method, data=data, headers=headers)
        for attempt in range(4):
            check_cancel(self.cancel)
            try:
                with urlopen(req, timeout=60) as response:
                    body = response.read()
                if body.startswith(b'\x1f\x8b'):
                    body = gzip.decompress(body)
                if raw_path is not None:
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    raw_path.write_bytes(body)
                return response_type.FromString(body)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
            except (URLError, TimeoutError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)


class Exporter:
    def __init__(self, api, output, progress=None, cancel=None):
        self.api = api
        self.progress = progress or update
        self.cancel = cancel
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.output / 'archive.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS messages (room TEXT, id TEXT, timestamp INTEGER, json TEXT, PRIMARY KEY(room,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS assets (url TEXT PRIMARY KEY, path TEXT, size INTEGER, sha256 TEXT)')
        self.report = {'scope': '可访问的 Fandom Chat 文字、图片、短视频及关注资料图片',
                       'source': 'huangwg2529/nsy_chat_live@69c6d519cb0b394f839296d2af51f5e954078b81',
                       'started_at': datetime.now(JST).isoformat(), 'rooms': [], 'errors': [],
                       'limitations': ['已覆盖聊天和可读取的提问/回答视频列表；独立历史直播回放及未公开的其他内容尚无法确认。']}

    def save_page(self, room, messages):
        for message in messages:
            # 旧响应模型保留 unknown fields；用新模型读取封面、提问等已确认字段。
            message = ChatMessage.FromString(message.SerializeToString())
            if not message.chat_message_id:
                raise ValueError('消息缺少 ID；无法保证去重和完整性。')
            value = to_dict(message)
            value['time_jst'] = datetime.fromtimestamp(message.timestamp.seconds, JST).isoformat()
            self.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?)',
                            (room, message.chat_message_id, message.timestamp.seconds, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def room_sync(self, room):
        room_id = room.chat_room_id
        raw = self.output / 'raw' / 'chat' / room_id
        result = {'room_id': room_id, 'name': room.user_profile.display_name, 'history_complete': False, 'directions': {}}
        self.report['rooms'].append(result)
        def page(cursor, backward, index, size=100):
            check_cancel(self.cancel)
            request = pb.ListChatMessagesRequest(user_id=room.user_id, chat_room_id=room_id,
                        max_page_size=size, cursor_chat_message_id=cursor, backward=backward)
            return self.api.rpc('user.v1.ChatService/ListChatMessages', request,
                               pb.ListChatMessagesResponse, raw / f'{"older" if backward else "newer"}-{index:05d}.pb')
        initial = page('', False, 0, 1)
        if not initial.messages:
            # 按原项目兼容策略再尝试另一方向；空房间也需要保留可见状态。
            initial = page('', True, 0)
        if not initial.messages:
            result['status'] = 'empty_or_inaccessible'
            self.report['errors'].append({'room_id': room_id, 'error': '未取得初始消息，空房间与无访问权限尚无法区分。'})
            return
        self.save_page(room_id, initial.messages)
        anchor = initial.next_page_cursor_message_id or initial.messages[-1].chat_message_id
        for backward in (True, False):
            direction = 'older' if backward else 'newer'
            cursor = anchor
            seen = {cursor}
            for index in range(1, 10001):
                response = page(cursor, backward, index)
                self.save_page(room_id, response.messages)
                self.progress(state='exporting', detail=f'正在读取 {result["name"]} 的历史消息', room=result['name'], direction=direction, pages=index)
                if not response.messages or not response.next_page_cursor_message_id:
                    result['directions'][direction] = {'complete': True, 'pages': index,
                                                      'end': 'empty_page' if not response.messages else 'empty_cursor'}
                    break
                next_cursor = response.next_page_cursor_message_id
                if next_cursor in seen:
                    result['directions'][direction] = {'complete': False, 'pages': index, 'end': 'repeated_cursor'}
                    self.report['errors'].append({'room_id': room_id, 'error': f'{direction} 游标重复，历史完整性未确认。'})
                    break
                seen.add(next_cursor)
                cursor = next_cursor
                time.sleep(0.2)
            else:
                self.report['errors'].append({'room_id': room_id, 'error': f'{direction} 超出安全页数上限。'})
        result['history_complete'] = all(result['directions'].get(d, {}).get('complete') for d in ('older', 'newer'))
        row = self.db.execute('SELECT count(*),min(timestamp),max(timestamp) FROM messages WHERE room=?', (room_id,)).fetchone()
        result.update(message_count=row[0], earliest_jst=datetime.fromtimestamp(row[1], JST).isoformat(),
                      latest_jst=datetime.fromtimestamp(row[2], JST).isoformat())

    def profiles(self):
        values = []
        for name, request_type, response_type, field in [
                ('ListMyOshis', ext.ListMyOshisRequest, ext.ListMyOshisResponse, 'oshis'),
                ('ListFollowings', ext.ListFollowingsRequest, ext.ListFollowingsResponse, 'follow_targets')]:
            cursor = ''
            seen = {cursor}
            all_items = []
            try:
                for index in range(10000):
                    check_cancel(self.cancel)
                    request = request_type(max_page_size=100, page_token=cursor)
                    if name == 'ListFollowings':
                        request.user_id = 'me'
                        request.type = ext.FOLLOW_TARGET_TYPE_OSHI
                    response = self.api.rpc('user.v1.UserService/' + name, request, response_type,
                                            self.output / 'raw' / f'{name}-{index:05d}.pb')
                    all_items.extend(to_dict(x) for x in getattr(response, field))
                    if not response.next_page_token:
                        break
                    if response.next_page_token in seen:
                        raise ValueError(name + ' 分页游标重复。')
                    cursor = response.next_page_token
                    seen.add(cursor)
                    time.sleep(0.2)
                else:
                    raise ValueError(name + ' 超出安全页数上限。')
            except Exception as exc:
                self.report['errors'].append({'stage': name, 'error': safe_error(exc)})
            json_write(self.output / 'metadata' / (name + '.json'), all_items)
            values.extend(all_items)
        return values

    def download(self, url, path):
        check_cancel(self.cancel)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname:
            raise ValueError('媒体地址不是有效的 HTTPS 地址。')
        existing = self.db.execute('SELECT path,size,sha256 FROM assets WHERE url=?', (url,)).fetchone()
        if existing:
            old = self.output / existing[0]
            if old.is_file() and old.stat().st_size == existing[1] and file_hash(old) == existing[2]:
                return existing[0]
        # 同一不可变 VOD 文件每次读取会得到不同的临时签名；校验已有文件后复用。
        relpath = path.relative_to(self.output).as_posix()
        for old_url, old_path, size, digest in self.db.execute('SELECT url,path,size,sha256 FROM assets WHERE path=?', (relpath,)).fetchall():
            old = self.output / old_path
            if media_identity(old_url) == media_identity(url) and old.is_file() and old.stat().st_size == size and file_hash(old) == digest:
                self.db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?)', (url, old_path, size, digest))
                self.db.commit()
                return old_path
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + '.part')
        for attempt in range(4):
            check_cancel(self.cancel)
            try:
                digest = hashlib.sha256()
                count = 0
                with urlopen(Request(url, headers={'User-Agent': 'RepArchive'}), timeout=90) as response, partial.open('wb') as output:
                    content_type = response.headers.get('Content-Type', '')
                    if 'text/html' in content_type or 'application/json' in content_type:
                        raise ValueError('媒体地址返回了错误页面。')
                    length = response.headers.get('Content-Length')
                    while chunk := response.read(1024 * 1024):
                        check_cancel(self.cancel)
                        output.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                if not count or (length and int(length) != count):
                    raise ValueError('媒体文件为空或传输长度不完整。')
                partial.replace(path)
                relpath = path.relative_to(self.output).as_posix()
                self.db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?)', (url, relpath, count, digest.hexdigest()))
                self.db.commit()
                return relpath
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def media(self, profiles, rooms):
        urls = {}
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str) and key.endswith('_url') and any(t in key for t in ('image', 'avatar', 'background')) and item:
                        extn = Path(urlsplit(item).path).suffix or '.jpg'
                        if not re.fullmatch(r'\.[a-zA-Z0-9]{1,5}', extn):
                            extn = '.bin'
                        urls.setdefault(item, self.output / 'media' / 'profiles' / (hashlib.sha256(item.encode()).hexdigest()[:24] + extn))
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(profiles)
        walk([to_dict(r) for r in rooms])
        for room, message_id, timestamp, raw_json in self.db.execute('SELECT room,id,timestamp,json FROM messages').fetchall():
            message = json.loads(raw_json)
            date = datetime.fromtimestamp(timestamp, JST)
            for key, suffix in [('image_url', '.jpg'), ('video_url', '.mp4'),
                                ('video_thumbnail_jpeg_url', '.jpg'), ('video_thumbnail_gif_url', '.gif')]:
                url = message.get(key)
                if url:
                    if key != 'video_url':
                        extn = Path(urlsplit(url).path).suffix
                        if re.fullmatch(r'\.(?:jpe?g|png|gif|webp|avif)', extn, re.I):
                            suffix = extn
                    filename = date.strftime('%Y-%m-%d_') + message_id + '_' + key.removesuffix('_url') + suffix
                    urls.setdefault(url, self.output / 'media' / room / date.strftime('%Y/%m') / filename)
        for index, (url, path) in enumerate(urls.items(), 1):
            self.progress(state='downloading', detail='正在保存图片与短视频', media_done=index - 1, media_total=len(urls))
            try:
                self.download(url, path)
            except Exception as exc:
                self.report['errors'].append({'stage': 'media', 'url': url, 'error': safe_error(exc)})
        count, size = self.db.execute('SELECT count(*),coalesce(sum(size),0) FROM assets').fetchone()
        self.report.update(media_expected=len(urls), media_downloaded=count, media_bytes=size)
        self.progress(media_done=len(urls), media_total=len(urls))

    def extra(self):
        from extra_content import ExtraContent
        ExtraContent(self, self.progress, to_dict, json_write, safe_error).run()

    def readable(self):
        links = []
        for room in self.report['rooms']:
            room_id = room['room_id']
            messages = [json.loads(x[0]) for x in self.db.execute('SELECT json FROM messages WHERE room=? ORDER BY timestamp,id', (room_id,))]
            target = self.output / 'chats' / room_id
            target.mkdir(parents=True, exist_ok=True)
            rows = []
            for message in messages:
                items = []
                for key in ('image_url', 'video_url'):
                    if url := message.get(key):
                        record = self.db.execute('SELECT path FROM assets WHERE url=?', (url,)).fetchone()
                        if record:
                            message[key.replace('_url', '_local_path')] = record[0]
                            source = html.escape('../../' + record[0], quote=True)
                            if key == 'image_url':
                                items.append(f'<img loading="lazy" src="{source}">')
                            else:
                                poster = self.db.execute('SELECT path FROM assets WHERE url=?', (message.get('video_thumbnail_jpeg_url', ''),)).fetchone()
                                attribute = ' poster="' + html.escape('../../' + poster[0], quote=True) + '"' if poster else ''
                                items.append(f'<video controls preload="none" src="{source}"{attribute}></video>')
                        else:
                            items.append('<p class="missing">附件未下载，见缺失清单。</p>')
                question = '<p>提问：' + html.escape(message['card_content']) + '</p>' if message.get('card_content') else ''
                rows.append('<article><time>' + html.escape(message['time_jst']) + '</time><p>' + html.escape(message.get('content', '')) + '</p>' + question + ''.join(items) + '</article>')
            (target / 'messages.jsonl').write_text(''.join(json.dumps(m, ensure_ascii=False) + '\n' for m in messages), encoding='utf-8')
            document = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + html.escape(room['name']) + '</title><style>body{max-width:800px;margin:24px auto;padding:16px;font-family:system-ui;background:#f5f5f5}article{padding:18px;background:white;margin:16px 0;border-radius:10px}time{color:#666;font-size:13px}p{white-space:pre-wrap;overflow-wrap:anywhere}img,video{max-width:100%}.missing{color:#b43}</style><h1>' + html.escape(room['name']) + '</h1>' + ''.join(rows)
            (target / 'index.html').write_text(document, encoding='utf-8')
            links.append(f'<li><a href="chats/{html.escape(room_id, quote=True)}/index.html">{html.escape(room["name"])}</a> · {len(messages)} 条消息</li>')
        if 'extra_content' in self.report:
            links.append('<li><a href="answers/index.html">独立回答视频</a> · ' + str(self.report['extra_content']['video_count']) + ' 个</li>')
        state = '可读取的聊天与回答备份已完成' if not self.report['errors'] else '备份含未完成项目，请查看 report.json'
        (self.output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>RepArchive</title><h1>' + state + '</h1><ul>' + ''.join(links) + '</ul>', encoding='utf-8')

    def run(self):
        try:
            user = self.api.rpc('user.v1.UserService/GetUserPrivate', ext.GetUserPrivateRequest(), ext.GetUserPrivateResponse).user
            if not user.user_id:
                raise ValueError('未能确认当前账号身份，请检查登录会话。')
            identity_path = self.output / 'account_identity.json'
            if identity_path.exists() and json.loads(identity_path.read_text())['user_id'] != user.user_id:
                raise ValueError('当前登录会话属于另一个账号；请使用原账号或新的输出目录。')
            identity = {'user_id': user.user_id, 'unique_id': user.unique_id, 'display_name': user.display_name}
            json_write(identity_path, identity)
            self.progress(account=user.display_name)
            rooms_response = self.api.rpc('user.v1.ChatService/ListChatRooms', pb.ListChatRoomsRequest(max_page_size=32), pb.ListChatRoomsResponse, self.output / 'raw' / 'rooms.pb')
            rooms = rooms_response.chat_rooms
            json_write(self.output / 'metadata' / 'rooms.json', [to_dict(r) for r in rooms])
            self.report['room_count'] = len(rooms)
            if not rooms:
                self.report['errors'].append({'stage': 'rooms', 'error': '未取得订阅聊天房间，请核对是否为原账号及当前订阅状态。'})
            if len(rooms) >= 32:
                self.report['errors'].append({'stage': 'rooms', 'error': '房间数触及参考协议上限 32；完整订阅列表尚未确认。'})
            for room in rooms:
                try:
                    self.room_sync(room)
                except Exception as exc:
                    self.report['errors'].append({'room_id': room.chat_room_id, 'error': safe_error(exc)})
                json_write(self.output / 'report.json', self.report)
            profiles = self.profiles()
            self.media(profiles, rooms)
            try:
                self.extra()
            except Exception as exc:
                self.report['errors'].append({'stage': 'extra_content', 'error': safe_error(exc)})
            self.readable()
            manifest = [dict(zip(('url', 'path', 'size', 'sha256'), row)) for row in self.db.execute('SELECT url,path,size,sha256 FROM assets ORDER BY path')]
            json_write(self.output / 'media_manifest.json', manifest)
            self.report['finished_at'] = datetime.now(JST).isoformat()
            self.report['chat_snapshot_complete'] = not any(not str(e.get('stage', '')).startswith(('extra', 'video', 'subscription')) for e in self.report['errors'])
            self.report['accessible_content_complete'] = not self.report['errors'] and self.report.get('extra_content', {}).get('complete', False)
            self.report['all_subscribed_content_complete'] = False
            count, size = self.db.execute('SELECT count(*),coalesce(sum(size),0) FROM assets').fetchone()
            files, file_bytes = self.db.execute('SELECT count(*),coalesce(sum(size),0) FROM (SELECT path,max(size) AS size FROM assets GROUP BY path)').fetchone()
            self.report.update(total_media_downloaded=files, total_media_bytes=file_bytes, media_url_count=count)
            json_write(self.output / 'report.json', self.report)
            self.progress(state='export_complete' if self.report['accessible_content_complete'] else 'partial',
                   detail='可读取的聊天与独立回答已保存。查看 report.json 核对完整性。', output=str(self.output), report=self.report)
        finally:
            self.db.close()


def run_export(token, output):
    try:
        update(state='authenticating', detail='正在验证 Replive 登录会话')
        Exporter(API(token), output).run()
    except Exception as exc:
        update(state='failed', detail=safe_error(exc))


def save_session(token):
    secret = ROOT / '.private' / 'refresh_token.txt'
    secret.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secret.parent, 0o700)
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(token)
    os.chmod(secret, 0o600)


PAGE = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>RepArchive</title>
<style>body{max-width:720px;margin:36px auto;padding:20px;font:16px/1.6 system-ui;background:#f5f7fa;color:#243042}section{background:white;padding:24px;margin:20px 0;border-radius:12px}label{display:block;margin:10px 0}input,textarea{box-sizing:border-box;width:100%;padding:12px;border:1px solid #cad1dd;border-radius:6px;font:16px system-ui}textarea{height:110px}button{padding:12px 20px;margin-top:10px;background:#2549d7;color:white;border:0;border-radius:6px;font:16px system-ui}button:disabled{opacity:.5}small{color:#555}.row{display:grid;grid-template-columns:150px 1fr;gap:16px}#status{white-space:pre-wrap;padding:18px;background:#e9eef8;border-radius:8px}.error{color:#ac2331}details{margin:24px 0}</style>
<h1>RepArchive</h1><p>手机号登录，导出聊天文字、图片和短视频。</p>
<section><h2>1. 获取短信验证码</h2><form method="post" action="/phone/send"><input type="hidden" name="csrf" value="__CSRF__">
<div class="row"><label>国际区号<input name="country_code" value="81" inputmode="numeric" autocomplete="tel-country-code" required></label>
<label>手机号<input name="phone" type="tel" autocomplete="tel-national" placeholder="原账号绑定的手机号" required></label></div>
<small>日本填写 81，中国填写 86；可以按当地习惯输入号码，例如日本号码以 090 开头。</small><br><button type="submit">发送验证码</button></form></section>
<section><h2>2. 登录并开始导出</h2><form method="post" action="/phone/login"><input type="hidden" name="csrf" value="__CSRF__">
<label>短信验证码<input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="填写收到的验证码" pattern="[0-9]{4,8}" required></label>
<button type="submit">登录并开始导出</button></form></section>
<h2>当前状态</h2><div id="status" role="status">等待手机号登录，尚未导出账号内容。</div>
<details><summary>已有登录会话</summary><form method="post" action="/start"><input type="hidden" name="csrf" value="__CSRF__"><label>refresh_token<textarea name="token" autocomplete="off" spellcheck="false" required></textarea></label><button type="submit">使用会话导出</button></form></details>
<script>
let submitting=false;
async function refresh(){try{let r=await fetch('/status');let s=await r.json();let text=s.detail;if(s.media_total!==undefined)text+='\\n附件：'+s.media_done+' / '+s.media_total;if(s.output)text+='\\n保存位置：'+s.output;document.querySelector('#status').textContent=text;document.querySelector('#status').className=['failed','login_error'].includes(s.state)?'error':''}catch{document.querySelector('#status').textContent='本地服务已关闭。双击“start.command”可重新打开。'}}
for(const form of document.forms){form.addEventListener('submit',async event=>{event.preventDefault();if(submitting)return;submitting=true;const button=form.querySelector('button');button.disabled=true;try{let response=await fetch(form.action,{method:'POST',body:new URLSearchParams(new FormData(form))});let result=await response.json();if(!response.ok)throw new Error(result.error);form.querySelectorAll('input[name="code"],textarea').forEach(x=>x.value='');await refresh()}catch(error){document.querySelector('#status').textContent=error.message;document.querySelector('#status').className='error'}finally{button.disabled=false;submitting=false}})}
refresh();setInterval(()=>{if(!submitting)refresh()},3000);
</script>'''


def make_server(port, output, phone_client=None, export_fn=None):
    from phone_login import PhoneLogin
    phone_client = phone_client or PhoneLogin()
    export_fn = export_fn or run_export
    csrf = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, body, content_type):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_address[1]}':
                self.send(403, b'Local host required', 'text/plain')
                return
            if self.path == '/':
                self.send(200, PAGE.replace('__CSRF__', csrf).encode(), 'text/html; charset=utf-8')
            elif self.path == '/status':
                with LOCK:
                    body = json.dumps(STATE, ensure_ascii=False).encode()
                self.send(200, body, 'application/json; charset=utf-8')
            else:
                self.send(404, b'Not found', 'text/plain')

        def do_POST(self):
            if self.path not in ('/start', '/phone/send', '/phone/login'):
                self.send(404, b'Not found', 'text/plain')
                return
            expected = f'127.0.0.1:{self.server.server_address[1]}'
            if self.headers.get('Host') != expected or self.headers.get('Origin') not in (None, f'http://{expected}'):
                self.send(403, b'Local origin required', 'text/plain')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = -1
            if length < 0 or length > 16384:
                self.send(413, b'Input too large', 'text/plain')
                return
            values = parse_qs(self.rfile.read(length).decode())
            if values.get('csrf') != [csrf]:
                self.send(403, b'Invalid form', 'text/plain')
                return
            try:
                previous_state = None
                with LOCK:
                    if STATE['state'] in ('authenticating', 'exporting', 'downloading', 'sending_code', 'logging_in'):
                        raise ValueError('登录或导出正在进行，请勿重复操作。')
                    previous_state = dict(STATE)
                    STATE.update(state='sending_code' if self.path == '/phone/send' else 'logging_in', detail='正在请求 Replive 登录服务')
                if self.path == '/phone/send':
                    phone_client.send_code(values.get('country_code', [''])[0], values.get('phone', [''])[0])
                    update(state='code_sent', detail='短信验证码已发送。填写验证码后，登录并开始导出。')
                else:
                    if self.path == '/phone/login':
                        result = phone_client.login(values.get('code', [''])[0])
                        identity_path = Path(output) / 'account_identity.json'
                        if identity_path.exists() and json.loads(identity_path.read_text())['user_id'] != result.user.user_id:
                            raise ValueError('此手机号属于另一个账号，请使用原账号绑定的手机号。')
                        token = result.refresh_token
                    else:
                        token = token_from_bytes(values.get('token', [''])[0].encode())
                    save_session(token)
                    update(state='authenticating', detail='登录已成功，正在读取订阅列表')
                    threading.Thread(target=export_fn, args=(token, output), daemon=True).start()
                self.send(200, b'{"ok":true}', 'application/json')
            except Exception as exc:
                if previous_state is not None:
                    update(state='login_error', detail=safe_error(exc))
                self.send(400, json.dumps({'error': safe_error(exc)}, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(port, output, resume=False):
    update(state='waiting_for_phone', detail='等待手机号登录；尚未导出账号内容。')
    print(f'本地接收页：http://127.0.0.1:{port}/', flush=True)
    server = make_server(port, output)
    if resume:
        token = token_from_bytes((ROOT / '.private/refresh_token.txt').read_bytes())
        threading.Thread(target=run_export, args=(token, output), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        update(state='stopped', detail='本地服务已关闭；已保存文件保留。')
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'exports' / 'account')
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--check-phone-api', action='store_true', help='检验登录路由，不发送短信')
    parser.add_argument('--resume', action='store_true', help='在本地页面显示进度，并用已保存会话续传')
    parser.add_argument('--generate', action='store_true', help='离线整理目录并生成独立页面')
    parser.add_argument('--verify', action='store_true', help='离线校验已导出的媒体')
    args = parser.parse_args()
    args.output = args.output.expanduser().resolve()
    os.umask(0o077)
    if args.generate or args.verify:
        from archive_site import generate, verify
        result = verify(args.output) if args.verify else generate(args.output)
        print(json.dumps(result.get('summary', result), ensure_ascii=False))
        return 0 if result.get('ok', True) else 1
    if args.check_phone_api:
        from phone_login import PhoneLogin
        print(json.dumps(PhoneLogin().preflight(), ensure_ascii=False))
        return 0
    if args.token_file:
        from archive_site import export
        export(token_from_bytes(args.token_file.read_bytes()), args.output, update)
        report = json.loads((args.output / '_data/report.json').read_text())
        return 0 if report.get('accessible_content_complete') else 1
    from webui import serve as serve_control
    serve_control(args.port, args.output, resume=args.resume)


if __name__ == '__main__':
    sys.exit(main())
