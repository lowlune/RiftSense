"""RiftSense update checker, downloader, and apply launcher (stdlib only).

Public interface (stable; ui/server.py and AutoCoach code against it):

    REPO, ASSET_NAME
    app_version() -> str
    check(force=False, channel='stable', timeout=10) -> dict
    download(version=None, channel='stable', timeout=60) -> dict
    apply(staged_path=None) -> dict
    state() -> dict
    log_tail(limit=80) -> list[str]

Discovery is Atom-first (no REST rate limit); the REST API is consulted only
when release notes, asset details, or digests are needed. TLS is always
verified here; never reuse the cert-disabled context server.py needs for
Riot's self-signed LCU API.
"""

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
VERSION_FILE = os.path.join(ROOT, 'VERSION')
UPDATE_DIR = os.path.join(ROOT, '.update')
STAGED_DIR = os.path.join(UPDATE_DIR, 'staged')
LOG_DIR = os.path.join(UPDATE_DIR, 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'update.log')
STATE_FILE = os.path.join(ROOT, 'update_state.json')
HELPER_FILE = os.path.join(BASE, 'update_apply.ps1')

REPO = 'lowlune/RiftSense'
ASSET_NAME = 'RiftSense-win-x64.zip'
SUMS_NAME = 'SHA256SUMS.txt'
ATOM_URL = 'https://github.com/%s/releases.atom' % REPO
REST_LATEST_URL = 'https://api.github.com/repos/%s/releases/latest' % REPO
REST_LIST_URL = 'https://api.github.com/repos/%s/releases?per_page=20' % REPO
DOWNLOAD_BASE = 'https://github.com/%s/releases/download' % REPO
GITHUB_API_VERSION = '2022-11-28'
ATOM_NS = 'http://www.w3.org/2005/Atom'
CHANNELS = ('stable', 'beta')
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
CHECK_TTL = 6 * 60 * 60
STATE_SCHEMA = 1

_SEMVER_RE = re.compile(
    r'^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?'
    r'(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$')
_HEX_RE = re.compile(r'^[0-9a-f]{64}$')
_TAG_RE = re.compile(r'v?\d+(?:\.\d+){1,3}(?:-[0-9A-Za-z.\-]+)?')


class DownloadTooLarge(Exception):
    pass


# ---------------------------------------------------------------- versioning

def app_version():
    try:
        with open(VERSION_FILE, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
    except OSError:
        return '0.0.0'
    value = raw.lstrip('vV').strip()
    return value or '0.0.0'


def parse_version(text):
    if not isinstance(text, str):
        return None
    match = _SEMVER_RE.match(text.strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    return major, minor, patch, match.group(4)


def _prerelease_key(pre):
    if pre is None:
        return (1,)
    parts = []
    for ident in pre.split('.'):
        if ident.isdigit():
            parts.append((0, int(ident), ''))
        else:
            parts.append((1, 0, ident))
    return (0, tuple(parts))


def version_key(text):
    parsed = parse_version(text)
    if parsed is None:
        return None
    major, minor, patch, pre = parsed
    return major, minor, patch, _prerelease_key(pre)


def compare_versions(a, b):
    key_a = version_key(a)
    key_b = version_key(b)
    if key_a is None or key_b is None:
        return None
    if key_a < key_b:
        return -1
    if key_a > key_b:
        return 1
    return 0


def is_newer(candidate, current):
    return compare_versions(candidate, current) == 1


# -------------------------------------------------------------------- state

def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _read_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path, obj):
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp_update_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_state(state):
    _atomic_write_json(STATE_FILE, state)


def _cache_fresh(stored, channel=None):
    last = stored.get('last_check') if isinstance(stored, dict) else None
    stamp = stored.get('last_ts') if isinstance(stored, dict) else None
    if not isinstance(last, dict) or not isinstance(stamp, (int, float)):
        return False
    if last.get('status') not in ('up_to_date', 'update_available'):
        return False
    if channel is not None and last.get('channel') != channel:
        return False
    return (time.time() - stamp) < CHECK_TTL


def _save(stored, result, atom_etag=None, rest_etag=None):
    doc = dict(stored) if isinstance(stored, dict) else {}
    doc['schema'] = STATE_SCHEMA
    doc['last_check'] = result
    doc['last_ts'] = time.time()
    etags = dict(doc.get('etags') or {})
    if atom_etag:
        etags['atom'] = atom_etag
    if rest_etag:
        etags['rest'] = rest_etag
    doc['etags'] = etags
    _write_state(doc)


def _result(ok, status, channel, current, latest=None, asset=None, notes=None,
            publishedAt=None, source=None, error=None):
    return {
        'ok': bool(ok),
        'status': status,
        'current': current,
        'latest': latest,
        'channel': channel,
        'asset': asset,
        'notes': notes,
        'publishedAt': publishedAt,
        'checkedAt': _now_iso(),
        'source': source,
        'error': error,
    }


# --------------------------------------------------------------------- http

def _tls_context():
    return ssl.create_default_context()


def _read_capped(stream, max_bytes=None):
    if max_bytes is None:
        return stream.read()
    chunks = []
    total = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DownloadTooLarge('response exceeds %d bytes' % max_bytes)
        chunks.append(chunk)
    return b''.join(chunks)


def _http_get(url, headers=None, timeout=10, max_bytes=None):
    """Return (status, headers, body). Never raises for HTTP status codes."""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_tls_context()) as response:
            body = _read_capped(response, max_bytes)
            return response.status, response.headers, body
    except urllib.error.HTTPError as ex:
        try:
            body = _read_capped(ex, max_bytes)
        except Exception:
            body = b''
        return ex.code, ex.headers, body


