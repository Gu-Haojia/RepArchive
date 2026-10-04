"""Portable archive output. No controller, authentication or network dependency."""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from functools import wraps
from datetime import datetime
from urllib.parse import urlsplit

from export_replive import ROOT, JST, API, Exporter, check_cancel, file_hash, json_write

LEGACY = ('archive.sqlite3', 'archive.sqlite3-wal', 'archive.sqlite3-shm', 'account_identity.json',
          'metadata', 'raw', 'media', 'chats', 'answers', 'report.json', 'media_manifest.json', 'index.html')
MARKER = 'archive-site.json'
_held = threading.local()


@contextmanager
def archive_lock(root):
    root = output_path(root)
    if root.exists() and not any((root / p).exists() for p in ('archive.sqlite3', '_data/archive.sqlite3', '.layout-migration.json')):
        if any(p.name not in ('_data', '.archive.lock') for p in root.iterdir()):
            raise ValueError('请使用空目录或已有 Replive 备份目录。')
    root.mkdir(parents=True, exist_ok=True)
    held = getattr(_held, 'roots', set())
    if root in held:
        yield
        return
    file = (root / '.archive.lock').open('a+b')
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                file.seek(0); file.write(b'0'); file.flush(); file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('另一个进程正在操作此备份目录，请等待该任务结束。')
        _held.roots = held | {root}
        try:
            yield
        finally:
            _held.roots = held
    finally:
        file.close()  # OS releases the lock even after a crash.


def locked(function):
    @wraps(function)
    def run(root, *args, **kwargs):
        with archive_lock(root):
            return function(root, *args, **kwargs)
    return run


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def output_path(value):
    path = Path(value).expanduser().resolve()
    if path == ROOT or ROOT in path.parents and path.parts[len(ROOT.parts)] in ('.private', 'src', 'test', 'docs', 'dist', 'research', 'reference', 'tools', '.venv'):
        raise ValueError('请使用独立的备份目录。')
    if path == Path(path.anchor) or path == Path.home() or path == Path.home() / 'Desktop':
        raise ValueError('请在此位置下新建一个专用备份文件夹。')
    return path


def prepare(root):
    root = output_path(root)
    root.mkdir(parents=True, exist_ok=True)
    data = root / '_data'
    if data.is_symlink() or root.is_symlink():
        raise ValueError('备份目录不能使用符号链接。')
    old = (root / 'archive.sqlite3').is_file()
    journal = root / '.layout-migration.json'
    if old or journal.exists():
        # Persist the migration plan first; replay it safely after interruption.
        plan = read_json(journal, [name for name in LEGACY if (root / name).exists()])
        json_write(journal, plan)
        data.mkdir(exist_ok=True)
        for name in plan:
            src, dst = root / name, data / name
            if src.exists():
                if dst.exists():
                    raise ValueError('旧备份与新结构有重名文件；请先核对两个 _data 数据集。')
                src.rename(dst)
        journal.unlink()
    elif not (data / 'archive.sqlite3').exists() and any(root.iterdir()):
        # A cancelled first export can leave only our empty data folder.
        if any(p.name not in ('_data', '.archive.lock') for p in root.iterdir()) or (data.exists() and any(data.iterdir())):
            raise ValueError('此目录含有其他文件，请选择空目录或已有 Replive 备份目录。')
    data.mkdir(exist_ok=True)
    return root, data


def clean_name(value, limit=70):
    text = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', str(value)).strip(' .')[:limit].rstrip(' .')
    if text.upper() in ('CON', 'PRN', 'AUX', 'NUL') or re.fullmatch(r'(COM|LPT)[0-9]', text.upper()):
        text = '_' + text
    return text or '名称未設定'


def js_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.building')
    temp.write_text(value, encoding='utf-8')
    temp.replace(path)


def public_value(value):
    if isinstance(value, dict):
        return {k: public_value(v) for k, v in value.items() if not k.endswith('_url') and k not in ('url', 'sharelink_url')}
    if isinstance(value, list):
        return [public_value(v) for v in value]
    return value


def subtitle_cues(path):
    """Embed VTT cues so direct file opening does not depend on track CORS."""
    if not path.is_file():
        return []
    def seconds(value):
        parts = value.replace(',', '.').split(':')
        result = 0.0
        for part in parts:
            result = result * 60 + float(part)
        return result
    result = []
    for block in re.split(r'\n\s*\n', path.read_text(encoding='utf-8-sig').replace('\r\n', '\n')):
        lines = block.splitlines()
        if lines and lines[0].startswith(('NOTE', 'STYLE', 'REGION')):
            continue
        for index, line in enumerate(lines):
            timing = re.match(r'([\d:]+[.,]\d+)\s+-->\s+([\d:]+[.,]\d+)', line)
            if timing:
                begin, end = seconds(timing[1]), seconds(timing[2])
                if end > begin:
                    result.append([begin, end, '\n'.join(lines[index + 1:])])
                break
    return result


