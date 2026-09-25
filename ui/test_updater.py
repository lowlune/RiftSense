"""Tests for ui/updater.py. No network: internal HTTP helpers are mocked."""

import hashlib
import json
import os
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from ui import updater


def atom_feed(entries):
    """entries: list of (tag, updated-iso)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<feed xmlns="%s">' % updater.ATOM_NS,
             '<title>Release notes from RiftSense</title>']
    for tag, updated in entries:
        parts.append('<entry>')
        parts.append('<id>tag:github.com,2008:Repository/1/%s</id>' % tag)
        parts.append('<updated>%s</updated>' % updated)
        parts.append('<link rel="alternate" type="text/html" '
                     'href="https://github.com/%s/releases/tag/%s"/>' % (updater.REPO, tag))
        parts.append('<title>%s</title>' % tag)
        parts.append('</entry>')
    parts.append('</feed>')
    return '\n'.join(parts).encode('utf-8')


def release_payload(tag='v1.2.0', digest=None, body='notes', prerelease=False):
    data = b'PK\x03\x04fake-zip-bytes'
    if digest is None:
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    return data, {
        'tag_name': tag,
        'name': 'RiftSense %s' % tag,
        'draft': False,
        'prerelease': prerelease,
        'body': body,
        'published_at': '2026-01-03T00:00:00Z',
        'assets': [
            {'name': updater.ASSET_NAME, 'size': len(data), 'digest': digest,
             'browser_download_url': 'https://example.test/%s/%s' % (tag, updater.ASSET_NAME)},
            {'name': updater.SUMS_NAME, 'size': 90, 'digest': None,
             'browser_download_url': 'https://example.test/%s/%s' % (tag, updater.SUMS_NAME)},
        ],
    }


class FakeHTTP:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, headers=None, timeout=10, max_bytes=None):
        self.calls.append({'url': url, 'headers': dict(headers or {}),
                           'timeout': timeout, 'max_bytes': max_bytes})
        for matcher, code, resp_headers, body in self.responses:
            if callable(matcher):
                if matcher(url):
                    return code, resp_headers, body
            elif matcher in url:
                return code, resp_headers, body
        raise AssertionError('unexpected HTTP GET: %s' % url)


class VersionTests(unittest.TestCase):
    def test_numeric_ordering(self):
        self.assertEqual(updater.compare_versions('1.9.0', '1.10.0'), -1)
        self.assertEqual(updater.compare_versions('1.10.0', '1.9.9'), 1)
        self.assertEqual(updater.compare_versions('1.9', '1.9.0'), 0)
        self.assertTrue(updater.is_newer('1.10.0', '1.9.9'))
        self.assertFalse(updater.is_newer('1.9.0', '1.10.0'))

    def test_prerelease_ordering(self):
        ordered = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta',
                   '1.0.0-beta', '1.0.0-beta.2', '1.0.0-beta.11',
                   '1.0.0-rc.1', '1.0.0']
        for older, newer in zip(ordered, ordered[1:]):
            self.assertEqual(updater.compare_versions(older, newer), -1,
                             '%s should be older than %s' % (older, newer))
            self.assertEqual(updater.compare_versions(newer, older), 1)

    def test_build_metadata_and_prefixes_ignored(self):
        self.assertEqual(updater.compare_versions('v1.2.3', '1.2.3'), 0)
        self.assertEqual(updater.compare_versions('1.2.3+build.7', 'v1.2.3'), 0)
        self.assertIsNone(updater.compare_versions('nightly', '1.0.0'))
        self.assertFalse(updater.is_newer('nightly', '1.0.0'))

    def test_app_version_reads_and_strips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'VERSION')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('  v1.4.2\n')
            with mock.patch.object(updater, 'VERSION_FILE', path):
                self.assertEqual(updater.app_version(), '1.4.2')

    def test_app_version_fallback(self):
        with mock.patch.object(updater, 'VERSION_FILE',
                               os.path.join(tempfile.gettempdir(), 'no-such-version-file')):
            self.assertEqual(updater.app_version(), '0.0.0')


class AtomTests(unittest.TestCase):
    FEED = atom_feed([('v1.3.0-beta.2', '2026-01-04T00:00:00Z'),
                      ('v1.2.0', '2026-01-03T00:00:00Z'),
                      ('v1.1.0', '2026-01-01T00:00:00Z')])

    def test_parse_atom_entries(self):
        entries = updater.parse_atom(self.FEED.decode('utf-8'))
        self.assertEqual([e['tag'] for e in entries],
                         ['v1.3.0-beta.2', 'v1.2.0', 'v1.1.0'])
        self.assertEqual(entries[0]['publishedAt'], '2026-01-04T00:00:00Z')
        self.assertIn('/releases/tag/v1.3.0-beta.2', entries[0]['url'])

    def test_parse_atom_rejects_garbage(self):
        self.assertEqual(updater.parse_atom('<not-xml'), [])

    def test_channel_filtering(self):
        entries = updater.parse_atom(self.FEED.decode('utf-8'))
        self.assertEqual(updater._select_release(entries, 'stable')['tag'], 'v1.2.0')
        self.assertEqual(updater._select_release(entries, 'beta')['tag'], 'v1.3.0-beta.2')
        only_beta = updater.parse_atom(atom_feed(
            [('v9.0.0-beta.1', '2026-01-05T00:00:00Z')]).decode('utf-8'))
        self.assertIsNone(updater._select_release(only_beta, 'stable'))

    def test_release_tag_extraction(self):
        self.assertEqual(updater.release_tag('tag:github.com,2008:Repository/1/v1.2.3'),
                         'v1.2.3')
        self.assertEqual(updater.release_tag('v1.2.3'), 'v1.2.3')
        self.assertEqual(updater.release_tag('1.2.3'), '1.2.3')
        self.assertEqual(updater.release_tag('RiftSense v1.2.3 is out'), 'v1.2.3')
        self.assertIsNone(updater.release_tag('no version here'))


class DigestTests(unittest.TestCase):
    def test_parse_sums(self):
        sha = 'a' * 64
        text = '# comment\n%s  %s\n%s *%s\n' % (sha, updater.ASSET_NAME, 'b' * 64, 'other.zip')
        sums = updater._parse_sums(text)
        self.assertEqual(sums[updater.ASSET_NAME], sha)
        self.assertEqual(sums['other.zip'], 'b' * 64)

    def test_verify_digest_matches_and_mismatches(self):
        sha = 'c' * 64
        self.assertIsNone(updater._verify_digest(sha, 'sha256:' + sha, None, updater.ASSET_NAME))
        sums = {updater.ASSET_NAME: sha}
        self.assertIsNone(updater._verify_digest(sha, None, sums, updater.ASSET_NAME))
        self.assertIsNotNone(updater._verify_digest('d' * 64, 'sha256:' + sha, None,
                                                    updater.ASSET_NAME))
        self.assertIsNotNone(updater._verify_digest(sha, 'sha256:' + 'd' * 64, None,
                                                    updater.ASSET_NAME))
        self.assertIsNotNone(updater._verify_digest(sha, None,
                                                    {updater.ASSET_NAME: 'd' * 64},
                                                    updater.ASSET_NAME))

    def test_verify_digest_fails_closed_without_checksums(self):
        self.assertIsNotNone(updater._verify_digest('e' * 64, None, {}, updater.ASSET_NAME))


class UpdaterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = {name: getattr(updater, name) for name in (
            'VERSION_FILE', 'UPDATE_DIR', 'STAGED_DIR', 'LOG_DIR', 'LOG_FILE',
            'STATE_FILE')}
        updater.UPDATE_DIR = os.path.join(self.tmp.name, '.update')
        updater.STAGED_DIR = os.path.join(updater.UPDATE_DIR, 'staged')
        updater.LOG_DIR = os.path.join(updater.UPDATE_DIR, 'logs')
        updater.LOG_FILE = os.path.join(updater.LOG_DIR, 'update.log')
        updater.STATE_FILE = os.path.join(self.tmp.name, 'update_state.json')
        updater.VERSION_FILE = os.path.join(self.tmp.name, 'VERSION')
        with open(updater.VERSION_FILE, 'w', encoding='utf-8') as f:
            f.write('1.1.0\n')

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(updater, name, value)
        self.tmp.cleanup()

    def _write_cached_check(self, status='update_available', latest='1.2.0',
                            channel='stable', asset=None, ts=None,
                            atom_etag='"etag-atom"'):
        check = {
            'ok': status in ('up_to_date', 'update_available'),
            'status': status,
            'current': '1.1.0',
            'latest': latest,
            'channel': channel,
            'asset': asset,
            'notes': 'cached notes',
            'publishedAt': '2026-01-02T00:00:00Z',
            'checkedAt': '2026-01-02T00:00:00Z',
            'source': 'rest',
            'error': None,
        }
        updater._write_state({
            'schema': 1,
            'last_check': check,
            'last_ts': time.time() if ts is None else ts,
            'etags': {'atom': atom_etag},
            'staged': {},
        })

    def _example_asset(self, data=None, digest=None, size=None, tag='v1.2.0'):
        if data is None:
            data = b'PK\x03\x04fake-zip-bytes'
        if digest is None:
            digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        return data, {
            'name': updater.ASSET_NAME,
            'size': len(data) if size is None else size,
            'digest': digest,
            'url': 'https://example.test/releases/download/%s/%s' % (tag, updater.ASSET_NAME),
        }


class CheckTests(UpdaterTestCase):
    def test_up_to_date_from_atom_without_rest(self):
        fake = FakeHTTP([('releases.atom', 200, {'ETag': '"a1"'},
                          atom_feed([('v1.1.0', '2026-01-01T00:00:00Z')]))])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'up_to_date')
        self.assertEqual(result['source'], 'atom')
        self.assertEqual(result['latest'], '1.1.0')
        self.assertEqual(len(fake.calls), 1)

    def test_update_available_fetches_rest_details_and_etags(self):
        _data, release = release_payload(tag='v1.2.0')
        fake = FakeHTTP([
            ('releases.atom', 200, {'ETag': '"a1"'},
             atom_feed([('v1.2.0', '2026-01-03T00:00:00Z')])),
            ('api.github.com', 200, {'ETag': '"r1"'}, json.dumps(release).encode('utf-8')),
        ])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'update_available')
        self.assertEqual(result['latest'], 'v1.2.0')
        self.assertEqual(result['source'], 'rest')
        self.assertEqual(result['notes'], 'notes')
        self.assertEqual(result['asset']['name'], updater.ASSET_NAME)
        self.assertTrue(result['asset']['digest'].startswith('sha256:'))
        stored = updater._read_state()
        self.assertEqual(stored['etags']['atom'], '"a1"')
        self.assertEqual(stored['etags']['rest'], '"r1"')

    def test_cache_used_when_not_forced(self):
        self._write_cached_check(status='up_to_date', latest='1.1.0')

        def boom(*args, **kwargs):
            raise AssertionError('network must not be touched')

        with mock.patch.object(updater, '_http_get', boom):
            result = updater.check(force=False)
        self.assertEqual(result['status'], 'up_to_date')
        self.assertEqual(result['source'], 'cache')
        self.assertEqual(result['current'], '1.1.0')

    def test_cache_recomputes_up_to_date_after_local_upgrade(self):
        self._write_cached_check(status='update_available', latest='1.1.0')

        def boom(*args, **kwargs):
            raise AssertionError('network must not be touched')

        with mock.patch.object(updater, '_http_get', boom):
            result = updater.check(force=False)
        self.assertEqual(result['status'], 'up_to_date')
        self.assertEqual(result['source'], 'cache')
        self.assertEqual(result['latest'], '1.1.0')
        self.assertIsNone(result['asset'])

    def test_stale_cache_is_ignored(self):
        self._write_cached_check(status='up_to_date', latest='1.1.0',
                                 ts=time.time() - updater.CHECK_TTL - 10)
        fake = FakeHTTP([('releases.atom', 200, {}, atom_feed(
            [('v1.1.0', '2026-01-01T00:00:00Z')]))])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=False)
        self.assertEqual(result['source'], 'atom')
        self.assertEqual(len(fake.calls), 1)

    def test_etag_304_uses_cache(self):
        self._write_cached_check(status='update_available', latest='1.2.0',
                                 asset={'name': updater.ASSET_NAME, 'size': 3,
                                        'digest': 'sha256:' + 'a' * 64,
                                        'url': 'https://example.test/a.zip'})
        fake = FakeHTTP([('releases.atom', 304, {}, b'')])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=True)
        self.assertEqual(result['status'], 'update_available')
        self.assertEqual(result['latest'], '1.2.0')
        self.assertEqual(result['source'], 'cache')
        self.assertEqual(fake.calls[0]['headers'].get('If-None-Match'), '"etag-atom"')
        self.assertEqual(result['asset']['url'], 'https://example.test/a.zip')

    def test_stable_channel_ignores_prerelease(self):
        fake = FakeHTTP([('releases.atom', 200, {}, atom_feed(
            [('v1.9.0-beta.1', '2026-01-05T00:00:00Z')]))])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=True, channel='stable')
        self.assertEqual(result['status'], 'up_to_date')
        self.assertEqual(len(fake.calls), 1)

    def test_beta_channel_considers_prerelease_via_rest_list(self):
        _data, release = release_payload(tag='v1.9.0-beta.1', prerelease=True)
        fake = FakeHTTP([
            ('releases.atom', 200, {}, atom_feed(
                [('v1.9.0-beta.1', '2026-01-05T00:00:00Z')])),
            ('api.github.com', 200, {}, json.dumps([release]).encode('utf-8')),
        ])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=True, channel='beta')
        self.assertEqual(result['status'], 'update_available')
        self.assertEqual(result['latest'], 'v1.9.0-beta.1')
        self.assertEqual(result['channel'], 'beta')
        self.assertEqual(result['source'], 'rest')
        self.assertIn('releases?per_page=20', fake.calls[1]['url'])

    def test_rate_limited(self):
        fake = FakeHTTP([('releases.atom', 429, {'x-ratelimit-reset': '1700000000'}, b'')])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'rate_limited')
        self.assertIsNotNone(result['error'])

    def test_network_failure_is_check_failed(self):
        def boom(*args, **kwargs):
            raise OSError('offline')

        with mock.patch.object(updater, '_http_get', boom):
            result = updater.check(force=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'check_failed')
        self.assertIn('offline', result['error'])

    def test_invalid_channel(self):
        result = updater.check(force=True, channel='nightly')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'check_failed')
        self.assertIn('unknown channel', result['error'])

    def test_rest_fallback_when_atom_fails(self):
        _data, release = release_payload(tag='v1.2.0')
        fake = FakeHTTP([
            ('releases.atom', 500, {}, b''),
            ('api.github.com', 200, {}, json.dumps(release).encode('utf-8')),
        ])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.check(force=True)
        self.assertEqual(result['status'], 'update_available')
        self.assertEqual(result['source'], 'rest')
        self.assertEqual(result['latest'], 'v1.2.0')


class DownloadTests(UpdaterTestCase):
    def _cached_asset(self, data, digest=None, size=None):
        data, asset = self._example_asset(data=data, digest=digest, size=size)
        self._write_cached_check(latest='v1.2.0', asset=asset)
        return data, asset

    def test_download_verifies_and_stages(self):
        data, _asset = self._cached_asset(b'PK\x03\x04' + b'x' * 64)
        sums = ('%s  %s\n' % (hashlib.sha256(data).hexdigest(), updater.ASSET_NAME)).encode()
        fake = FakeHTTP([
            (updater.SUMS_NAME, 200, {}, sums),
            ('.zip', 200, {}, data),
        ])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.download()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'downloaded')
        self.assertEqual(result['version'], 'v1.2.0')
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['bytes'], len(data))
        self.assertTrue(os.path.isfile(result['path']))
        with open(result['path'], 'rb') as f:
            self.assertEqual(f.read(), data)
        staged = updater._read_state()['staged']
        self.assertTrue(staged['ready'])
        self.assertEqual(staged['version'], 'v1.2.0')
        leftovers = [name for name in os.listdir(updater.UPDATE_DIR)
                     if name.startswith('tmp-download-')]
        self.assertEqual(leftovers, [])

    def test_download_rejects_digest_mismatch(self):
        data, _asset = self._cached_asset(b'PK\x03\x04' + b'y' * 64,
                                          digest='sha256:' + '0' * 64)
        fake = FakeHTTP([
            (updater.SUMS_NAME, 404, {}, b''),
            ('.zip', 200, {}, data),
        ])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.download()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'verify_failed')
        self.assertIn('mismatch', result['error'])
        self.assertFalse(os.path.exists(os.path.join(updater.STAGED_DIR, updater.ASSET_NAME)))

    def test_download_verifies_with_sums_when_digest_missing(self):
        data = b'PK\x03\x04' + b'z' * 32
        _data, asset = self._example_asset(data=data, digest=None)
        asset['digest'] = None
        self._write_cached_check(latest='v1.2.0', asset=asset)
        sums = ('%s  %s\n' % (hashlib.sha256(data).hexdigest(), updater.ASSET_NAME)).encode()
        fake = FakeHTTP([
            (updater.SUMS_NAME, 200, {}, sums),
            ('.zip', 200, {}, data),
        ])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.download()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'downloaded')

    def test_download_fails_closed_without_checksum(self):
        data = b'PK\x03\x04' + b'w' * 16
        _data, asset = self._example_asset(data=data, digest=None)
        asset['digest'] = None
        self._write_cached_check(latest='v1.2.0', asset=asset)
        fake = FakeHTTP([
            (updater.SUMS_NAME, 404, {}, b''),
            ('.zip', 200, {}, data),
        ])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.download()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'verify_failed')
        self.assertIn('no published checksum', result['error'])

    def test_download_rejects_size_mismatch(self):
        data, _asset = self._cached_asset(b'PK\x03\x04' + b'v' * 8, size=9999)
        fake = FakeHTTP([('.zip', 200, {}, data)])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.download()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'verify_failed')
        self.assertIn('size mismatch', result['error'])

    def test_download_enforces_size_cap(self):
        data, _asset = self._cached_asset(b'PK\x03\x04' + b'u' * 64)
        fake = FakeHTTP([('.zip', 200, {}, data)])
        with mock.patch.object(updater, 'MAX_DOWNLOAD_BYTES', 8):
            with mock.patch.object(updater, '_http_get', fake):
                result = updater.download()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'download_failed')

    def test_download_no_update_for_current_or_older(self):
        result = updater.download(version='1.1.0')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_update')
        result = updater.download(version='1.0.0')
        self.assertEqual(result['status'], 'no_update')

    def test_download_without_known_version(self):
        result = updater.download()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_update')

    def test_download_rejects_cached_check_from_other_channel(self):
        self._write_cached_check(latest='v1.9.0-beta.1', channel='beta')
        result = updater.download(channel='stable')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_update')
        self.assertIn('channel', result['error'])

    def test_download_invalid_channel(self):
        result = updater.download(version='1.2.0', channel='nightly')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'unsupported')

    def test_download_failure_status(self):
        self._cached_asset(b'PK\x03\x04' + b't' * 8)
        fake = FakeHTTP([('.zip', 500, {}, b'')])
        with mock.patch.object(updater, '_http_get', fake):
            result = updater.download()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'download_failed')


class ApplyTests(UpdaterTestCase):
    @unittest.skipIf(os.name == 'nt', 'non-Windows expectation')
    def test_apply_unsupported_off_windows(self):
        result = updater.apply()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'unsupported')
        for key in ('ok', 'status', 'helper', 'logPath', 'error'):
            self.assertIn(key, result)
        self.assertEqual(result['logPath'], updater.LOG_FILE)


class StateTests(UpdaterTestCase):
    def test_state_unknown_without_cache(self):
        result = updater.state()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['current'], '1.1.0')
        self.assertFalse(result['staged']['ready'])

    def test_state_includes_cached_check_and_staged(self):
        data, asset = self._example_asset()
        self._write_cached_check(latest='v1.2.0', asset=asset)
        os.makedirs(updater.STAGED_DIR, exist_ok=True)
        staged_path = os.path.join(updater.STAGED_DIR, updater.ASSET_NAME)
        with open(staged_path, 'wb') as f:
            f.write(data)
        stored = updater._read_state()
        stored['staged'] = {'ready': True, 'version': 'v1.2.0', 'path': staged_path,
                            'sha256': hashlib.sha256(data).hexdigest(),
                            'bytes': len(data), 'stagedAt': '2026-01-03T00:00:00Z'}
        updater._write_state(stored)
        result = updater.state()
        self.assertEqual(result['status'], 'update_available')
        self.assertEqual(result['current'], '1.1.0')
        self.assertTrue(result['staged']['ready'])
        self.assertEqual(result['staged']['version'], 'v1.2.0')

    def test_log_tail(self):
        os.makedirs(updater.LOG_DIR, exist_ok=True)
        with open(updater.LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join('line %d' % i for i in range(5)) + '\n')
        self.assertEqual(updater.log_tail(2), ['line 3', 'line 4'])
        self.assertEqual(updater.log_tail(0), [])
        self.assertEqual(len(updater.log_tail()), 5)
        self.assertEqual(updater.log_tail(limit='bogus'), ['line 0', 'line 1', 'line 2',
                                                           'line 3', 'line 4'])

    def test_log_tail_missing_file(self):
        self.assertEqual(updater.log_tail(), [])


class AtomicStateTests(UpdaterTestCase):
    def test_write_leaves_no_temp_files_and_replaces(self):
        updater._write_state({'schema': 1, 'value': [1, 2, 3]})
        with open(updater.STATE_FILE, 'r', encoding='utf-8') as f:
            self.assertEqual(json.load(f)['value'], [1, 2, 3])
        leftovers = [name for name in os.listdir(self.tmp.name)
                     if name.startswith('.tmp_update_')]
        self.assertEqual(leftovers, [])

    def test_failed_replace_cleans_up_temp_file(self):
        with mock.patch.object(updater.os, 'replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                updater._write_state({'schema': 1})
        leftovers = [name for name in os.listdir(self.tmp.name)
                     if name.startswith('.tmp_update_')]
        self.assertEqual(leftovers, [])


class StagedExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self._orig = {name: getattr(updater, name) for name in (
            'ROOT', 'UPDATE_DIR', 'STAGED_DIR', 'EXTRACTED_DIR', 'LOG_DIR',
            'LOG_FILE', 'STATE_FILE', 'VERSION_FILE', 'HELPER_FILE')}
        updater.ROOT = root
        updater.UPDATE_DIR = os.path.join(root, '.update')
        updater.STAGED_DIR = os.path.join(updater.UPDATE_DIR, 'staged')
        updater.EXTRACTED_DIR = os.path.join(updater.UPDATE_DIR, 'extracted')
        updater.LOG_DIR = os.path.join(updater.UPDATE_DIR, 'logs')
        updater.LOG_FILE = os.path.join(updater.LOG_DIR, 'update.log')
        updater.STATE_FILE = os.path.join(root, 'update_state.json')
        updater.VERSION_FILE = os.path.join(root, 'VERSION')
        updater.HELPER_FILE = os.path.join(root, 'update_apply.ps1')

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(updater, name, value)
        self.tmp.cleanup()

    def _zip(self, members, name='pkg.zip'):
        path = os.path.join(self.tmp.name, name)
        with zipfile.ZipFile(path, 'w') as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    def test_extract_strips_single_top_level_prefix(self):
        archive = self._zip({
            'RiftSense/VERSION': '1.2.0\n',
            'RiftSense/ui/server.py': 'x',
            'RiftSense/README.md': 'r',
        })
        payload, version = updater._extract_staged(archive)
        self.assertEqual(version, '1.2.0')
        self.assertTrue(os.path.isfile(os.path.join(payload, 'VERSION')))
        self.assertTrue(os.path.isfile(os.path.join(payload, 'ui', 'server.py')))

    def test_extract_flat_archive_without_prefix(self):
        archive = self._zip({'VERSION': '2.0.0', 'ui/server.py': 'x'})
        payload, version = updater._extract_staged(archive)
        self.assertEqual(version, '2.0.0')
        self.assertTrue(os.path.isfile(os.path.join(payload, 'ui', 'server.py')))

    def test_extract_mixed_archive_still_finds_prefixed_version(self):
        archive = self._zip({'SHA256SUMS.txt': 'x',
                             'RiftSense/VERSION': '2.1.0',
                             'RiftSense/ui/server.py': 'x'})
        payload, version = updater._extract_staged(archive)
        self.assertEqual(version, '2.1.0')
        self.assertTrue(os.path.isfile(os.path.join(payload, 'ui', 'server.py')))

    def test_extract_moves_into_place(self):
        archive = self._zip({'VERSION': '1.5.0', 'AutoCoach.ps1': 'x'})
        payload, _ = updater._extract_staged(archive)
        self.assertTrue(payload.startswith(updater.EXTRACTED_DIR + os.sep))
        self.assertTrue(os.path.isfile(os.path.join(payload, 'AutoCoach.ps1')))

    def test_rejects_path_traversal(self):
        archive = self._zip({'RiftSense/VERSION': '1.0.0',
                             'RiftSense/../evil.txt': 'x'})
        with self.assertRaises(updater.UpdateError):
            updater._extract_staged(archive)

    def test_rejects_absolute_member(self):
        archive = self._zip({'/etc/passwd': 'x', 'VERSION': '1.0.0'})
        with self.assertRaises(updater.UpdateError):
            updater._extract_staged(archive)

    def test_rejects_symlink_member(self):
        path = os.path.join(self.tmp.name, 'sym.zip')
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('VERSION', '1.0.0')
            info = zipfile.ZipInfo('link')
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, 'target')
        with self.assertRaises(updater.UpdateError):
            updater._extract_staged(path)

    def test_rejects_missing_version(self):
        archive = self._zip({'RiftSense/README.md': 'x'})
        with self.assertRaises(updater.UpdateError):
            updater._extract_staged(archive)

    def test_rejects_version_mismatch(self):
        archive = self._zip({'VERSION': '1.0.0', 'ui/server.py': 'x'})
        with self.assertRaises(updater.UpdateError):
            updater._extract_staged(archive, expected_version='2.0.0')

    def test_apply_directory_payload_passes_payload_dir_and_restart(self):
        payload = os.path.join(self.tmp.name, 'payload')
        os.makedirs(os.path.join(payload, 'ui'))
        with open(os.path.join(payload, 'VERSION'), 'w', encoding='utf-8') as f:
            f.write('9.9.9')
        with open(os.path.join(payload, 'ui', 'server.py'), 'w', encoding='utf-8') as f:
            f.write('x')
        with open(updater.HELPER_FILE, 'w', encoding='utf-8') as f:
            f.write('# fake helper')
        with mock.patch.object(updater, 'app_version', return_value='1.0.0'), \
                mock.patch.object(updater.subprocess, 'Popen') as popen:
            result = updater._apply(payload)
        self.assertTrue(result['ok'])
        args = popen.call_args[0][0]
        self.assertIn(payload, args)
        self.assertIn('-Restart', args)
        self.assertIn('-WaitPid', args)
        self.assertFalse(updater.state()['staged']['ready'],
                         'consumed staged state must be cleared after apply')

    def test_log_tail_reads_helper_dated_logs(self):
        os.makedirs(updater.LOG_DIR, exist_ok=True)
        with open(os.path.join(updater.LOG_DIR, 'update.log'), 'w', encoding='utf-8') as f:
            f.write('python line 1\npython line 2\n')
        dated = os.path.join(updater.LOG_DIR, 'update-20260101.log')
        with open(dated, 'w', encoding='utf-8') as f:
            f.write('helper line 1\nhelper line 2\n')
        os.utime(dated, (time.time() + 5, time.time() + 5))
        lines = updater.log_tail(3)
        self.assertEqual(lines, ['python line 2', 'helper line 1', 'helper line 2'])

    def test_apply_extracts_zip_before_spawning_helper(self):
        archive = self._zip({'RiftSense/VERSION': '9.9.9',
                             'RiftSense/ui/server.py': 'x'})
        with open(updater.HELPER_FILE, 'w', encoding='utf-8') as f:
            f.write('# fake helper')
        with mock.patch.object(updater, 'app_version', return_value='1.0.0'), \
                mock.patch.object(updater.subprocess, 'Popen') as popen:
            result = updater._apply(archive)
        self.assertTrue(result['ok'])
        args = popen.call_args[0][0]
        staged_arg = args[args.index('-Staged') + 1]
        self.assertTrue(os.path.isfile(os.path.join(staged_arg, 'VERSION')))
        self.assertFalse(staged_arg.endswith('.zip'))


if __name__ == '__main__':
    unittest.main()
