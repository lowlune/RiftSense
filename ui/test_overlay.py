"""Tests for the stream-safe overlay (ui/overlay.html) and its server route.

The inline scripts of ui/index.html and ui/overlay.html must parse under
``node --check``. A small DOM-stub harness (test_overlay_harness.js) drives
the overlay render helpers and checks escaping, numeric validation, privacy
and graceful degradation. Everything is skipped when node is unavailable.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.path.join(HERE, 'overlay.html')
INDEX = os.path.join(HERE, 'index.html')
SERVER = os.path.join(HERE, 'server.py')
HARNESS = os.path.join(HERE, 'test_overlay_harness.js')
NODE = shutil.which('node')

SCRIPT_RE = re.compile(r'<script(?:\s[^>]*)?>(.*?)</script>', re.S | re.I)


def inline_scripts(path):
    with open(path, 'r', encoding='utf-8') as f:
        return SCRIPT_RE.findall(f.read())


class OverlayMarkupTest(unittest.TestCase):
    def setUp(self):
        with open(OVERLAY, 'r', encoding='utf-8') as f:
            self.html = f.read()
        self.lower = self.html.lower()

    def test_no_private_panels(self):
        for needle in ('/api/cost', 'costtotal', 'summoner', 'identity', 'api/plan', 'api/purchase'):
            self.assertNotIn(needle, self.lower, 'overlay must not reference %s' % needle)

    def test_polls_required_endpoints(self):
        for endpoint in ('/api/game', '/api/coach', '/api/death', '/api/timeline/current'):
            self.assertIn(endpoint, self.html)

    def test_shows_required_panels(self):
        for marker in ('id="clock"', 'id="conn"', 'id="donow"', 'id="death"',
                       'id="dragonVal"', 'id="baronVal"'):
            self.assertIn(marker, self.html)

    def test_supports_background_and_scale_params(self):
        self.assertIn('params.get', self.html)
        self.assertIn('bg-transparent', self.lower)
        self.assertIn('scale', self.lower)

    def test_server_routes_overlay(self):
        with open(SERVER, 'r', encoding='utf-8') as f:
            src = f.read()
        self.assertIn("'/overlay'", src)
        self.assertIn("'/overlay.html'", src)
        self.assertIn("'overlay.html'", src)


class IndexControlsTest(unittest.TestCase):
    def setUp(self):
        with open(INDEX, 'r', encoding='utf-8') as f:
            self.html = f.read()

    def test_overlay_link(self):
        self.assertIn('href="/overlay"', self.html)
        self.assertIn('id="overlayBtn"', self.html)

    def test_voice_controls(self):
        for marker in ('id="voiceBtn"', 'id="voiceState"', 'speechSynthesis',
                       'SpeechSynthesisUtterance', 'TTS_MIN_INTERVAL_MS'):
            self.assertIn(marker, self.html)

    def test_voice_priority_order(self):
        self.assertRegex(self.html, r"TTS_PRIORITY\s*=\s*\{\s*coach:\s*1,\s*objective:\s*2,\s*death:\s*3\s*\}")


@unittest.skipUnless(NODE, 'node is required for script checks')
class ScriptParseTest(unittest.TestCase):
    def test_inline_scripts_parse(self):
        for path in (OVERLAY, INDEX):
            scripts = inline_scripts(path)
            self.assertTrue(scripts, 'no inline script found in %s' % path)
            for i, code in enumerate(scripts):
                handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
                try:
                    handle.write(code)
                    handle.close()
                    proc = subprocess.run([NODE, '--check', handle.name],
                                          capture_output=True, text=True)
                finally:
                    os.unlink(handle.name)
                self.assertEqual(proc.returncode, 0,
                                 'node --check failed for %s script %d:\n%s' % (path, i, proc.stderr))

    def test_overlay_harness(self):
        proc = subprocess.run([NODE, HARNESS], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         'overlay harness failed:\n%s\n%s' % (proc.stdout, proc.stderr))


if __name__ == '__main__':
    unittest.main()
