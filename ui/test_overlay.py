"""Tests for the stream-safe overlay (ui/overlay.html) and its server route.

The inline scripts of ui/index.html and ui/overlay.html must parse under
``node --check``. A small DOM-stub harness (test_overlay_harness.js) drives
the overlay render helpers and checks action priority/TTL selection, stale
clock handling, preset parsing, privacy stripping, escaping, numeric
validation and graceful degradation. Everything is skipped when node is
unavailable.
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

    def test_consumes_action_endpoint(self):
        self.assertIn('/api/action', self.html)
        self.assertIn('applyAction', self.html)
        self.assertIn('actionState', self.html)

    def test_action_priority_and_ttl_rules(self):
        self.assertIn('KIND_PRIORITY', self.html)
        self.assertIn('KIND_TTL_MS', self.html)
        self.assertRegex(self.html, r"death:\s*3,\s*objective:\s*2,\s*coach:\s*1")
        self.assertRegex(self.html, r"death:\s*60000,\s*objective:\s*90000,\s*coach:\s*90000")

    def test_session_and_refresh_transitions(self):
        self.assertIn('sessionMatches', self.html)
        self.assertIn('clearTransient', self.html)
        self.assertIn('triggerImmediateRefresh', self.html)

    def test_presets_and_independent_scales(self):
        self.assertIn('preset=', self.lower)
        for name in ('corner', 'strip', 'second-monitor'):
            self.assertIn(name, self.lower)
        for marker in ('--clock-scale', '--action-scale', '--timer-scale',
                       'clockScale', 'actionScale', 'timerScale'):
            self.assertIn(marker, self.html)

    def test_responsive_overflow_fallback(self):
        self.assertIn('overflow: auto', self.html)
        self.assertIn('overscroll-behavior', self.html)
        self.assertIn('@media', self.html)

    def test_privacy_strict_mode(self):
        self.assertIn('privacy', self.lower)
        self.assertIn('strict', self.lower)
        self.assertIn('TAG_RE', self.html)
        self.assertIn('STRICT_TEXT_MAX', self.html)

    def test_objective_state_text_not_color_only(self):
        self.assertIn('id="dragonState"', self.html)
        self.assertIn('id="baronState"', self.html)
        self.assertIn("'SOON'", self.html)
        self.assertIn("'STALE'", self.html)

    def test_a11y_semantics(self):
        self.assertIn('role="status"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('<h1', self.lower)
        self.assertIn('<h2', self.lower)
        self.assertIn('aria-label', self.lower)

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
