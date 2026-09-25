import http.server
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

_IMPORT_TMP = tempfile.mkdtemp(prefix='riftsense-trends-import-')
os.environ.setdefault('RIFTSENSE_TIMELINE_DB', os.path.join(_IMPORT_TMP, 'timeline.db'))

from ui import server  # noqa: E402
from ui import trends  # noqa: E402

TIMELINE = trends.timeline


class TrendsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        TIMELINE.configure(os.path.join(self.tmp.name, 'timeline.db'))
        TIMELINE.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_database_is_ok_with_zero_samples(self):
        report = trends.build_trends(now=1000.0)
        self.assertTrue(report['ok'])
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['generatedAt'], 1000.0)
        self.assertEqual(report['games'], {'played': 0, 'open': 0, 'withChamp': 0})
        self.assertEqual(report['champs'], [])
        self.assertEqual(report['deathBuckets']['total'], 0)
        self.assertEqual(report['deathBuckets']['unknown'], 0)
        self.assertEqual([b['label'] for b in report['deathBuckets']['buckets']],
                         ['0-5', '5-10', '10-15', '15-20', '20+'])
        self.assertEqual([b['count'] for b in report['deathBuckets']['buckets']], [0, 0, 0, 0, 0])
        self.assertEqual(report['objectives']['total'], 0)
        self.assertEqual([c['label'] for c in report['objectives']['counts']],
                         list(trends.OBJECTIVE_LABELS))
        self.assertTrue(all(c['count'] == 0 for c in report['objectives']['counts']))
        self.assertEqual(report['inference']['requests'], 0)
        self.assertEqual(report['unavailable'], [])

    def test_aggregates_only_ended_games(self):
        TIMELINE.ensure_game('s1', champ='Nunu & Willump')
        TIMELINE.record_event('death', 'died 2:00', t_game=120, session_id='s1')
        TIMELINE.record_event('death', 'died 5:20', t_game=320, session_id='s1')
        TIMELINE.record_event('death', 'died 11:40', t_game=700, session_id='s1')
        TIMELINE.record_event('objective', 'dragon up soon', t_game=280, session_id='s1')
        TIMELINE.record_advice('coach', 'take dragon', t_game=430, session_id='s1')
        TIMELINE.record_inference('r1', 'started', session_id='s1')
        TIMELINE.record_inference('r1', 'ok', session_id='s1', tokens_in=100,
                                  tokens_out=25, cost=0.02)
        TIMELINE.end_game('s1')

        TIMELINE.ensure_game('s2', champ='Warwick', force_new=True)
        TIMELINE.record_event('death', 'died 20:50', t_game=1250, session_id='s2')
        TIMELINE.record_event('objective', 'baron up soon', t_game=1190, session_id='s2')
        TIMELINE.record_inference('r2', 'failed', session_id='s2')
        TIMELINE.end_game('s2')

        TIMELINE.ensure_game('s3', champ='Nunu & Willump', force_new=True)
        TIMELINE.record_event('death', 'open game death', t_game=60, session_id='s3')

        report = trends.build_trends()
        self.assertTrue(report['ok'])
        self.assertEqual(report['games']['played'], 2)
        self.assertEqual(report['games']['open'], 1)
        self.assertEqual(report['games']['withChamp'], 2)

        champs = {c['champ']: c for c in report['champs']}
        self.assertEqual(champs['Nunu & Willump']['games'], 1)
        self.assertEqual(champs['Nunu & Willump']['deaths'], 3)
        self.assertEqual(champs['Nunu & Willump']['avgDeaths'], 3.0)
        self.assertEqual(champs['Warwick']['games'], 1)
        self.assertEqual(champs['Warwick']['deaths'], 1)
        self.assertEqual(champs['Warwick']['avgDeaths'], 1.0)

        buckets = {b['label']: b['count'] for b in report['deathBuckets']['buckets']}
        self.assertEqual(report['deathBuckets']['total'], 4)
        self.assertEqual(report['deathBuckets']['unknown'], 0)
        self.assertEqual(buckets, {'0-5': 1, '5-10': 1, '10-15': 1, '15-20': 0, '20+': 1})

        objectives = {c['label']: c['count'] for c in report['objectives']['counts']}
        self.assertEqual(report['objectives']['total'], 2)
        self.assertEqual(objectives['dragon'], 1)
        self.assertEqual(objectives['baron'], 1)

        self.assertEqual(report['inference']['requests'], 2)
        self.assertEqual(report['inference']['ok'], 1)
        self.assertEqual(report['inference']['failed'], 1)
        self.assertEqual(report['inference']['tokensIn'], 100)
        self.assertEqual(report['inference']['tokensOut'], 25)
        self.assertEqual(report['inference']['cost'], 0.02)
        self.assertEqual(report['unavailable'], [])

    def test_unknown_champ_and_missing_death_clock_are_bucketed(self):
        TIMELINE.ensure_game('s4')
        TIMELINE.record_event('death', 'no clock', t_game=None, session_id='s4')
        TIMELINE.end_game('s4')

        report = trends.build_trends()
        self.assertEqual(report['games']['played'], 1)
        self.assertEqual(report['games']['withChamp'], 0)
        self.assertEqual(len(report['champs']), 1)
        self.assertEqual(report['champs'][0]['champ'], '(unknown)')
        self.assertEqual(report['champs'][0]['deaths'], 1)
        self.assertEqual(report['deathBuckets']['total'], 1)
        self.assertEqual(report['deathBuckets']['unknown'], 1)
        self.assertEqual([b['count'] for b in report['deathBuckets']['buckets']], [0, 0, 0, 0, 0])

    def test_db_failure_reports_unavailable(self):
        with mock.patch.object(trends.timeline, '_connect',
                               side_effect=sqlite3.OperationalError('boom')):
            report = trends.build_trends()
        self.assertFalse(report['ok'])
        self.assertEqual(report['status'], 'db_error')
        self.assertEqual(report['unavailable'], ['timeline_db'])


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'build_intent.txt')
        self._orig_build = server.BUILD_FILE
        server.BUILD_FILE = self.path
        self.original = ("PLAN[Nunu & Willump]: Haunting Guise -> Liandry's Torment\n"
                         "\n"
                         "Notes for the coach:\n"
                         "- keep this note\n")
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(self.original)

    def tearDown(self):
        server.BUILD_FILE = self._orig_build
        self.tmp.cleanup()

    def read(self, path=None):
        with open(path or self.path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_valid_plan_is_saved_atomically_with_backup(self):
        new_text = 'PLAN[Warwick]: Blade of The Ruined King -> Thornmail\n- note\n'
        result = server.write_plan_text(new_text)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(result['planLines'], 1)
        self.assertEqual(result['backup'], 'build_intent.txt.bak')
        self.assertEqual(self.read(), new_text)
        self.assertEqual(self.read(self.path + '.bak'), self.original)
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_crlf_and_legacy_default_line_normalize(self):
        result = server.write_plan_text('PLAN: situational items\r\n\r\n')
        self.assertTrue(result['ok'])
        self.assertEqual(self.read(), 'PLAN: situational items\n')

    def test_invalid_plan_leaves_original_and_no_backup(self):
        bad_inputs = (
            'PLAN[Broken: missing bracket',
            'PLAN[]: Bloodthirster',
            'PLAN[Nunu & Willump]: Haunting Guise -> ',
            'PLAN[Nunu]: ' + ' -> '.join('Item %d' % i for i in range(13)),
            'no plan lines at all',
        )
        for bad in bad_inputs:
            with self.subTest(bad=bad[:40]):
                result = server.write_plan_text(bad)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error'], 'invalid_plan')
                self.assertEqual(self.read(), self.original)
                self.assertFalse(os.path.exists(self.path + '.bak'))

    def test_duplicate_champion_rejected(self):
        result = server.write_plan_text('PLAN[Nunu]: A -> B\nPLAN[nunu]: C -> D\n')
        self.assertFalse(result['ok'])
        self.assertTrue(any('duplicate' in d for d in result['details']))
        self.assertEqual(self.read(), self.original)

    def test_control_characters_and_long_lines_rejected(self):
        result = server.write_plan_text('PLAN[Nunu]: A -> B\x00C\n')
        self.assertFalse(result['ok'])
        result = server.write_plan_text('PLAN[Nunu]: ' + 'A' * 500 + '\n')
        self.assertFalse(result['ok'])
        self.assertEqual(self.read(), self.original)

    def test_too_large_text_rejected(self):
        result = server.write_plan_text('PLAN[Nunu]: ' + 'a' * (server.PLAN_MAX_BYTES + 1))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'too_large')
        self.assertEqual(self.read(), self.original)

    def test_invalid_unicode_rejected(self):
        result = server.write_plan_text('PLAN[Nunu]: A -> B\ud800')
        self.assertFalse(result['ok'])
        self.assertEqual(self.read(), self.original)

    def test_lines_payload(self):
        text = server.plan_text_from_payload({'lines': ['PLAN[Nunu]: A -> B', '']})
        self.assertEqual(text, 'PLAN[Nunu]: A -> B\n')
        for payload in ({}, {'lines': 'nope'}, {'lines': []}, {'lines': [1]}, 'nope'):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    server.plan_text_from_payload(payload)

    def test_validate_requires_at_least_one_plan_line(self):
        result = server.validate_plan_text('just prose\n- and a note\n')
        self.assertFalse(result['ok'])
        self.assertTrue(any('at least one' in d for d in result['details']))


class ServerEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        TIMELINE.configure(os.path.join(self.tmp.name, 'timeline.db'))
        TIMELINE.init_db()
        self._orig_build = server.BUILD_FILE
        server.BUILD_FILE = os.path.join(self.tmp.name, 'build_intent.txt')
        with open(server.BUILD_FILE, 'w', encoding='utf-8') as f:
            f.write('PLAN[Nunu]: A -> B\n')
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        server.BUILD_FILE = self._orig_build
        self.tmp.cleanup()

    def request(self, path, payload=None):
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read().decode('utf-8'))

    def test_trends_plan_raw_and_plan_edit_routes(self):
        status, body = self.request('/api/trends')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['games']['played'], 0)

        status, body = self.request('/api/plan/raw')
        self.assertEqual(status, 200)
        self.assertEqual(body['text'], 'PLAN[Nunu]: A -> B')

        status, body = self.request('/api/plan/edit', {'text': 'PLAN[Warwick]: C -> D'})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['backup'], 'build_intent.txt.bak')

        status, body = self.request('/api/plan/edit', {'text': 'PLAN[broken'})
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_plan')
        with open(server.BUILD_FILE, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'PLAN[Warwick]: C -> D\n')

        status, body = self.request('/api/plan/edit', {'nope': True})
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_body')


