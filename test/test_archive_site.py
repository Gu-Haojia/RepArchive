import csv
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

import archive_site as site
import export_replive as core


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    exporter = core.Exporter(None, root)
    room = {'chat_room_id': 'room', 'user_id': 'author', 'user_profile': {'display_name': '日本語 / 名前', 'avatar_url': 'https://example.com/avatar.jpg'}}
    core.json_write(root / 'metadata/rooms.json', [room])
    core.json_write(root / 'metadata/ListMyOshis.json', [])
    core.json_write(root / 'account_identity.json', {'user_id': 'account'})
    core.json_write(root / 'report.json', {'total_media_downloaded': 1, 'total_media_bytes': 4, 'accessible_content_complete': True})
    (root / 'media').mkdir()
    path = root / 'media/avatar.jpg'
    path.write_bytes(b'jpeg')
    exporter.db.execute('INSERT INTO assets VALUES (?,?,?,?)', ('https://example.com/avatar.jpg', 'media/avatar.jpg', 4, core.file_hash(path)))
    value = {'chat_message_id': 'm', 'time_jst': '2025-01-01T13:00:00+09:00', 'content': '=1+2\n</script><script>malicious()</script>', 'image_url': 'https://example.com/avatar.jpg', 'type': 2}
    exporter.db.execute('INSERT INTO messages VALUES (?,?,?,?)', ('room', 'm', 1, json.dumps(value)))
    exporter.db.commit()
    exporter.db.close()
    (root / 'index.html').write_text('old index')
    return value