@locked
def generate(root, progress=lambda **kw: None, cancel=None):
    root, data = prepare(root)
    if not (data / 'archive.sqlite3').is_file():
        raise ValueError('此目录尚无备份数据，请先导出。')
    progress(state='rendering', detail='正在生成独立页面与人物目录')
    owned = set()
    linked_checks = {}
    def text(rel, value):
        write_text(root / rel, value)
        owned.add(Path(rel).as_posix())
    def document(rel, value):
        text(rel, json.dumps(value, ensure_ascii=False, indent=2))
    db = sqlite3.connect((data / 'archive.sqlite3').as_uri() + '?mode=ro', uri=True)
    try:
        assets = {url: (path, size, digest) for url, path, size, digest in db.execute('SELECT url,path,size,sha256 FROM assets')}
        missing = []
        linked = set()
        def link(url, rel):
            if not url:
                return ''
            record = assets.get(url)
            if not record:
                missing.append({'file': str(rel), 'reason': '附件尚未下载'})
                return ''
            src = (data / record[0]).resolve()
            if data.resolve() not in src.parents or not src.is_file() or src.stat().st_size != record[1]:
                missing.append({'file': str(rel), 'reason': '附件缺失或长度不符'})
                return ''
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or not os.path.samefile(src, dst):
                temp = dst.with_name(dst.name + '.building')
                temp.unlink(missing_ok=True)
                try:
                    os.link(src, temp)
                except OSError:
                    shutil.copyfile(src, temp)
                temp.replace(dst)
            owned.add(Path(rel).as_posix())
            linked_checks[Path(rel).as_posix()] = {'size': record[1], 'sha256': record[2], 'source': '_data/' + record[0]}
            linked.add(record[0])
            return Path(rel).as_posix()
        def suffix(url, fallback):
            ext = Path(urlsplit(url or '').path).suffix.lower()
            return ext if re.fullmatch(r'\.[a-z0-9]{1,5}', ext) else fallback

        oshis = read_json(data / 'metadata/ListMyOshis.json', [])
        profiles = {o.get('user', {}).get('user_id'): o.get('user', {}) for o in oshis}
        rooms, people, all_messages = [], {}, {}
        room_metadata = read_json(data / 'metadata/rooms.json', [])
        starts = {s['user_id']: datetime.fromtimestamp(int(s.get('start_time', {}).get('seconds', 0)), JST).date() for s in read_json(data / 'metadata/subscriptions.json', []) if s.get('start_time', {}).get('seconds')}
        for room in room_metadata:
            check_cancel(cancel)
            uid, rid = room['user_id'], room['chat_room_id']
            profile = {**room.get('user_profile', {}), **profiles.get(uid, {})}
            name = profile.get('display_name') or uid
            folder = Path('人物') / (clean_name(name) + '_' + clean_name(uid))
            avatar = link(profile.get('avatar_url') or profile.get('profile_image_url'), folder / ('头像' + suffix(profile.get('avatar_url') or profile.get('profile_image_url'), '.jpg')))
            background = link(profile.get('profile_background_image_url'), folder / ('背景' + suffix(profile.get('profile_background_image_url'), '.jpg')))
            people[uid] = {'id': uid, 'name': name, 'unique': profile.get('unique_id', ''), 'avatar': avatar, 'background': background, 'folder': folder.as_posix()}
            document(folder / '资料.json', public_value(profile))
            messages = []
            md = [f'# {name}\n\n时间均为日本标准时间（JST）。\n']
            csv_rows = []
            for raw, in db.execute('SELECT json FROM messages WHERE room=? ORDER BY timestamp,id', (rid,)):
                check_cancel(cancel)
                value = json.loads(raw)
                mid = value['chat_message_id']
                stamp = value.get('time_jst', '')
                day, month = stamp[:10] or '日期未設定', stamp[:7] or '日期未設定'
                local = {}
                for key, kind, fallback in [('image_url', '图片', '.jpg'), ('video_url', '视频', '.mp4'), ('video_thumbnail_jpeg_url', '视频封面', '.jpg'), ('video_thumbnail_gif_url', '视频封面', '.gif')]:
                    if value.get(key):
                        local[key] = link(value[key], folder / kind / month / (day + '_' + clean_name(mid) + suffix(value[key], fallback)))
                msg = {'id': mid, 'date': stamp, 'type': value.get('type', 0), 'text': value.get('content', ''), 'question': value.get('card_content', ''),
                       'image': local.get('image_url', ''), 'video': local.get('video_url', ''), 'poster': local.get('video_thumbnail_jpeg_url', ''), 'videoId': value.get('video_id', ''), 'deleted': value.get('deleted', False)}
                messages.append(msg)
                md.append('\n## ' + stamp + '\n\n' + msg['text'] + '\n')
                if msg['question']:
                    md.append('\n提问：' + msg['question'] + '\n')
                for field in ('image', 'video'):
                    if msg[field]:
                        relative = Path(msg[field]).relative_to(folder).as_posix()
                        md.append(f'\n[{"图片" if field == "image" else "视频"}]({relative})\n')
                csv_rows.append((stamp, mid, msg['type'], msg['text'], msg['question'], msg['image'], msg['video']))
            text(folder / '聊天记录.md', ''.join(md))
            text(folder / '聊天记录.jsonl', ''.join(json.dumps(m, ensure_ascii=False) + '\n' for m in messages))
            csvfile = root / folder / '聊天记录.csv'
            with csvfile.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(('时间_JST', '消息ID', '类型', '正文', '问题', '图片路径', '视频路径'))
                # Prevent spreadsheet formula execution from exported message content.
                writer.writerows(tuple("'" + str(v) if str(v).startswith(('=', '+', '-', '@')) else v for v in row) for row in csv_rows)
            owned.add(csvfile.relative_to(root).as_posix())
            text(Path('页面资源/聊天') / (clean_name(rid) + '.js'), 'RepArchive.registerRoom(' + js_json(rid) + ',' + js_json(messages) + ');')
            day_count = max(1, (datetime.now(JST).date() - starts[uid]).days + 1) if uid in starts else None
            rooms.append({'id': rid, 'user': uid, 'name': name, 'avatar': avatar, 'count': len(messages), 'dayCount': day_count, 'latest': messages[-1] if messages else None, 'first': messages[0]['date'] if messages else '', 'data': '页面资源/聊天/' + clean_name(rid) + '.js'})
            all_messages[rid] = messages
        cards = {c['card_id']: c for c in read_json(data / 'metadata/cards.json', [])}
        public_cards = []
        for card_id, card in cards.items():
            uid = card.get('user_id', '')
            folder = Path(people.get(uid, {}).get('folder', '人物/' + clean_name(uid))) / '提问卡片'
            stamp = int(card.get('create_time', {}).get('seconds', 0))
            date = datetime.fromtimestamp(stamp, JST).isoformat() if stamp else ''
            title = (date[:10] or '日期未設定') + '_' + clean_name(card_id)
            text(folder / (title + '.txt'), card.get('content', '') + '\n')
            document(folder / (title + '.json'), public_value(card))
            public_cards.append({'id': card_id, 'user': uid, 'date': date, 'text': card.get('content', ''), 'sender': card.get('sending_user', {}).get('display_name', '')})
        answers = []
        for v in sorted(read_json(data / 'metadata/videos.json', []), key=lambda v: v.get('time_jst', ''), reverse=True):
            check_cancel(cancel)
            uid, vid = v['user_id'], v['video_id']
            card = cards.get(v.get('card_id'), {})
            person = people.setdefault(uid, {'id': uid, 'name': profiles.get(uid, {}).get('display_name', uid), 'avatar': '', 'folder': '人物/' + clean_name(uid)})
            date = v.get('time_jst', '')
            question = card.get('content', '')
            folder = Path(person['folder']) / '回答视频' / (date[:7] or '日期未設定') / ((date[:10] or '日期未設定') + '_' + clean_name(question, 28) + '_' + clean_name(vid))
            paths = {key: link(v.get(key), folder / (label + suffix(v.get(key), fallback))) for key, label, fallback in [('video_url', '回答', '.mp4'), ('thumbnail_url', '封面', '.jpg'), ('thumbnail_gif_url', '动态封面', '.gif'), ('subtitles_url', '字幕', '.vtt')]}
            text(folder / '问题.txt', question + '\n')
            document(folder / '详情.json', {**public_value(v), 'question': question, 'files': paths})
            cues = subtitle_cues(root / paths['subtitles_url']) if paths['subtitles_url'] else []
            answers.append({'id': vid, 'card': v.get('card_id', ''), 'user': uid, 'date': date, 'question': question, 'video': paths['video_url'], 'poster': paths['thumbnail_url'], 'subtitles': paths['subtitles_url'], 'cues': cues, 'duration': int(v.get('duration_msec', 0)) / 1000})
        report = public_value(read_json(data / 'report.json', {}))
        # Preserve every downloaded asset in the human-readable view, including
        # followed profiles or alternate avatar resolutions outside the rooms.
        for url, (rel, _, _) in assets.items():
            if rel not in linked:
                check_cancel(cancel)
                link(url, Path('其他资料资源') / (hashlib.sha256(rel.encode()).hexdigest()[:12] + '_' + clean_name(Path(rel).name, 180)))
        document('导出报告.json', report)
        document('缺失资源.json', missing)
        payload = {'version': 1, 'created': datetime.now(JST).isoformat(), 'rooms': rooms, 'people': list(people.values()), 'answers': answers,
                   'cards': public_cards, 'scope': {'accessibleComplete': report.get('accessible_content_complete', False), 'limitations': report.get('limitations', []), 'missing': len(missing)}}
        text('页面资源/archive-data.js', 'window.__REPARCHIVE__=' + js_json(payload) + ';')
        for source, target in [('index.html', 'index.html'), ('viewer.js', '页面资源/viewer.js'), ('viewer.css', '页面资源/viewer.css'), ('logo.svg', '页面资源/logo.svg')]:
            text(target, (ROOT / 'src/viewer' / source).read_text(encoding='utf-8'))
        text('备份说明.md', '# RepArchive 备份\n\n双击 `index.html` 浏览内容。\n\n- `人物/姓名_ID/`：聊天记录 Markdown、CSV、JSONL，图片、短视频、回答视频与字幕。\n- `页面资源/`：页面样式、程序与本地数据。\n- `_data/`：原始响应、元数据、SQLite 与媒体，供更新和校验。\n- `导出报告.json` / `缺失资源.json`：导出范围、统计与缺失项。\n\n人物目录媒体优先使用硬链接。复制到其他文件系统时可能占用两份空间。仅浏览可复制 index.html、页面资源、人物、导出报告.json 和缺失资源.json；更新资源时请保留整个目录。\n')
        summary = {'rooms': len(rooms), 'messages': sum(r['count'] for r in rooms), 'answers': len(answers), 'missing': len(missing), 'mediaFiles': report.get('total_media_downloaded', 0), 'mediaBytes': report.get('total_media_bytes', 0)}
        checks = {}
        for rel in sorted(owned):
            check_cancel(cancel)
            checks[rel] = linked_checks.get(rel) or {'size': (root / rel).stat().st_size, 'sha256': file_hash(root / rel)}
        manifest = {'format': 'replive-local-site', 'version': 1, 'created': payload['created'], 'summary': summary, 'files': sorted(owned), 'checksums': checks}
        json_write(root / MARKER, manifest)
        progress(detail='独立页面已生成', summary=summary)
        return manifest
    finally:
        db.close()