def _user_agent():
    return 'RiftSense/%s' % app_version()


# ----------------------------------------------------------------- awa atom

def _xml_text(node):
    return (node.text or '').strip() if node is not None else ''


def release_tag(raw):
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.startswith('tag:'):
        value = value.rsplit(':', 1)[-1]
    match = _TAG_RE.search(value)
    return match.group(0) if match else None


def parse_atom(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries = []
    for entry in root.findall('{%s}entry' % ATOM_NS):
        tag = release_tag(_xml_text(entry.find('{%s}id' % ATOM_NS)))
        if not tag:
            tag = release_tag(_xml_text(entry.find('{%s}title' % ATOM_NS)))
        if not tag:
            continue
        link = None
        for node in entry.findall('{%s}link' % ATOM_NS):
            if node.get('rel') in (None, 'alternate'):
                link = node.get('href')
                break
        entries.append({
            'tag': tag,
            'publishedAt': _xml_text(entry.find('{%s}updated' % ATOM_NS)) or None,
            'url': link,
        })
    return entries


def _matches_channel(tag, channel):
    if channel == 'beta':
        return True
    return '-' not in tag.lstrip('vV')


def _select_release(entries, channel):
    best = None
    for entry in entries:
        tag = entry.get('tag')
        if not tag or not _matches_channel(tag, channel):
            continue
        if version_key(tag) is None:
            continue
        if best is None or is_newer(tag, best['tag']):
            best = entry
    return best


def _fetch_atom(etag, timeout):
    headers = {
        'Accept': 'application/atom+xml, application/xml;q=0.9, */*;q=0.8',
        'User-Agent': _user_agent(),
    }
    if etag:
        headers['If-None-Match'] = etag
    try:
        code, resp_headers, body = _http_get(ATOM_URL, headers=headers, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError) as ex:
        return {'code': None, 'entries': None, 'etag': etag, 'error': str(ex)}
    new_etag = (resp_headers.get('ETag') if resp_headers else None) or etag
    if code == 304:
        return {'code': 304, 'entries': None, 'etag': new_etag, 'error': None}
    if code != 200:
        return {'code': code, 'entries': None, 'etag': new_etag,
                'error': 'atom HTTP %s' % code}
    try:
        entries = parse_atom(body.decode('utf-8', 'replace'))
    except Exception as ex:
        return {'code': code, 'entries': None, 'etag': new_etag,
                'error': 'atom parse failed: %s' % ex}
    return {'code': 200, 'entries': entries, 'etag': new_etag, 'error': None}


# ------------------------------------------------------------------- REST

def _pick_rest_release(data, channel):
    if isinstance(data, dict):
        releases = [data]
    elif isinstance(data, list):
        releases = [rel for rel in data if isinstance(rel, dict)]
    else:
        return None
    best = None
    for rel in releases:
        if rel.get('draft'):
            continue
        if channel == 'stable' and rel.get('prerelease'):
            continue
        tag = release_tag(rel.get('tag_name'))
        if not tag or not _matches_channel(tag, channel):
            continue
        best_tag = release_tag(best.get('tag_name')) if best else None
        if best is None or best_tag is None or is_newer(tag, best_tag):
            best = rel
    return best


def _fetch_rest(channel, etag, timeout):
    url = REST_LATEST_URL if channel == 'stable' else REST_LIST_URL
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': GITHUB_API_VERSION,
        'User-Agent': _user_agent(),
    }
    if etag:
        headers['If-None-Match'] = etag
    try:
        code, resp_headers, body = _http_get(url, headers=headers, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError) as ex:
        return {'release': None, 'etag': etag, 'code': None,
                'rate_limited': False, 'error': str(ex)}
    new_etag = (resp_headers.get('ETag') if resp_headers else None) or etag
    if code == 304:
        return {'release': None, 'etag': new_etag, 'code': 304,
                'rate_limited': False, 'error': None}
    rate_limited = code in (403, 429)
    if code != 200:
        return {'release': None, 'etag': new_etag, 'code': code,
                'rate_limited': rate_limited, 'error': 'rest HTTP %s' % code}
    try:
        data = json.loads(body.decode('utf-8'))
    except ValueError as ex:
        return {'release': None, 'etag': new_etag, 'code': code,
                'rate_limited': False, 'error': 'rest parse failed: %s' % ex}
    return {'release': _pick_rest_release(data, channel), 'etag': new_etag,
            'code': 200, 'rate_limited': False, 'error': None}


