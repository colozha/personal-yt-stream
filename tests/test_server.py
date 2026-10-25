import io
import json
import socket
import subprocess
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from email.message import Message

import server


MASTER = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="a"
video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="a"
too-large.m3u8
'''
SOURCE = 'https://manifest.googlevideo.com/master.m3u8'


class Response(io.BytesIO):
    def __init__(self, body, status=200, content_type='application/vnd.apple.mpegurl', **headers):
        super().__init__(body)
        self.url, self.status = SOURCE, status
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        self.headers['Content-Length'] = str(len(body))
        for key, value in headers.items():
            self.headers[key.replace('_', '-')] = value


def session():
    return {'id': 'a' * 32, 'accessed': time.monotonic(), 'headers': {}, 'resources': {}, 'urls': {}}


class SourceTests(unittest.TestCase):
    def test_source_validation_and_private_addresses(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('142.250.1.1', 443))]
        with patch('server.socket.getaddrinfo', return_value=public):
            self.assertEqual(server.validate_source(SOURCE), SOURCE)
            for url in ['http://manifest.googlevideo.com/a', 'https://example.com/a',
                        'https://evilgooglevideo.com/a', 'https://googlevideo.com.evil/a',
                        'https://user@manifest.googlevideo.com/a', 'https://manifest.googlevideo.com:444/a']:
                with self.subTest(url=url), self.assertRaises(server.PlaybackError):
                    server.validate_source(url)
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1']:
            with patch('server.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]):
                with self.subTest(ip=ip), self.assertRaises(server.PlaybackError):
                    server.validate_source(SOURCE)

    def test_redirect_cannot_leave_cdn(self):
        with self.assertRaises(server.PlaybackError):
            server.SourceRedirect().redirect_request(Request(SOURCE), None, 302, '', {}, 'http://127.0.0.1/admin')

    def test_manifest_filters_quality_and_rewrites_every_source(self):
        state = session()
        result = server.rewrite_manifest(server.filter_master(MASTER), SOURCE, state)
        self.assertNotIn('1920x1080', result)
        self.assertNotIn('googlevideo.com', result)
        self.assertIn('URI="/api/playback/', result)
        self.assertEqual(len(state['resources']), 2)
        self.assertIn('https://manifest.googlevideo.com/audio.m3u8', state['urls'])
        self.assertEqual(server.rewrite_manifest(server.filter_master(MASTER), SOURCE, state), result)

    def test_media_playlist_rewrites_init_relative_segments_and_byte_ranges(self):
        text = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4",BYTERANGE="100@0"\n#EXT-X-BYTERANGE:100@100\n#EXTINF:6,\n../seg.mp4\n#EXT-X-ENDLIST\n'
        state = session()
        result = server.rewrite_manifest(text, 'https://r.googlevideo.com/video/index.m3u8', state)
        self.assertIn('BYTERANGE="100@0"', result)
        self.assertIn('#EXT-X-BYTERANGE:100@100', result)
        self.assertIn('#EXT-X-ENDLIST', result)
        self.assertEqual(set(state['resources'].values()), {'https://r.googlevideo.com/video/init.mp4', 'https://r.googlevideo.com/seg.mp4'})

    def test_unsupported_manifests_and_external_resources(self):
        with self.assertRaises(server.PlaybackError):
            server.filter_master('#EXTM3U\n')
        with self.assertRaises(server.PlaybackError):
            server.read_manifest(Response(b'<html>not a playlist</html>'))
        with self.assertRaises(server.PlaybackError):
            server.rewrite_manifest('#EXTM3U\nhttps://127.0.0.1/a', SOURCE, session())
        with self.assertRaises(server.PlaybackError):
            server.rewrite_manifest('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"', SOURCE, session())

    def test_long_recording_playlist_is_not_truncated(self):
        text = b'#EXTM3U\n' + b'#EXTINF:6,\nsegment.ts\n' * 150000
        self.assertEqual(server.read_manifest(Response(text)), text.decode())

    def test_expired_sessions_removed_but_active_kept(self):
        old, active = session(), session()
        old['accessed'] -= server.SESSION_TTL + 1
        with patch.dict(server.SESSIONS, {'old': old, 'active': active}, clear=True):
            server.prune_sessions()
            self.assertEqual(list(server.SESSIONS), ['active'])


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.port_patch = patch.object(server, 'PORT', cls.http.server_port)
        cls.port_patch.start()
        cls.log_patch = patch.object(server.Handler, 'log_message')
        cls.log_patch.start()
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()
        cls.port_patch.stop()
        cls.log_patch.stop()

    def setUp(self):
        server.SESSIONS.clear()

    def request(self, path, method='GET', body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.base + path, data=data, method=method,
                      headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.code, dict(response.headers), response.read()

    def test_invalid_input_never_runs_extractor(self):
        with patch('server.create_session') as extractor:
            for body in [{'videoId': 'bad'}, {'videoId': 'https://evil.com'}, {}, [], {'videoId': 123}]:
                code, _, data = self.request('/api/playback', 'POST', body)
                self.assertEqual(code, 400)
                self.assertEqual(json.loads(data)['code'], 'INVALID_INPUT')
            extractor.assert_not_called()

    def test_host_origin_and_source_files_rejected(self):
        for headers in [{'Origin': 'https://evil.example'}, {'Origin': 'null'},
                        {'Host': 'evil.example'}, {'Sec-Fetch-Site': 'cross-site'}]:
            self.assertEqual(self.request('/', headers=headers)[0], 403)
        for path in ['/server.py', '/README.md', '/tests/test_server.py', '/.git/config', '/assets/../server.py']:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/app.js')[0], 200)

    def test_create_proxy_and_delete_session(self):
        extracted = {'formats': [{'protocol': 'm3u8_native', 'manifest_url': SOURCE, 'http_headers': {}}]}
        result = subprocess.CompletedProcess([], 0, json.dumps(extracted), '')
        with patch('server.shutil.which', return_value='/bin/tool'), patch('server.subprocess.run', return_value=result) as run, patch('server.open_source', return_value=Response(MASTER.encode())):
            code, _, data = self.request('/api/playback', 'POST', {'videoId': 'M7lc1UVf-VE'})
        self.assertEqual(code, 201)
        args = run.call_args.args[0]
        self.assertIn('--ignore-config', args)
        self.assertIn('--no-cache-dir', args)
        self.assertIn('--skip-download', args)
        self.assertNotIn('shell', run.call_args.kwargs)
        info = json.loads(data)
        self.assertNotIn('googlevideo', data.decode())
        code, _, master = self.request(info['manifestUrl'])
        self.assertEqual(code, 200)
        state = server.SESSIONS[info['sessionId']]
        path = server.resource_url(state, 'https://r.googlevideo.com/media.mp4')
        with patch('server.open_source', return_value=Response(b'abc', 206, 'video/mp4', Content_Range='bytes 0-2/20', Accept_Ranges='bytes')) as upstream:
            code, headers, data = self.request(path, headers={'Range': 'bytes=0-2'})
        self.assertEqual((code, data, headers['Content-Range']), (206, b'abc', 'bytes 0-2/20'))
        self.assertEqual(upstream.call_args.args[2], 'bytes=0-2')
        self.assertEqual(self.request(path, headers={'Range': 'bytes=0-1,4-5'})[0], 416)
        for _ in range(2):
            self.assertEqual(self.request(f"/api/playback/{info['sessionId']}", 'DELETE')[0], 200)
        self.assertEqual(self.request(info['manifestUrl'])[0], 410)

    def test_dependency_failure_timeout_and_no_hls(self):
        with patch('server.shutil.which', return_value=None):
            self.assertEqual(self.request('/api/playback', 'POST', {'videoId': 'M7lc1UVf-VE'})[0], 503)
        with patch('server.shutil.which', return_value='/bin/tool'), patch('server.subprocess.run', side_effect=subprocess.TimeoutExpired('yt-dlp', 45)):
            self.assertEqual(self.request('/api/playback', 'POST', {'videoId': 'M7lc1UVf-VE'})[0], 504)
        result = subprocess.CompletedProcess([], 0, '{"formats": []}', '')
        with patch('server.shutil.which', return_value='/bin/tool'), patch('server.subprocess.run', return_value=result):
            self.assertEqual(self.request('/api/playback', 'POST', {'videoId': 'M7lc1UVf-VE'})[0], 415)

    def test_upstream_expiry_and_unsatisfied_range_preserved(self):
        state = session()
        path = server.resource_url(state, 'https://r.googlevideo.com/segment')
        server.SESSIONS[state['id']] = state
        for code in (403, 410, 416):
            headers = Message()
            headers['Content-Range'] = 'bytes */20'
            with patch('server.open_source', side_effect=HTTPError(SOURCE, code, 'error', headers, io.BytesIO())):
                status, returned, _ = self.request(path)
            self.assertEqual(status, code)
            if code == 416:
                self.assertEqual(returned['Content-Range'], 'bytes */20')


if __name__ == '__main__':
    unittest.main()