class TrendsAuditTests(unittest.TestCase):
    """Regressions for B8 (unknown usage), B9/B10 (coverage and reminder
    naming) and the 'coverage front and centre' requirement."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        TIMELINE.configure(os.path.join(self.tmp.name, 'timeline.db'))
        TIMELINE.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_objective_reminders_renamed_with_outcomes_separate(self):
        TIMELINE.ensure_game('s-obj', champ='Ahri')
        TIMELINE.record_event('objective', 'dragon up soon', {'secondsLeft': 30},
                              t_game=300, session_id='s-obj')
        TIMELINE.record_event('objective', 'baron up soon', {'secondsLeft': 60},
                              t_game=1100, session_id='s-obj')
        TIMELINE.record_event('objective', 'dragon killed', {'outcome': True},
                              t_game=700, session_id='s-obj')
        TIMELINE.end_game('s-obj')

        report = trends.build_trends()
        self.assertEqual(report['objectives'], report['objectiveReminders'])
        self.assertEqual(report['objectiveReminders']['kind'], 'reminder')
        self.assertEqual(report['objectiveReminders']['total'], 2)
        reminders = {c['label']: c['count']
                     for c in report['objectiveReminders']['counts']}
        self.assertEqual(reminders['dragon'], 1)
        self.assertEqual(reminders['baron'], 1)
        self.assertEqual(report['objectiveOutcomes']['kind'], 'outcome')
        self.assertEqual(report['objectiveOutcomes']['total'], 1)
        outcomes = {c['label']: c['count']
                    for c in report['objectiveOutcomes']['counts']}
        self.assertEqual(outcomes['dragon'], 1)
        self.assertNotIn('killed', [c['label']
                                    for c in report['objectiveReminders']['counts']])

    def test_unknown_inference_usage_renders_null_not_zero(self):
        game = TIMELINE.ensure_game('s-inf', champ='Ahri')
        TIMELINE.record_event('death', 'died 2:00', t_game=120, session_id='s-inf')
        TIMELINE.end_game('s-inf')
        con = sqlite3.connect(TIMELINE.DB_PATH)
        try:
            con.execute(
                'INSERT INTO inference (game_id, request_id, started_at, finished_at, status,'
                ' tokens_in, tokens_out, cost) VALUES (?,?,?,?,?,?,?,?)',
                (game['id'], 'unknown-1', 1, 2, 'ok', None, None, None))
            con.execute(
                'INSERT INTO inference (game_id, request_id, started_at, finished_at, status,'
                ' tokens_in, tokens_out, cost) VALUES (?,?,?,?,?,?,?,?)',
                (game['id'], 'known-1', 1, 2, 'ok', 100, 25, 0.02))
            con.commit()
        finally:
            con.close()

        report = trends.build_trends()
        self.assertEqual(report['inference']['requests'], 2)
        self.assertIsNone(report['inference']['tokensIn'])
        self.assertIsNone(report['inference']['tokensOut'])
        self.assertIsNone(report['inference']['cost'])
        self.assertIn('inference:tokensIn', report['unavailable'])
        self.assertIn('inference:tokensOut', report['unavailable'])
        self.assertIn('inference:cost', report['unavailable'])
        self.assertEqual(report['status'], 'partial')

    def test_fully_unknown_inference_usage_is_null_never_zero(self):
        TIMELINE.ensure_game('s-inf2', champ='Ahri')
        TIMELINE.end_game('s-inf2')
        con = sqlite3.connect(TIMELINE.DB_PATH)
        try:
            con.execute(
                'INSERT INTO inference (game_id, request_id, started_at, finished_at, status,'
                ' tokens_in, tokens_out, cost) VALUES (?,?,?,?,?,?,?,?)',
                (1, 'unknown-2', 1, 2, 'failed', None, None, None))
            con.commit()
        finally:
            con.close()

        report = trends.build_trends()
        self.assertIsNone(report['inference']['tokensIn'])
        self.assertIsNone(report['inference']['tokensOut'])
        self.assertIsNone(report['inference']['cost'])
        self.assertNotEqual(report['inference']['tokensIn'], 0)

    def test_coverage_section_keeps_sample_size_visible(self):
        report = trends.build_trends()
        self.assertEqual(report['coverage']['status'], 'no_games')
        self.assertEqual(report['coverage']['sampleSize'], 0)

        TIMELINE.ensure_game('s-cov', champ='Ahri')
        TIMELINE.end_game('s-cov')
        report2 = trends.build_trends()
        self.assertEqual(report2['coverage']['status'], 'unavailable')
        self.assertEqual(report2['coverage']['gamesWithCoverage'], 0)
        self.assertEqual(report2['coverage']['sampleSize'], 1)
        self.assertEqual(report2['games']['played'], 1)

        fake = lambda game_id: {'intervals': [[0, 1800]], 'adequate': True,
                                'observedRatio': 0.9}
        with mock.patch.object(trends.timeline, 'coverage', fake, create=True):
            report3 = trends.build_trends()
        self.assertEqual(report3['coverage']['status'], 'ok')
        self.assertEqual(report3['coverage']['gamesWithCoverage'], 1)
        self.assertEqual(report3['coverage']['adequateGames'], 1)


if __name__ == '__main__':
    unittest.main()
