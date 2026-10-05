"""导出订阅清单、可读取的提问卡片与独立回答视频。"""
import hashlib
import html
import json
from pathlib import Path
import re
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import subscription_content_pb2 as pb


def video_time(video):
    timestamp = video.get('create_time') or video.get('reply_start_time') or {}
    return int(timestamp.get('seconds', 0))


class ExtraContent:
    def __init__(self, exporter, update, to_dict, json_write, safe_error):
        self.exporter = exporter
        self.api = exporter.api
        self.output = exporter.output
        self.report = exporter.report
        self.update = update
        self.to_dict = to_dict
        self.json_write = json_write
        self.safe_error = safe_error
        self.videos = {}
        self.cards = {}
        self.sources = []
        self.errors = []

    def pages(self, service, name, request, response_type, field, label):
        result = {'source': name, 'label': label, 'complete': False, 'count': 0, 'pages': 0}
        self.sources.append(result)
        seen = {''}
        items = []
        for index in range(10000):
            from export_replive import check_cancel
            check_cancel(self.exporter.cancel)
            self.update(state='exporting', detail=f'正在核对 {label} 的独立内容')
            response = self.api.rpc(f'user.v1.{service}/{name}', request, response_type,
                                    self.output / 'raw/extra' / label / f'{name}-{index:05d}.pb')
            page = list(getattr(response, field))
            items.extend(page)
            result.update(count=len(items), pages=index + 1)
            if field == 'videos':
                for video in page:
                    if not video.video_id:
                        raise ValueError('视频缺少 ID，完整性未确认。')
                    self.videos[video.video_id] = self.to_dict(video)
                for key, card in response.cards.items():
                    self.cards[key] = self.to_dict(card)
            if not response.next_page_token:
                result['complete'] = True
                return items
            if response.next_page_token in seen:
                raise ValueError(name + ' 分页游标重复，完整性未确认。')
            request.page_token = response.next_page_token
            seen.add(request.page_token)
            time.sleep(0.2)
        raise ValueError(name + ' 超出页数上限。')

    def call_pages(self, *args):
        try:
            return self.pages(*args)
        except Exception as exc:
            self.errors.append({'stage': 'extra_list', 'source': args[1], 'label': args[-1], 'error': self.safe_error(exc)})
            return []

    def run(self):
        user_id = json.loads((self.output / 'account_identity.json').read_text(encoding='utf-8'))['user_id']
        subscriptions = self.call_pages('UserService', 'ListMembershipPlanSubscriptions',
            pb.ListMembershipPlanSubscriptionsRequest(max_page_size=100), pb.ListMembershipPlanSubscriptionsResponse,
            'subscriptions', 'subscriptions')
        self.json_write(self.output / 'metadata/subscriptions.json', [self.to_dict(x) for x in subscriptions])
        rooms = json.loads((self.output / 'metadata/rooms.json').read_text(encoding='utf-8'))
        room_users = {room['user_id'] for room in rooms}
        active_users = {s.user_id for s in subscriptions if not s.expired}
        self.report['active_subscription_count'] = len(active_users)
        self.report['subscription_rooms_match'] = active_users == room_users
        if active_users != room_users:
            self.errors.append({'stage': 'subscription_match', 'error': '有效订阅作者与聊天房间列表不一致，完整性未确认。'})
        oshis = json.loads((self.output / 'metadata/ListMyOshis.json').read_text(encoding='utf-8'))
        for oshi in oshis:
            self.call_pages('LiveService', 'ListOshiVideos',
                pb.ListOshiVideosRequest(oshi_id=oshi['oshi_id'], max_page_size=100),
                pb.ListOshiVideosResponse, 'videos', 'oshi-' + oshi['oshi_id'])
        for author in sorted(active_users):
            cards = self.call_pages('LiveService', 'ListCards',
                pb.ListCardsRequest(user_id=author, max_page_size=100, is_fandom_only=True),
                pb.ListCardsResponse, 'cards', 'cards-' + author)
            for card in cards:
                self.cards[card.card_id] = self.to_dict(card)
        self.call_pages('LiveService', 'ListRepliedVideos',
            pb.ListRepliedVideosRequest(user_id=user_id, max_page_size=100),
            pb.ListRepliedVideosResponse, 'videos', 'my-replied')
        self.call_pages('LiveService', 'ListSavedVideos',
            pb.ListSavedVideosRequest(saving_user_id=user_id, max_page_size=100),
            pb.ListSavedVideosResponse, 'videos', 'my-saved')
        # 获取详情，以核对列表中的播放地址并保存对应问题和作者资料。
        for index, (video_id, value) in enumerate(list(self.videos.items()), 1):
            self.update(state='exporting', detail=f'正在保存独立视频详情 {index} / {len(self.videos)}')
            try:
                response = self.api.rpc('user.v1.LiveService/GetVideo',
                    pb.GetVideoRequest(user_id=value['user_id'], video_id=video_id), pb.GetVideoResponse,
                    self.output / 'raw/extra/details' / (video_id + '.pb'))
                if response.video.video_id != video_id:
                    raise ValueError('视频详情 ID 不匹配。')
                self.videos[video_id] = self.to_dict(response.video)
                self.json_write(self.output / 'metadata/video_details' / (video_id + '.json'), self.to_dict(response))
                if response.card.card_id:
                    self.cards[response.card.card_id] = self.to_dict(response.card)
            except Exception as exc:
                self.errors.append({'stage': 'video_detail', 'video_id': video_id, 'error': self.safe_error(exc)})
        # 在下载前落盘，保证单个媒体失败时仍然保留全部列表和问题文字。
        for video in self.videos.values():
            if seconds := video_time(video):
                video['time_jst'] = datetime.fromtimestamp(seconds, ZoneInfo('Asia/Tokyo')).isoformat()
        self.json_write(self.output / 'metadata/videos.json', list(self.videos.values()))
        self.json_write(self.output / 'metadata/cards.json', list(self.cards.values()))
        assets = {}
        for video_id, video in self.videos.items():
            if not video.get('video_url'):
                self.errors.append({'stage': 'video_media', 'video_id': video_id, 'error': '服务没有提供视频地址。'})
            for key, fallback in [('video_url', '.mp4'), ('thumbnail_url', '.jpg'),
                                  ('thumbnail_gif_url', '.gif'), ('subtitles_url', '.vtt')]:
                if url := video.get(key):
                    suffix = Path(urlsplit(url).path).suffix.lower()
                    if key == 'video_url' and suffix in ('.m3u8', '.mpd'):
                        self.errors.append({'stage': 'video_media', 'video_id': video_id, 'error': '播放清单需另行下载片段，未标记视频完成。'})
                        continue
                    if not re.fullmatch(r'\.[a-z0-9]{1,5}', suffix):
                        suffix = fallback
                    assets.setdefault(url, self.output / 'media/answers' / video_id / (key[:-4] + suffix))
        for index, (url, path) in enumerate(assets.items(), 1):
            self.update(state='downloading', detail='正在保存独立回答视频及封面', media_done=index - 1, media_total=len(assets))
            try:
                self.exporter.download(url, path)
            except Exception as exc:
                self.errors.append({'stage': 'extra_media', 'asset_id': hashlib.sha256(url.encode()).hexdigest()[:24],
                                    'error': self.safe_error(exc)})
        self.report['extra_content'] = {'sources': self.sources, 'video_count': len(self.videos),
            'card_count': len(self.cards), 'assets_expected': len(assets), 'complete': not self.errors and all(s['complete'] for s in self.sources),
            'errors': self.errors}
        self.report['errors'].extend(self.errors)
        self.readable()
        self.update(media_done=len(assets), media_total=len(assets))

    def readable(self):
        articles = []
        for video_id, video in sorted(self.videos.items(), key=lambda row: (video_time(row[1]), row[0])):
            card = self.cards.get(video.get('card_id'), {})
            content = html.escape(card.get('content', ''), quote=True)
            media = []
            for key in ('video_url', 'thumbnail_url', 'subtitles_url'):
                record = self.exporter.db.execute('SELECT path FROM assets WHERE url=?', (video.get(key, ''),)).fetchone()
                if record:
                    source = html.escape('../' + record[0], quote=True)
                    if key == 'video_url':
                        media.append(f'<video controls preload="none" src="{source}"></video>')
                    else:
                        media.append(f'<a href="{source}">{"封面" if key == "thumbnail_url" else "字幕"}</a>')
                elif key == 'video_url':
                    media.append('<p>视频未保存，见 report.json 缺失清单。</p>')
            date = video.get('time_jst') or (datetime.fromtimestamp(video_time(video), ZoneInfo('Asia/Tokyo')).isoformat() if video_time(video) else '')
            articles.append('<article><time>' + html.escape(date) + '</time><p>' + content + '</p>' + ''.join(media) + '</article>')
        target = self.output / 'answers'
        target.mkdir(exist_ok=True)
        target.joinpath('index.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>独立回答视频</title><style>body{max-width:800px;margin:24px auto;padding:16px;font-family:system-ui;background:#f5f5f5}article{background:white;padding:18px;margin:16px 0;border-radius:10px}p{white-space:pre-wrap}video{display:block;width:100%}a{margin-right:16px}time{font-size:13px;color:#666}</style><a href="../index.html">返回目录</a><h1>独立回答视频 · ' + str(len(self.videos)) + '</h1>' + ''.join(articles), encoding='utf-8')
