"""HTTP-level tests for the /api/update endpoints in ui/server.py."""

import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from ui import server


class UpdateApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        self.token = server._WRITE_TOKEN

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, path, payload=None, headers=None):
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        request_headers = {'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=request_headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read().decode('utf-8'))

    def _auth(self, extra=None):
        headers = {'Origin': 'http://127.0.0.1',
                   server.WRITE_TOKEN_HEADER: self.token}
        headers.update(extra or {})
        return headers

    def test_get_state_uses_updater_state(self):
        payload = {'ok': True, 'status': 'up_to_date', 'current': '1.2.3',
                   'staged': {'ready': False}}
        with mock.patch.object(server.updater, 'state', return_value=payload):
            status, body = self._request('/api/update')
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)

    def test_get_state_real_shape_without_cache(self):
        with mock.patch.object(server.updater, 'STATE_FILE',
                               os.path.join(self.tmp.name, 'missing.json')):
            status, body = self._request('/api/update')
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'unknown')
        self.assertEqual(body['current'], server.updater.app_version())
        self.assertIn('staged', body)
        self.assertFalse(body['staged']['ready'])

    def test_version_and_health_include_app(self):
        with mock.patch.object(server.updater, 'app_version', return_value='9.9.9'):
            status, body = self._request('/api/version')
            self.assertEqual(status, 200)
            self.assertEqual(body['app'], '9.9.9')
            with mock.patch.object(server, 'db_health', return_value={'ok': True}):
                with mock.patch.object(server, 'lcu_health', return_value={'status': 'ok'}):
                    status, body = self._request('/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(body['app'], '9.9.9')

    def test_check_requires_token_with_origin(self):
        status, body = self._request('/api/update/check', {}, headers={'Origin': 'http://127.0.0.1'})
        self.assertEqual(status, 403)
        self.assertEqual(body['error'], 'bad_token')
        with mock.patch.object(server.updater, 'check', return_value={'ok': True,
                                                                      'status': 'up_to_date'}):
            status, body = self._request('/api/update/check', {}, headers=self._auth())
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'up_to_date')

    def test_check_passes_force_and_channel(self):
        calls = []

        def fake_check(force=False, channel='stable', timeout=10):
            calls.append({'force': force, 'channel': channel})
            return {'ok': True, 'status': 'up_to_date'}

        with mock.patch.object(server.updater, 'check', side_effect=fake_check):
            self._request('/api/update/check', {'channel': 'beta'}, headers=self._auth())
            self._request('/api/update/check', {'force': False, 'channel': 'beta'},
                          headers=self._auth())
        self.assertEqual(calls[0], {'force': True, 'channel': 'beta'})
        self.assertEqual(calls[1], {'force': False, 'channel': 'beta'})

    def test_check_accepts_empty_body(self):
        with mock.patch.object(server.updater, 'check',
                               return_value={'ok': True, 'status': 'up_to_date'}):
            req = urllib.request.Request(
                self.base + '/api/update/check', data=b'',
                headers={'Content-Type': 'application/json',
                         server.WRITE_TOKEN_HEADER: self.token,
                         'Origin': 'http://127.0.0.1'})
            with urllib.request.urlopen(req, timeout=5) as response:
                body = json.loads(response.read().decode('utf-8'))
        self.assertEqual(body['status'], 'up_to_date')

    def test_download_in_game_is_409(self):
        with mock.patch.object(server, 'build_state', return_value={'status': 'live'}):
            with mock.patch.object(server.updater, 'download') as download:
                status, body = self._request('/api/update/download', {}, headers=self._auth())
        self.assertEqual(status, 409)
        self.assertEqual(body, {'ok': False, 'error': 'in_game'})
        download.assert_not_called()

    def test_apply_in_game_is_409(self):
        with mock.patch.object(server, 'build_state', return_value={'status': 'live'}):
            with mock.patch.object(server.updater, 'apply') as apply_fn:
                status, body = self._request('/api/update/apply', {}, headers=self._auth())
        self.assertEqual(status, 409)
        self.assertEqual(body, {'ok': False, 'error': 'in_game'})
        apply_fn.assert_not_called()

    def test_busy_update_operation_is_409(self):
        self.assertTrue(server._UPDATE_LOCK.acquire(blocking=False))
        try:
            with mock.patch.object(server, 'build_state',
                                   return_value={'status': 'no_active_game'}):
                for path in ('/api/update/check', '/api/update/download', '/api/update/apply'):
                    status, body = self._request(path, {}, headers=self._auth())
                    self.assertEqual(status, 409)
                    self.assertEqual(body, {'ok': False, 'error': 'busy'})
        finally:
            server._UPDATE_LOCK.release()

    def test_download_happy_path(self):
        payload = {'ok': True, 'status': 'downloaded', 'path': '/tmp/x.zip',
                   'sha256': 'a' * 64, 'bytes': 3, 'version': 'v1.2.0', 'error': None}
        with mock.patch.object(server, 'build_state',
                               return_value={'status': 'no_active_game'}):
            with mock.patch.object(server.updater, 'download',
                                   return_value=payload) as download:
                status, body = self._request(
                    '/api/update/download', {'version': 'v1.2.0', 'channel': 'stable'},
                    headers=self._auth())
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(download.call_args.kwargs,
                         {'version': 'v1.2.0', 'channel': 'stable'})

    def test_apply_happy_path(self):
        payload = {'ok': True, 'status': 'started', 'helper': '/tmp/update_apply.ps1',
                   'logPath': '/tmp/update.log', 'error': None}
        with mock.patch.object(server, 'build_state',
                               return_value={'status': 'no_active_game'}):
            with mock.patch.object(server.updater, 'apply', return_value=payload) as apply_fn, \
                    mock.patch.object(server, 'request_shutdown') as shutdown:
                status, body = self._request('/api/update/apply', {},
                                             headers=self._auth())
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertIn('staged_path', apply_fn.call_args.kwargs)
        shutdown.assert_called_once()

    def test_get_update_log_endpoint(self):
        seen = []

        def fake_tail(limit=80):
            seen.append(limit)
            return ['one', 'two']

        with mock.patch.object(server.updater, 'log_tail', side_effect=fake_tail):
            status, body = self._request('/api/update/log?limit=2')
            self.assertEqual(status, 200)
            self.assertEqual(body['lines'], ['one', 'two'])
            self.assertEqual(body['count'], 2)
            self.assertIn('logPath', body)
            status, body = self._request('/api/update/log')
            self.assertEqual(status, 200)
        self.assertEqual(seen, [2, 80])

    def test_unknown_update_post_is_404(self):
        status, body = self._request('/api/update/nope', {}, headers=self._auth())
        self.assertEqual(status, 404)
        self.assertEqual(body['error'], 'not_found')


if __name__ == '__main__':
    unittest.main()
