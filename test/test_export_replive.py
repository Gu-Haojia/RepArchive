import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import export_replive as app


def message(identifier, ts, **kwargs):
    return app.pb.ListChatMessages(chat_message_id=identifier, timestamp=app.pb.Timestamp(seconds=ts), **kwargs)


class ExportTest(unittest.TestCase):
    def test_request_body_and_token_are_read_without_deleting_first_character(self):
        token = 'f8fb1234-5678-aaaa-bbbb-111111111111'
        self.assertEqual(app.token_from_bytes(token.encode()), token)
        self.assertEqual(app.token_from_bytes(app.pb.RefreshAccessTokenRequest(refresh_token=token).SerializeToString()), token)
        self.assertEqual(app.token_from_bytes(json.dumps({'refresh_token': token}).encode()), token)

    def test_padded_session_and_alphabetic_protobuf_length_byte(self):
        # 实际短信会话有 base64 padding；长度 108 的 protobuf 长度字节是字母 l。
        token = 'a' * 106 + '=='
        self.assertEqual(app.token_from_bytes(token.encode()), token)
        self.assertEqual(app.token_from_bytes(app.pb.RefreshAccessTokenRequest(refresh_token=token).SerializeToString()), token)

    def test_new_chat_fields_survive_old_response_parser(self):
        modern = app.ChatMessage(chat_message_id='m', timestamp=app.pb.Timestamp(seconds=1),
                                 video_thumbnail_jpeg_url='https://example.com/poster.jpg',
                                 video_thumbnail_gif_url='https://example.com/poster.gif', card_content='question')
        old = app.pb.ListChatMessages.FromString(modern.SerializeToString())
        with tempfile.TemporaryDirectory() as temporary:
            exporter = app.Exporter(None, temporary)
            exporter.save_page('r', [old])
            saved = json.loads(exporter.db.execute('SELECT json FROM messages').fetchone()[0])
            self.assertEqual(saved['video_thumbnail_jpeg_url'], modern.video_thumbnail_jpeg_url)
            self.assertEqual(saved['video_thumbnail_gif_url'], modern.video_thumbnail_gif_url)
            self.assertEqual(saved['card_content'], 'question')
            exporter.db.close()

    def test_paginated_snapshot_survives_failed_media_and_is_readable_offline(self):
        # 旧页含当前游标不应造成提前结束；继续取下一页直到服务返回结束。
        room = app.pb.ChatRoom(user_id='author', chat_room_id='room', user_profile=app.pb.UserProfile(display_name='测试角色'))
        calls = []
        class FakeAPI:
            def rpc(self, method, request, response_type, raw_path=None, auth=True):
                calls.append((method, request))
                if raw_path:
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    raw_path.write_bytes(b'raw')
                if method.endswith('/GetUserPrivate'):
                    return app.ext.GetUserPrivateResponse(user=app.pb.UserPrivate(user_id='account', display_name='test'))
                if method.endswith('/ListChatRooms'):
                    return app.pb.ListChatRoomsResponse(chat_rooms=[room])
                if method.endswith('/ListMyOshis'):
                    return app.ext.ListMyOshisResponse()
                if method.endswith('/ListFollowings'):
                    return app.ext.ListFollowingsResponse()
                if not request.cursor_chat_message_id:
                    return app.pb.ListChatMessagesResponse(messages=[message('latest', 3)], next_page_cursor_message_id='latest')
                if request.backward and request.cursor_chat_message_id == 'latest':
                    return app.pb.ListChatMessagesResponse(messages=[message('latest', 3), message('middle', 2, content='<script>test</script>')], next_page_cursor_message_id='middle')
                if request.backward:
                    return app.pb.ListChatMessagesResponse(messages=[message('first', 1, image_url='https://example.com/missing.jpg')])
                return app.pb.ListChatMessagesResponse()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'backup'
            exporter = app.Exporter(FakeAPI(), output)
            with patch.object(app, 'update'), patch.object(app.time, 'sleep'), patch.object(exporter, 'download', side_effect=ValueError('测试附件失败')):
                exporter.run()
            report = json.loads((output / 'report.json').read_text())
            self.assertTrue(report['rooms'][0]['history_complete'])
            self.assertEqual(report['rooms'][0]['message_count'], 3)
            self.assertFalse(report['chat_snapshot_complete'])
            self.assertFalse(report['all_subscribed_content_complete'])
            messages = [json.loads(line) for line in (output / 'chats/room/messages.jsonl').read_text().splitlines()]
            self.assertEqual([x['chat_message_id'] for x in messages], ['first', 'middle', 'latest'])
            document = (output / 'chats/room/index.html').read_text()
            self.assertIn('&lt;script&gt;', document)
            self.assertNotIn('<script>test</script>', document)
            self.assertIn('附件未下载', document)
            self.assertEqual(messages[0]['time_jst'], '1970-01-01T09:00:01+09:00')
            self.assertTrue((output / 'raw/chat/room/older-00002.pb').exists())

    def test_repeated_cursor_is_not_reported_as_complete(self):
        class FakeAPI:
            def rpc(self, *args):
                return app.pb.ListChatMessagesResponse(messages=[message('a', 1)], next_page_cursor_message_id='a')
        with tempfile.TemporaryDirectory() as temporary:
            exporter = app.Exporter(FakeAPI(), temporary)
            with patch.object(app, 'update'), patch.object(app.time, 'sleep'):
                exporter.room_sync(app.pb.ChatRoom(user_id='u', chat_room_id='r'))
            self.assertFalse(exporter.report['rooms'][0]['history_complete'])
            self.assertEqual(len(exporter.report['errors']), 2)
            exporter.db.close()

    def test_media_integrity_and_resume(self):
        class Response(io.BytesIO):
            headers = {'Content-Type': 'image/jpeg', 'Content-Length': '4'}
        with tempfile.TemporaryDirectory() as temporary:
            exporter = app.Exporter(None, temporary)
            path = Path(temporary) / 'media/test.jpg'
            with patch.object(app, 'urlopen', return_value=Response(b'jpeg')) as mocked:
                exporter.download('https://example.com/test.jpg', path)
                exporter.download('https://example.com/test.jpg', path)
                self.assertEqual(mocked.call_count, 1)
            path.write_bytes(b'bad!')
            with patch.object(app, 'urlopen', return_value=Response(b'jpeg')) as mocked:
                exporter.download('https://example.com/test.jpg', path)
                self.assertEqual(mocked.call_count, 1)
            self.assertEqual(path.read_bytes(), b'jpeg')
            exporter.db.close()

    def test_rotated_vod_signature_reuses_intact_file_but_keeps_content_parameters(self):
        class Response(io.BytesIO):
            headers = {'Content-Type': 'video/mp4', 'Content-Length': '4'}
        with tempfile.TemporaryDirectory() as temporary:
            exporter = app.Exporter(None, temporary)
            path = Path(temporary) / 'media/video.mp4'
            with patch.object(app, 'urlopen', return_value=Response(b'mp4!')) as mocked:
                exporter.download('https://vod.replive.com/id.mp4?t=a&us=b&sign=c', path)
                exporter.download('https://vod.replive.com/id.mp4?t=d&us=e&sign=f', path)
                self.assertEqual(mocked.call_count, 1)
            with patch.object(app, 'urlopen', return_value=Response(b'mp4!')) as mocked:
                exporter.download('https://vod.replive.com/id.mp4?t=d&us=e&sign=f&exper=30', path)
                self.assertEqual(mocked.call_count, 1)
            exporter.db.close()


if __name__ == '__main__':
    unittest.main()
