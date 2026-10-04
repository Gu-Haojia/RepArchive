import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import export_replive as app
from extra_content import ExtraContent
import subscription_content_pb2 as pb


class ExtraContentTest(unittest.TestCase):
    def export(self, repeat=False):
        video = pb.Video(video_id='video', user_id='author', card_id='question', video_url='https://example.com/v.mp4', create_time=app.pb.Timestamp(seconds=1))
        card = pb.Card(card_id='question', user_id='author', content='<script>question</script>')
        class FakeAPI:
            def rpc(self, method, request, response_type, raw_path=None):
                if method.endswith('/ListMembershipPlanSubscriptions'):
                    response = pb.ListMembershipPlanSubscriptionsResponse(subscriptions=[pb.MembershipPlanSubscription(user_id='author')])
                elif method.endswith('/ListOshiVideos'):
                    response = pb.ListOshiVideosResponse(videos=[video], next_page_token='repeat' if repeat else '')
                elif method.endswith('/ListCards'):
                    response = pb.ListCardsResponse(cards=[card])
                elif method.endswith('/ListRepliedVideos'):
                    response = pb.ListRepliedVideosResponse()
                elif method.endswith('/ListSavedVideos'):
                    response = pb.ListSavedVideosResponse(videos=[video])
                elif method.endswith('/GetVideo'):
                    response = pb.GetVideoResponse(video=video, card=card)
                else:
                    raise AssertionError(method)
                if raw_path:
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    raw_path.write_bytes(response.SerializeToString())
                return response
        class Response(io.BytesIO):
            headers = {'Content-Type': 'video/mp4', 'Content-Length': '4'}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            app.json_write(output/'account_identity.json', {'user_id':'account'})
            app.json_write(output/'metadata/rooms.json', [{'user_id':'author'}])
            app.json_write(output/'metadata/ListMyOshis.json', [{'oshi_id':'oshi'}])
            exporter = app.Exporter(FakeAPI(), output)
            extra = ExtraContent(exporter, lambda **kwargs: None, app.to_dict, app.json_write, app.safe_error)
            with patch.object(app, 'urlopen', return_value=Response(b'mp4!')) as download, patch('extra_content.time.sleep'):
                extra.run()
            self.assertEqual(download.call_count, 1)
            self.assertEqual(exporter.report['extra_content']['video_count'], 1)
            self.assertEqual((output/'media/answers/video/video.mp4').read_bytes(), b'mp4!')
            self.assertEqual(json.loads((output/'metadata/videos.json').read_text())[0]['video_id'], 'video')
            self.assertIn('&lt;script&gt;question', (output/'answers/index.html').read_text())
            self.assertIn('../media/answers/video/video.mp4', (output/'answers/index.html').read_text())
            self.assertIn('1970-01-01T09:00:01+09:00', (output/'answers/index.html').read_text())
            self.assertTrue((output/'raw/extra/details/video.pb').exists())
            report = exporter.report
            exporter.db.close()
            return report

    def test_sources_deduplicate_videos_and_offline_assets_link_correctly(self):
        report = self.export()
        self.assertTrue(report['extra_content']['complete'])
        self.assertTrue(report['subscription_rooms_match'])
        self.assertEqual(report['errors'], [])

    def test_partial_pagination_preserves_metadata_and_does_not_claim_complete(self):
        report = self.export(repeat=True)
        self.assertFalse(report['extra_content']['complete'])
        self.assertEqual(report['errors'][0]['stage'], 'extra_list')
        self.assertIn('游标重复', report['errors'][0]['error'])


if __name__ == '__main__':
    unittest.main()
