"""Exercise complete controller exports with a Windows GBK file default."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import archive_site as site
import export_replive as core
import subscription_content_pb2 as content
import webui


native_read_text = Path.read_text


def gbk_read_text(path, encoding=None, errors=None):
    # Explicit encodings still work; unspecified reads behave like Chinese Windows.
    return native_read_text(path, encoding=encoding or 'gbk', errors=errors)


class ReplyAPI:
    def rpc(self, method, request, response_type, raw_path=None, auth=True):
        video = content.Video(video_id='reply', user_id='author', card_id='card',
                              video_url='https://example.com/reply.mp4',
                              create_time=core.pb.Timestamp(seconds=1))
        card = content.Card(card_id='card', user_id='author', content='日本語の質問中')
        if method.endswith('/GetUserPrivate'):
            response = core.ext.GetUserPrivateResponse(
                user=core.pb.UserPrivate(user_id='account', display_name='テストの名前中'))
        elif method.endswith('/ListChatRooms'):
            response = core.pb.ListChatRoomsResponse(chat_rooms=[core.pb.ChatRoom(
                user_id='author', chat_room_id='room',
                user_profile=core.pb.UserProfile(display_name='日本語の人物中'))])
        elif method.endswith('/ListChatMessages'):
            response = core.pb.ListChatMessagesResponse()
            if not request.cursor_chat_message_id:
                response.messages.append(core.pb.ListChatMessages(
                    chat_message_id='message', content='こんにちは、中',
                    timestamp=core.pb.Timestamp(seconds=1)))
        elif method.endswith('/ListMyOshis'):
            response = core.ext.ListMyOshisResponse(oshis=[core.ext.ListMyOshisOshi(
                oshi_id='oshi', name='日本語の推し中')])
        elif method.endswith('/ListFollowings'):
            response = core.ext.ListFollowingsResponse()
        elif method.endswith('/ListMembershipPlanSubscriptions'):
            response = content.ListMembershipPlanSubscriptionsResponse(
                subscriptions=[content.MembershipPlanSubscription(user_id='author')])
        elif method.endswith('/ListOshiVideos'):
            response = content.ListOshiVideosResponse(videos=[video])
        elif method.endswith('/ListCards'):
            response = content.ListCardsResponse(cards=[card])
        elif method.endswith('/ListSavedVideos'):
            response = content.ListSavedVideosResponse(videos=[video])
        elif method.endswith('/ListRepliedVideos'):
            response = content.ListRepliedVideosResponse()
        elif method.endswith('/GetVideo'):
            response = content.GetVideoResponse(video=video, card=card)
        else:
            raise AssertionError(method)
        if raw_path:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(response.SerializeToString())
        return response


class MediaResponse(io.BytesIO):
    headers = {'Content-Type': 'video/mp4', 'Content-Length': '4'}


class WindowsEncodingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '备份 日本語'
        self.controller = webui.Controller(self.root, Path(self.temp.name) / 'state')
        self.controller.token_path.write_text('00000000-0000-0000-0000-000000000000', encoding='utf-8')

    def export(self, gbk=False):
        with patch.object(site, 'API', return_value=ReplyAPI()), \
             patch.object(core, 'urlopen', side_effect=lambda *a, **kw: MediaResponse(b'mp4!')):
            if gbk:
                with patch.object(Path, 'read_text', gbk_read_text):
                    self.controller.start('export', output=self.root)
                    self.controller.worker.join(5)
            else:
                self.controller.start('export', output=self.root)
                self.controller.worker.join(5)
        self.assertFalse(self.controller.worker.is_alive())

    def assert_complete_backup(self):
        self.assertEqual(self.controller.job['state'], 'complete', self.controller.job['detail'])
        report = site.read_json(self.root / '_data/report.json')
        self.assertTrue(report['chat_snapshot_complete'])
        self.assertTrue(report['extra_content']['complete'])
        self.assertEqual(report['errors'], [])
        self.assertEqual((self.root / '_data/media/answers/reply/video.mp4').read_bytes(), b'mp4!')
        videos = site.read_json(self.root / '_data/metadata/videos.json')
        self.assertEqual(videos[0]['video_id'], 'reply')
        self.assertTrue(site.verify(self.root)['ok'])
        self.assertIn('日本語の人物中',
                      (self.root / '页面资源/archive-data.js').read_text(encoding='utf-8'))

    def test_first_export_downloads_chat_and_reply_with_gbk_default(self):
        self.export(gbk=True)
        self.assert_complete_backup()

    def test_update_reads_existing_utf8_identity_with_gbk_default(self):
        self.export()
        self.assert_complete_backup()
        self.export(gbk=True)
        self.assert_complete_backup()

    def test_update_recovers_reply_stage_from_an_incomplete_backup(self):
        previous_error = UnicodeDecodeError('gbk', b'\xad"', 0, 1,
                                            'illegal multibyte sequence')
        with patch.object(core.Exporter, 'extra', side_effect=previous_error):
            self.export()
        self.assertEqual(self.controller.job['state'], 'partial')
        report = site.read_json(self.root / '_data/report.json')
        self.assertTrue(report['chat_snapshot_complete'])
        self.assertEqual(report['errors'][0]['stage'], 'extra_content')
        self.assertFalse((self.root / '_data/media/answers/reply/video.mp4').exists())
        self.export(gbk=True)
        self.assert_complete_backup()


if __name__ == '__main__':
    unittest.main()