def _asset_from(release):
    if not isinstance(release, dict):
        return None
    assets = release.get('assets')
    if not isinstance(assets, list):
        return None
    chosen = None
    for asset in assets:
        if isinstance(asset, dict) and asset.get('name') == ASSET_NAME:
            chosen = asset
            break
    if chosen is None:
        return None
    digest = chosen.get('digest')
    if not isinstance(digest, str) or not digest.startswith('sha256:'):
        digest = None
    return {
        'name': chosen.get('name') or ASSET_NAME,
        'size': chosen.get('size'),
        'digest': digest,
        'url': chosen.get('browser_download_url'),
    }


# ------------------------------------------------------------------- check

def check(force=False, channel='stable', timeout=10):
    try:
        return _check(force, channel, timeout)
    except Exception as ex:
        return _result(False, 'check_failed', channel, app_version(),
                       error='%s: %s' % (type(ex).__name__, ex))


def _check(force, channel, timeout):
    current = app_version()
    if channel not in CHANNELS:
        return _result(False, 'check_failed', channel, current,
                       error='unknown channel: %s' % channel)
    stored = _read_state()
    last = stored.get('last_check') if isinstance(stored.get('last_check'), dict) else None
    if not force and _cache_fresh(stored, channel):
        cached = dict(last)
        cached['source'] = 'cache'
        cached['current'] = current
        if not is_newer(cached.get('latest'), current):
            cached.update({'ok': True, 'status': 'up_to_date', 'latest': current,
                           'asset': None, 'notes': None, 'publishedAt': None,
                           'error': None})
        return cached

    etags = dict(stored.get('etags') or {})
    atom = _fetch_atom(etags.get('atom'), timeout)
    if atom['code'] in (403, 429):
        result = _result(False, 'rate_limited', channel, current,
                         error='atom HTTP %s (rate limited)' % atom['code'])
        _save(stored, result, atom_etag=atom.get('etag'))
        return result
    if atom['code'] == 304 and last and last.get('channel') == channel:
        result = _cached_result(last, current)
        _save(stored, result, atom_etag=atom.get('etag'))
        return result
    if atom['entries'] is not None:
        best = _select_release(atom['entries'], channel)
        if best is None or not is_newer(best['tag'], current):
            published = (best or {}).get('publishedAt')
            result = _result(True, 'up_to_date', channel, current, latest=current,
                             publishedAt=published, source='atom')
            _save(stored, result, atom_etag=atom.get('etag'))
            return result
        latest = best['tag']
        published = best.get('publishedAt')
        rest = _fetch_rest(channel, etags.get('rest'), timeout)
        release = rest.get('release')
        asset = None
        notes = None
        error = rest.get('error')
        source = 'atom'
        if release is not None:
            tag = release_tag(release.get('tag_name'))
            if tag:
                latest = tag
            published = release.get('published_at') or published
            asset = _asset_from(release)
            notes = release.get('body')
            error = None
            source = 'rest'
        elif rest.get('code') == 304 and last and last.get('latest') == latest:
            asset = last.get('asset')
            notes = last.get('notes')
            published = last.get('publishedAt') or published
            error = None
            source = 'cache'
        result = _result(True, 'update_available', channel, current, latest=latest,
                         asset=asset, notes=notes, publishedAt=published,
                         source=source, error=error)
        _save(stored, result, atom_etag=atom.get('etag'), rest_etag=rest.get('etag'))
        return result

    rest = _fetch_rest(channel, etags.get('rest'), timeout)
    if rest.get('code') == 304 and last and last.get('channel') == channel:
        result = _cached_result(last, current)
        _save(stored, result, rest_etag=rest.get('etag'))
        return result
    release = rest.get('release')
    if release is None:
        status = 'rate_limited' if rest.get('rate_limited') else 'check_failed'
        error = rest.get('error') or 'no release data'
        if rest.get('rate_limited'):
            error = 'rest HTTP %s (rate limited)' % rest.get('code')
        result = _result(False, status, channel, current, error=error)
        _save(stored, result, rest_etag=rest.get('etag'))
        return result
    tag = release_tag(release.get('tag_name')) or current
    if not is_newer(tag, current):
        result = _result(True, 'up_to_date', channel, current, latest=current,
                         publishedAt=release.get('published_at'), source='rest')
        _save(stored, result, rest_etag=rest.get('etag'))
        return result
    result = _result(True, 'update_available', channel, current, latest=tag,
                     asset=_asset_from(release), notes=release.get('body'),
                     publishedAt=release.get('published_at'), source='rest')
    _save(stored, result, rest_etag=rest.get('etag'))
    return result