class ArchiveSiteTest(unittest.TestCase):
    def test_chat_sender_uses_message_profile_instead_of_room_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'backup'
            fixture(root)
            db = core.sqlite3.connect(root / 'archive.sqlite3')
            db.execute('DELETE FROM messages')
            for index, (identifier, sender_id, sender_name, body) in enumerate([
                ('incoming', 'author', '日本語 / 名前', 'おすすめを教えて'),
                ('outgoing', 'account', 'private-account-name', 'おすすめはこちら'),
                ('another-sender', 'another-user', '別の送信者', '別のメッセージ'),
                ('legacy', '', '', 'プロフィールなし'),
                ('unknown', 'unknown-user', '', '名前なしの送信者'),
            ], 1):
                value = {'chat_message_id': identifier, 'user_id': 'author',
                         'type': 1, 'content': body,
                         'time_jst': f'2026-07-29T17:26:0{index}+09:00'}
                if sender_id:
                    value['user_profile'] = {'user_id': sender_id,
                                             'display_name': sender_name}
                db.execute('INSERT INTO messages VALUES (?,?,?,?)',
                           ('room', identifier, index, json.dumps(value)))
            db.commit();db.close()
            site.generate(root)
            path = next(root.glob('人物/*/聊天记录.jsonl'))
            messages = {m['id']: m for m in map(json.loads, path.read_text(encoding='utf-8').splitlines())}
            self.assertTrue(messages['outgoing']['outgoing'])
            self.assertEqual(messages['outgoing']['sender'], 'あなた')
            self.assertFalse(messages['incoming']['outgoing'])
            self.assertEqual(messages['incoming']['sender'], '日本語 / 名前')
            self.assertFalse(messages['another-sender']['outgoing'])
            self.assertEqual(messages['another-sender']['sender'], '別の送信者')
            self.assertEqual(messages['another-sender']['senderAvatar'], '')
            self.assertFalse(messages['legacy']['outgoing'])
            self.assertEqual(messages['legacy']['sender'], '日本語 / 名前')
            self.assertEqual(messages['unknown']['sender'], '送信者')
            self.assertEqual(messages['unknown']['senderAvatar'], '')
            self.assertNotIn('private-account-name', path.read_text(encoding='utf-8'))
            markdown = path.with_suffix('.md').read_text(encoding='utf-8')
            self.assertIn('17:26:02+09:00 · 我', markdown)
            with path.with_suffix('.csv').open(encoding='utf-8-sig', newline='') as stream:
                rows = {r['消息ID']: r for r in csv.DictReader(stream)}
            self.assertEqual(rows['outgoing']['发送方'], '我')
            self.assertEqual(rows['outgoing']['方向'], '发出')
            self.assertEqual(rows['incoming']['方向'], '收到')
            self.assertTrue(site.verify(root)['ok'])

    def test_migration_standalone_escaping_hardlinks_custom_directory_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '日本語 #? backup'
            fixture(root)
            (root / 'my-notes.txt').write_text('keep')
            manifest = site.generate(root)
            self.assertTrue((root / '_data/index.html').is_file())
            self.assertEqual((root / 'my-notes.txt').read_text(), 'keep')
            self.assertTrue((root / '_data/archive.sqlite3').is_file())
            javascript = (root / '页面资源/聊天/room.js').read_text()
            self.assertNotIn('</script>', javascript)
            self.assertNotIn('https://', javascript)
            self.assertNotIn('refresh_token', javascript)
            self.assertIn('\\u003c/script>', javascript)
            picture = next(root.glob('人物/*/图片/*/*.jpg'))
            self.assertTrue(os.path.samefile(picture, root / '_data/media/avatar.jpg'))
            csv = next(root.glob('人物/*/聊天记录.csv')).read_text(encoding='utf-8-sig')
            self.assertIn("'=1+2", csv)
            site.generate(root)
            self.assertTrue(site.verify(root)['ok'])
            self.assertEqual(manifest['summary']['messages'], 1)
            # Portable browsing package has no project/backend files.
            standalone = Path(temp) / 'portable'
            for rel in manifest['files']:
                dst = standalone / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, dst)
            self.assertFalse((standalone / '_data').exists())
            self.assertTrue((standalone / 'index.html').is_file())
            self.assertNotIn('/api/', (standalone / '页面资源/viewer.js').read_text())

    def test_corrupt_media_and_missing_generated_file_fail_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'backup';fixture(root);site.generate(root)
            (root / '_data/media/avatar.jpg').write_bytes(b'bad!')
            (root / '页面资源/viewer.js').unlink()
            result = site.verify(root)
            self.assertFalse(result['ok']);self.assertGreaterEqual(len(result['errors']), 2)

    def test_incomplete_migration_can_resume_without_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'backup';fixture(root)
            plan = [name for name in site.LEGACY if (root / name).exists()]
            core.json_write(root / '.layout-migration.json', plan)
            (root / '_data').mkdir()
            (root / 'archive.sqlite3').rename(root / '_data/archive.sqlite3')
            site.generate(root)
            self.assertFalse((root / '.layout-migration.json').exists())
            self.assertTrue(site.verify(root)['ok'])

    def test_refuses_unrelated_directory_and_cancel_prevents_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'unrelated';root.mkdir();(root / 'notes').write_text('keep')
            with self.assertRaises(ValueError): site.prepare(root)
            cancel = threading.Event();cancel.set()
            with self.assertRaises(core.Cancelled): core.API('secret', cancel).rpc('irrelevant', None, None)
            self.assertEqual((root / 'notes').read_text(), 'keep')

    def test_exclusive_archive_lock_blocks_another_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'backup';fixture(root)
            errors = []
            def concurrent():
                try:
                    site.generate(root)
                except ValueError as exc:
                    errors.append(str(exc))
            with site.archive_lock(root):
                thread = threading.Thread(target=concurrent);thread.start();thread.join(5)
                self.assertEqual(len(errors), 1)
            self.assertEqual(site.generate(root)['summary']['messages'], 1)

    def test_vtt_cues_support_direct_offline_captions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'subtitles.vtt'
            path.write_text('WEBVTT\n\nNOTE ignored\n00:01.000 --> 00:02.000\nignore\n\ncue-id\n00:00:03.250 --> 00:00:05.000 align:start\n日本語\n二行目\n')
            self.assertEqual(site.subtitle_cues(path), [[3.25, 5.0, '日本語\n二行目']])

    def test_readable_copy_and_page_tampering_are_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'backup';fixture(root);site.generate(root)
            target = next(root.glob('人物/*/图片/*/*.jpg'))
            replacement = target.with_name('replacement')
            replacement.write_bytes(b'bad!');replacement.replace(target)
            self.assertEqual((root / '_data/media/avatar.jpg').read_bytes(), b'jpeg')
            result = site.verify(root)
            self.assertFalse(result['ok']);self.assertEqual(result['errors'][0]['file'], target.relative_to(root).as_posix())


if __name__ == '__main__': unittest.main()