@locked
def verify(root, progress=lambda **kw: None, cancel=None):
    root, data = prepare(root)
    db = sqlite3.connect((data / 'archive.sqlite3').as_uri() + '?mode=ro', uri=True)
    errors, count, total = [], 0, 0
    try:
        rows = db.execute('SELECT path,max(size),max(sha256) FROM assets GROUP BY path').fetchall()
        for index, (rel, size, digest) in enumerate(rows, 1):
            check_cancel(cancel)
            progress(state='verifying', detail='正在校验媒体 SHA-256', media_done=index - 1, media_total=len(rows))
            path = (data / rel).resolve()
            if data.resolve() not in path.parents or not path.is_file() or path.stat().st_size != size or file_hash(path) != digest:
                errors.append({'file': rel, 'error': '缺失、长度或 SHA-256 不匹配'})
            else:
                count += 1
                total += size
        manifest = read_json(root / MARKER, {})
        for rel in manifest.get('files', []):
            check_cancel(cancel)
            target = (root / rel).resolve()
            if root not in target.parents or not target.is_file():
                errors.append({'file': rel, 'error': '页面或人物目录文件缺失'})
            elif manifest.get('checksums', {}).get(rel):
                expected = manifest['checksums'][rel]
                canonical = (root / expected.get('source', 'missing')).resolve()
                is_link = canonical.is_file() and data in canonical.parents and os.path.samefile(target, canonical)
                if target.stat().st_size != expected['size'] or (not is_link and file_hash(target) != expected['sha256']):
                    errors.append({'file': rel, 'error': '生成文件长度或 SHA-256 不匹配'})
        result = {'checked_at': datetime.now(JST).isoformat(), 'ok': not errors, 'media_files': count, 'media_bytes': total, 'errors': errors}
        json_write(root / '校验报告.json', result)
        progress(media_done=len(rows), media_total=len(rows))
        return result
    finally:
        db.close()


def export(token, root, progress=lambda **kw: None, cancel=None):
    with archive_lock(root):
        root, data = prepare(root)
        Exporter(API(token, cancel), data, progress, cancel).run()
        return generate(root, progress, cancel)