def _cached_result(last, current):
    result = dict(last)
    result['current'] = current
    result['source'] = 'cache'
    result['checkedAt'] = _now_iso()
    result['ok'] = True
    if is_newer(result.get('latest'), current):
        result['status'] = 'update_available'
    else:
        result['status'] = 'up_to_date'
        result['latest'] = current
        result['asset'] = None
        result['notes'] = None
        result['publishedAt'] = None
    result['error'] = None
    return result


# ---------------------------------------------------------------- download

def _download_result(ok, status, path, sha256, size, version, error):
    return {
        'ok': bool(ok),
        'status': status,
        'path': path,
        'sha256': sha256,
        'bytes': size,
        'version': version,
        'error': error,
    }


def download(version=None, channel='stable', timeout=60):
    try:
        return _download(version, channel, timeout)
    except Exception as ex:
        return _download_result(False, 'download_failed', None, None, 0, version,
                                '%s: %s' % (type(ex).__name__, ex))


def _parse_sums(text):
    sums = {}
    if not isinstance(text, str):
        return sums
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        name = parts[-1].lstrip('*').strip()
        if _HEX_RE.match(digest) and name:
            sums[name] = digest
    return sums


def _verify_digest(sha256_hex, asset_digest=None, sums=None, asset_name=ASSET_NAME):
    """Return None when verified, else an error string. Fails closed."""
    digest = (sha256_hex or '').lower()
    checked = False
    if isinstance(asset_digest, str) and asset_digest.strip():
        expected = asset_digest.strip().lower()
        if expected.startswith('sha256:'):
            expected = expected[len('sha256:'):]
        if _HEX_RE.match(expected):
            checked = True
            if digest != expected:
                return 'sha256 mismatch against GitHub release digest'
    if isinstance(sums, dict) and asset_name in sums:
        checked = True
        if digest != sums[asset_name]:
            return 'sha256 mismatch against %s' % SUMS_NAME
    if not checked:
        return 'no published checksum available to verify the download'
    return None


def _sums_urls(version):
    urls = []
    for tag in (version, 'v%s' % version):
        url = '%s/%s/%s' % (DOWNLOAD_BASE, tag, SUMS_NAME)
        if url not in urls:
            urls.append(url)
    return urls


def _fetch_sums(version, timeout):
    for url in _sums_urls(version):
        try:
            code, _headers, body = _http_get(
                url, headers={'User-Agent': _user_agent()}, timeout=timeout)
        except Exception:
            continue
        if code == 200:
            return _parse_sums(body.decode('utf-8', 'replace'))
    return {}


def _promote_staged(data, version, sha256_hex):
    path = os.path.join(STAGED_DIR, ASSET_NAME)
    tmp_dir = None
    try:
        os.makedirs(STAGED_DIR, exist_ok=True)
        tmp_dir = tempfile.mkdtemp(dir=UPDATE_DIR, prefix='tmp-download-')
        tmp_path = os.path.join(tmp_dir, ASSET_NAME)
        with open(tmp_path, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        return None
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    staged = {
        'ready': True,
        'version': version,
        'path': path,
        'sha256': sha256_hex,
        'bytes': len(data),
        'stagedAt': _now_iso(),
    }
    stored = _read_state()
    stored['schema'] = STATE_SCHEMA
    stored['staged'] = staged
    _write_state(stored)
    return path


def _download(version, channel, timeout):
    current = app_version()
    if channel not in CHANNELS:
        return _download_result(False, 'unsupported', None, None, 0, version,
                                'unknown channel: %s' % channel)
    stored = _read_state()
    last = stored.get('last_check') if isinstance(stored.get('last_check'), dict) else {}
    if version is None and last.get('channel') and last.get('channel') != channel:
        return _download_result(False, 'no_update', None, None, 0, None,
                                'cached check is for channel %s; run a %s check first'
                                % (last.get('channel'), channel))
    target = version or last.get('latest')
    if not target:
        return _download_result(False, 'no_update', None, None, 0, None,
                                'no update available; run a check first')
    if version_key(target) is None:
        return _download_result(False, 'download_failed', None, None, 0, target,
                                'invalid version: %s' % target)
    if not is_newer(target, current):
        return _download_result(False, 'no_update', None, None, 0, target,
                                'no newer version to download (current %s)' % current)

    asset = None
    if isinstance(last, dict) and last.get('latest') == target and isinstance(last.get('asset'), dict):
        asset = last['asset']
    url = (asset or {}).get('url') or '%s/%s/%s' % (DOWNLOAD_BASE, target, ASSET_NAME)
    digest = (asset or {}).get('digest')
    expected_size = (asset or {}).get('size')

    headers = {'User-Agent': _user_agent(), 'Accept': 'application/octet-stream'}
    try:
        code, _resp_headers, body = _http_get(
            url, headers=headers, timeout=timeout, max_bytes=MAX_DOWNLOAD_BYTES)
    except DownloadTooLarge as ex:
        return _download_result(False, 'download_failed', None, None, 0, target, str(ex))
    except Exception as ex:
        return _download_result(False, 'download_failed', None, None, 0, target, str(ex))
    if code != 200:
        return _download_result(False, 'download_failed', None, None, 0, target,
                                'download HTTP %s' % code)
    if len(body) > MAX_DOWNLOAD_BYTES:
        return _download_result(False, 'download_failed', None, None, 0, target,
                                'asset exceeds the %d byte cap' % MAX_DOWNLOAD_BYTES)
    if isinstance(expected_size, int) and expected_size > 0 and len(body) != expected_size:
        return _download_result(False, 'verify_failed', None, None, len(body), target,
                                'size mismatch: expected %d got %d' % (expected_size, len(body)))

    sha256_hex = hashlib.sha256(body).hexdigest()
    sums = _fetch_sums(target, timeout)
    error = _verify_digest(sha256_hex, digest, sums)
    if error:
        return _download_result(False, 'verify_failed', None, sha256_hex, len(body),
                                target, error)
    path = _promote_staged(body, target, sha256_hex)
    if not path:
        return _download_result(False, 'download_failed', None, sha256_hex, len(body),
                                target, 'could not promote download to %s' % STAGED_DIR)
    return _download_result(True, 'downloaded', path, sha256_hex, len(body), target, None)


# -------------------------------------------------------------------- apply

def apply(staged_path=None):
    if os.name != 'nt':
        return {'ok': False, 'status': 'unsupported', 'helper': None,
                'logPath': LOG_FILE,
                'error': 'applying updates is supported on Windows only'}
    try:
        return _apply(staged_path)
    except Exception as ex:
        return {'ok': False, 'status': 'error', 'helper': None, 'logPath': LOG_FILE,
                'error': '%s: %s' % (type(ex).__name__, ex)}


def _apply(staged_path):
    stored = _read_state()
    staged = stored.get('staged') if isinstance(stored.get('staged'), dict) else {}
    path = staged_path or staged.get('path') or os.path.join(STAGED_DIR, ASSET_NAME)
    if not os.path.isfile(path):
        return {'ok': False, 'status': 'error', 'helper': None, 'logPath': LOG_FILE,
                'error': 'no staged update found at %s' % path}
    if not os.path.isfile(HELPER_FILE):
        return {'ok': False, 'status': 'error', 'helper': None, 'logPath': LOG_FILE,
                'error': 'missing updater helper: %s' % HELPER_FILE}
    version = staged.get('version') or app_version()
    if version_key(version) is not None and not is_newer(version, app_version()):
        return {'ok': False, 'status': 'error', 'helper': None, 'logPath': LOG_FILE,
                'error': 'refusing to apply non-newer version %s (current %s)'
                         % (version, app_version())}
    os.makedirs(LOG_DIR, exist_ok=True)
    helper_dir = tempfile.mkdtemp(prefix='riftsense-apply-')
    helper = os.path.join(helper_dir, os.path.basename(HELPER_FILE))
    shutil.copy2(HELPER_FILE, helper)
    args = [
        'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', helper,
        '-Staged', os.path.dirname(path),
        '-InstallDir', ROOT,
        '-Version', version,
        '-LogDir', LOG_DIR,
    ]
    creationflags = (getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)
                     | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200))
    subprocess.Popen(
        args, creationflags=creationflags, cwd=helper_dir,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    return {'ok': True, 'status': 'started', 'helper': helper,
            'logPath': LOG_FILE, 'error': None}


# -------------------------------------------------------------------- state

def _staged_info(stored):
    staged = stored.get('staged') if isinstance(stored.get('staged'), dict) else {}
    path = staged.get('path') or os.path.join(STAGED_DIR, ASSET_NAME)
    ready = bool(staged.get('ready')) and os.path.isfile(path)
    return {
        'ready': ready,
        'version': staged.get('version'),
        'path': staged.get('path') or (path if ready else None),
        'sha256': staged.get('sha256'),
        'bytes': staged.get('bytes'),
        'stagedAt': staged.get('stagedAt'),
    }


def state():
    stored = _read_state()
    last = stored.get('last_check') if isinstance(stored.get('last_check'), dict) else None
    current = app_version()
    staged = _staged_info(stored)
    if last:
        payload = dict(last)
        payload['current'] = current
        payload['staged'] = staged
    else:
        payload = {
            'ok': True,
            'status': 'unknown',
            'current': current,
            'latest': None,
            'channel': 'stable',
            'asset': None,
            'notes': None,
            'publishedAt': None,
            'checkedAt': None,
            'source': None,
            'error': None,
            'staged': staged,
        }
    return payload


def log_tail(limit=80):
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 80
    if limit <= 0:
        return []
    return lines[-limit:]
