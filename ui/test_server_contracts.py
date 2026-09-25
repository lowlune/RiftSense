import hashlib
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest import mock

from ui import server, timeline


def _db_row(sql, params=()):
    con = sqlite3.connect(timeline.DB_PATH)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


class ActionStreamTests(unittest.TestCase):
    NOW = 1_700_000_000.0

    def _candidate(self, kind, age, session='s'):
        return {'kind': kind, 'text': '%s action' % kind, 'observedAt': self.NOW - age,
                'sessionId': session, 'source': kind}

    def test_priority_death_then_objective_then_coach(self):
        action, expired = server.select_action(
            [self._candidate('coach', 5), self._candidate('objective', 5),
             self._candidate('death', 5)], now=self.NOW)
        self.assertFalse(expired)
        self.assertEqual(action['kind'], 'death')
        self.assertEqual(action['priority'], 3)
        action, _ = server.select_action(
            [self._candidate('coach', 5), self._candidate('objective', 5)], now=self.NOW)
        self.assertEqual(action['kind'], 'objective')
        self.assertEqual(action['priority'], 2)
        action, _ = server.select_action([self._candidate('coach', 5)], now=self.NOW)
        self.assertEqual(action['kind'], 'coach')
        self.assertEqual(action['priority'], 1)

    def test_ttl_expiry_per_kind(self):
        action, expired = server.select_action([self._candidate('death', 61)], now=self.NOW)
        self.assertIsNone(action)
        self.assertTrue(expired)
        action, expired = server.select_action([self._candidate('objective', 60.5)], now=self.NOW)
        self.assertIsNone(action)
        self.assertTrue(expired)
        action, expired = server.select_action([self._candidate('coach', 89)], now=self.NOW)
        self.assertIsNotNone(action)
        self.assertFalse(expired)
        action, expired = server.select_action([self._candidate('coach', 91)], now=self.NOW)
        self.assertIsNone(action)
        self.assertTrue(expired)

    def test_fresher_candidate_wins_within_priority(self):
        action, _ = server.select_action(
            [self._candidate('coach', 30), self._candidate('coach', 3)], now=self.NOW)
        self.assertEqual(action['ageSec'], 3.0)

    def test_no_candidates_is_no_action_not_expired(self):
        action, expired = server.select_action([], now=self.NOW)
        self.assertIsNone(action)
        self.assertFalse(expired)

    def test_action_shape(self):
        with mock.patch.object(server, '_death_action', return_value=None), \
                mock.patch.object(server, '_objective_action', return_value=None), \
                mock.patch.object(
                    server, '_coach_action',
                    return_value={'kind': 'coach', 'text': 'reset', 'observedAt': self.NOW - 4,
                                  'sessionId': 'ses', 'source': 'coach'}):
            result = server.build_action(now=self.NOW, session_id='ses')
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'ok')
        action = result['action']
        for key in ('kind', 'text', 'priority', 'sessionId', 'observedAt',
                    'ageSec', 'expiresAt', 'source'):
            self.assertIn(key, action)
        self.assertEqual(action['expiresAt'], self.NOW - 4 + 90)
        self.assertEqual(action['ageSec'], 4.0)

    def test_build_action_filters_other_sessions(self):
        with mock.patch.object(
                server, '_death_action',
                return_value={'kind': 'death', 'text': 'x', 'observedAt': self.NOW - 1,
                              'sessionId': 'other', 'source': 'death'}), \
                mock.patch.object(server, '_coach_action', return_value=None), \
                mock.patch.object(server, '_objective_action', return_value=None):
            result = server.build_action(now=self.NOW, session_id='mine')
        self.assertIsNone(result['action'])
        self.assertEqual(result['status'], 'no_action')

    def test_build_action_expired_status(self):
        with mock.patch.object(
                server, '_death_action',
                return_value={'kind': 'death', 'text': 'x', 'observedAt': self.NOW - 100,
                              'sessionId': 'ses', 'source': 'death'}), \
                mock.patch.object(server, '_coach_action', return_value=None), \
                mock.patch.object(server, '_objective_action', return_value=None):
            result = server.build_action(now=self.NOW, session_id='ses')
        self.assertIsNone(result['action'])
        self.assertEqual(result['status'], 'expired')


class DeathReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.text_path = os.path.join(self.tmp.name, 'death_latest.txt')
        self.meta_path = os.path.join(self.tmp.name, 'death_latest.json')

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text, meta, text_mtime=None, meta_mtime=None):
        with open(self.text_path, 'w', encoding='utf-8') as f:
            f.write(text)
        with open(self.meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f)
        if text_mtime is not None:
            os.utime(self.text_path, (text_mtime, text_mtime))
        if meta_mtime is not None:
            os.utime(self.meta_path, (meta_mtime, meta_mtime))

    def _meta(self, session='ses-1'):
        return {
            'schema': 'riftsense.v1', 'kind': 'death', 'status': 'ok',
            'sessionId': session,
            'observedAt': datetime.now(timezone.utc).isoformat(),
            'facts': ['died to Zed'], 'hypotheses': [], 'doNow': 'recall now',
        }

    def test_matched_text_and_meta_are_consistent(self):
        self._write('DIED 5:00\nDO NOW: recall', self._meta())
        report = server.read_death_report(text_path=self.text_path, meta_path=self.meta_path,
                                          session_id='ses-1')
        self.assertFalse(report['mismatch'])
        self.assertIsNotNone(report['structured'])
        self.assertEqual(report['structured']['doNow'], 'recall now')

    def test_new_text_with_old_meta_is_mismatch(self):
        now = time.time()
        self._write('NEW REPORT', self._meta(), text_mtime=now, meta_mtime=now - 30)
        report = server.read_death_report(text_path=self.text_path, meta_path=self.meta_path,
                                          session_id='ses-1', now=now)
        self.assertTrue(report['mismatch'])
        self.assertEqual(report['mismatchReason'], 'text_meta_time_skew')
        self.assertIsNone(report['structured'])

    def test_old_text_with_new_meta_is_mismatch(self):
        now = time.time()
        self._write('OLD REPORT', self._meta(), text_mtime=now - 30, meta_mtime=now)
        report = server.read_death_report(text_path=self.text_path, meta_path=self.meta_path,
                                          session_id='ses-1', now=now)
        self.assertTrue(report['mismatch'])
        self.assertIsNone(report['structured'])

    def test_wrong_session_meta_is_mismatch(self):
        now = time.time()
        self._write('report', self._meta(session='old-game'),
                    text_mtime=now, meta_mtime=now)
        report = server.read_death_report(text_path=self.text_path, meta_path=self.meta_path,
                                          session_id='current-game', now=now)
        self.assertTrue(report['mismatch'])
        self.assertEqual(report['mismatchReason'], 'session_mismatch')
        self.assertIsNone(report['structured'])

    def test_no_meta_is_not_a_mismatch(self):
        now = time.time()
        with open(self.text_path, 'w', encoding='utf-8') as f:
            f.write('text only')
        report = server.read_death_report(text_path=self.text_path, meta_path=self.meta_path,
                                          session_id='ses-1', now=now)
        self.assertFalse(report['mismatch'])
        self.assertIsNone(report['structured'])


class CoachEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.meta_path = os.path.join(self.tmp.name, 'coach_latest.json')
        self.text_path = os.path.join(self.tmp.name, 'coach_latest.txt')

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, obj, mtime=None):
        with open(self.meta_path, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
        if mtime is not None:
            os.utime(self.meta_path, (mtime, mtime))

    def _envelope(self, status='ok', text='reset and buy', session='ses-1', seq=7):
        return {
            'schema': 'riftsense.v1', 'kind': 'coach', 'status': status, 'error': '',
            'session': session, 'seq': seq, 'observedGameTime': 420.0,
            'completedAt': datetime.now(timezone.utc).isoformat(), 'text': text,
        }

    def test_ok_envelope_is_fresh_with_worker_metadata(self):
        now = time.time()
        self._write(self._envelope())
        coach = server.build_coach(now=now, path=self.meta_path, text_path=self.text_path,
                                   current_session='ses-1')
        self.assertTrue(coach['fresh'])
        self.assertIsNone(coach['staleReason'])
        self.assertEqual(coach['worker']['status'], 'ok')
        self.assertEqual(coach['worker']['seq'], 7)
        self.assertEqual(coach['text'], 'reset and buy')
        self.assertEqual(coach['status'], 'ok')

    def test_failed_envelope_is_not_fresh_but_text_is_preserved(self):
        now = time.time()
        self._write(self._envelope(status='failed', text='previous advice'), mtime=now)
        coach = server.build_coach(now=now, path=self.meta_path, text_path=self.text_path,
                                   current_session='ses-1')
        self.assertFalse(coach['fresh'])
        self.assertEqual(coach['staleReason'], 'worker_failed')
        self.assertEqual(coach['text'], 'previous advice')
        self.assertEqual(coach['worker']['status'], 'failed')
        self.assertIsNone(coach['expiresAt'])

    def test_old_envelope_expires(self):
        now = time.time()
        self._write(self._envelope(), mtime=now - 200)
        coach = server.build_coach(now=now, path=self.meta_path, text_path=self.text_path,
                                   current_session='ses-1')
        self.assertFalse(coach['fresh'])
        self.assertEqual(coach['staleReason'], 'expired')

    def test_session_mismatch_is_not_fresh(self):
        now = time.time()
        self._write(self._envelope(session='other-game'), mtime=now)
        coach = server.build_coach(now=now, path=self.meta_path, text_path=self.text_path,
                                   current_session='ses-1')
        self.assertFalse(coach['fresh'])
        self.assertEqual(coach['staleReason'], 'session_mismatch')

    def test_source_age_expires_advice_even_when_publication_is_recent(self):
        now = time.time()
        envelope = self._envelope()
        envelope['observedGameTime'] = 100.0
        self._write(envelope, mtime=now)
        coach = server.build_coach(now=now, path=self.meta_path, text_path=self.text_path,
                                   current_session='ses-1', live_game_time=250.0, ttl=90)
        self.assertEqual(coach['sourceAgeSec'], 150.0)
        self.assertFalse(coach['fresh'])
        self.assertEqual(coach['staleReason'], 'source_expired')

    def test_lcu_game_time_requires_a_fresh_snapshot(self):
        with mock.patch.dict(server._LCU_STATE,
                             {'lastSnapshotAt': time.time(), 'lastGameTime': 100.0}):
            self.assertGreaterEqual(server.lcu_game_time(), 100.0)
        with mock.patch.dict(server._LCU_STATE,
                             {'lastSnapshotAt': time.time() - 120, 'lastGameTime': 100.0}):
            self.assertIsNone(server.lcu_game_time())

    def test_missing_envelope_falls_back_to_text_but_is_not_fresh(self):
        now = time.time()
        with open(self.text_path, 'w', encoding='utf-8') as f:
            f.write('legacy advice')
        coach = server.build_coach(now=now, path=self.meta_path, text_path=self.text_path,
                                   current_session='ses-1')
        self.assertEqual(coach['text'], 'legacy advice')
        self.assertFalse(coach['fresh'])
        self.assertEqual(coach['staleReason'], 'worker_unknown')
        self.assertEqual(coach['worker']['status'], 'unknown')


class LivePayloadValidationTests(unittest.TestCase):
    def _valid(self):
        return {
            'gameData': {'gameTime': 300.0, 'gameMode': 'CLASSIC', 'mapNumber': 11},
            'activePlayer': {'riotId': 'Me#EUW', 'currentGold': 500.5,
                             'abilities': {'Q': {'abilityLevel': 3}}},
            'allPlayers': [
                {'riotId': 'Me#EUW', 'championName': 'Nunu & Willump', 'team': 'ORDER',
                 'summonerName': 'Me', 'level': 8, 'scores': {'kills': 1, 'deaths': 0},
                 'items': [{'itemID': 1001, 'count': 2, 'displayName': 'Boots'}]},
                {'riotId': 'Foe#EUW', 'championName': 'Zed', 'team': 'CHAOS',
                 'summonerName': 'Foe', 'level': 8},
            ],
            'events': {'Events': [{'EventName': 'DragonKill', 'EventTime': 120.0}]},
        }

    def test_valid_payload_accepted(self):
        self.assertIsNone(server.validate_live_payload(self._valid()))

    def test_invalid_shapes_rejected(self):
        for payload in ({}, [], None, {'gameData': {}}, {'gameData': {'gameTime': 1.0}}):
            with self.subTest(payload=payload):
                self.assertIsNotNone(server.validate_live_payload(payload))

    def test_non_finite_numbers_rejected(self):
        cases = []
        bad = self._valid()
        bad['gameData']['gameTime'] = float('nan')
        cases.append(bad)
        bad = self._valid()
        bad['activePlayer']['currentGold'] = float('inf')
        cases.append(bad)
        bad = self._valid()
        bad['allPlayers'][0]['scores']['kills'] = float('nan')
        cases.append(bad)
        bad = self._valid()
        bad['events']['Events'][0]['EventTime'] = float('inf')
        cases.append(bad)
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertIsNotNone(server.validate_live_payload(payload))

    def test_invalid_payload_maps_to_api_error(self):
        self.assertEqual(server.game_status('invalid_payload'), 'api_error')


class WriteSecurityTests(unittest.TestCase):
    def _headers(self, host='127.0.0.1:7777', content_type='application/json',
                 origin=None, token=None):
        headers = {'Host': host, 'Content-Type': content_type}
        if origin is not None:
            headers['Origin'] = origin
        if token is not None:
            headers[server.WRITE_TOKEN_HEADER] = token
        return headers

    def test_host_allowlist(self):
        self.assertTrue(server.host_allowed('127.0.0.1:7777'))
        self.assertTrue(server.host_allowed('localhost:7777'))
        self.assertTrue(server.host_allowed('[::1]:7777'))
        self.assertFalse(server.host_allowed('evil.example:7777'))
        self.assertFalse(server.host_allowed('127.0.0.1.evil.example'))
        self.assertFalse(server.host_allowed(None))

    def test_origin_allowlist(self):
        self.assertTrue(server.origin_allowed('http://127.0.0.1:7777', port=7777))
        self.assertTrue(server.origin_allowed('http://localhost:7777', port=7777))
        self.assertTrue(server.origin_allowed('http://127.0.0.1', port=7777))
        self.assertFalse(server.origin_allowed('http://evil.example', port=7777))
        self.assertFalse(server.origin_allowed('http://127.0.0.1:9999', port=7777))
        self.assertFalse(server.origin_allowed('null', port=7777))
        self.assertFalse(server.origin_allowed('', port=7777))

    def test_content_type_required(self):
        self.assertTrue(server.content_type_ok('application/json'))
        self.assertTrue(server.content_type_ok('application/json; charset=utf-8'))
        self.assertFalse(server.content_type_ok('text/plain'))
        self.assertFalse(server.content_type_ok('application/x-www-form-urlencoded'))
        self.assertFalse(server.content_type_ok(None))

    def test_loopback_without_origin_and_without_token_is_allowed(self):
        self.assertIsNone(server.check_write_request(self._headers()))

    def test_loopback_origin_requires_token(self):
        denial = server.check_write_request(
            self._headers(origin='http://127.0.0.1:7777'))
        self.assertEqual(denial['code'], 403)
        self.assertEqual(denial['error'], 'bad_token')

    def test_loopback_origin_with_token_is_allowed(self):
        self.assertIsNone(server.check_write_request(
            self._headers(origin='http://127.0.0.1:7777', token=server._WRITE_TOKEN)))

    def test_wrong_token_is_rejected(self):
        denial = server.check_write_request(
            self._headers(origin='http://127.0.0.1:7777', token='nope'))
        self.assertEqual(denial['code'], 403)
        self.assertEqual(denial['error'], 'bad_token')

    def test_non_loopback_origin_is_rejected_even_with_token(self):
        denial = server.check_write_request(
            self._headers(origin='http://evil.example', token=server._WRITE_TOKEN))
        self.assertEqual(denial['code'], 403)
        self.assertEqual(denial['error'], 'forbidden_origin')

    def test_non_loopback_host_is_rejected(self):
        denial = server.check_write_request(self._headers(host='evil.example:7777'))
        self.assertEqual(denial['code'], 403)
        self.assertEqual(denial['error'], 'forbidden_host')

    def test_wrong_content_type_is_rejected(self):
        denial = server.check_write_request(self._headers(content_type='text/plain'))
        self.assertEqual(denial['code'], 415)
        self.assertEqual(denial['error'], 'unsupported_media_type')

    def test_token_is_generated_and_written(self):
        self.assertTrue(isinstance(server._WRITE_TOKEN, str) and server._WRITE_TOKEN)
        with open(server.TOKEN_FILE, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), server._WRITE_TOKEN)


class PlanRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'build_intent.txt')
        self.original = b'PLAN[Nunu]: A -> B\n'
        with open(self.path, 'wb') as f:
            f.write(self.original)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_revision_matches_file_hash(self):
        raw = server.build_plan_raw(path=self.path)
        self.assertEqual(raw['revision'],
                         hashlib.sha256(self.original).hexdigest())

    def test_missing_file_revision_is_empty_hash(self):
        raw = server.build_plan_raw(path=os.path.join(self.tmp.name, 'nope.txt'))
        self.assertEqual(raw['status'], 'missing')
        self.assertEqual(raw['revision'], hashlib.sha256(b'').hexdigest())

    def test_matching_revision_saves(self):
        revision = server.build_plan_raw(path=self.path)['revision']
        result = server.write_plan_text('PLAN[Warwick]: C -> D\n',
                                        expected_revision=revision, path=self.path)
        self.assertTrue(result['ok'])
        self.assertIn('revision', result)
        with open(self.path, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'PLAN[Warwick]: C -> D\n')

    def test_stale_revision_is_rejected(self):
        stale = server.build_plan_raw(path=self.path)['revision']
        server.write_plan_text('PLAN[Warwick]: C -> D\n', path=self.path)
        result = server.write_plan_text('PLAN[Ahri]: E -> F\n',
                                        expected_revision=stale, path=self.path)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'stale_revision')
        with open(self.path, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), 'PLAN[Warwick]: C -> D\n')

    def test_absent_revision_keeps_legacy_behavior(self):
        result = server.write_plan_text('PLAN[Warwick]: C -> D\n', path=self.path)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'saved')


class TimelineContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        timeline.configure(os.path.join(self.tmp.name, 'timeline.db'))
        timeline.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_version_recorded(self):
        self.assertEqual(_db_row('PRAGMA user_version')[0], timeline.SCHEMA_VERSION)

    def test_unmatched_session_is_quarantined_not_reassigned(self):
        timeline.ensure_game('real', champ='Nunu & Willump')
        result = timeline.record_event('death', 'ghost death', session_id='ghost')
        self.assertTrue(result['unmatched'])
        self.assertTrue(result['quarantined'])
        self.assertIsNone(result['gameId'])
        advice = timeline.record_advice('coach', 'ghost advice', session_id='ghost')
        self.assertTrue(advice['unmatched'])
        current = timeline.current()
        self.assertEqual(current['game']['session_id'], 'real')
        self.assertEqual(current['quarantine']['count'], 2)
        self.assertEqual(timeline.recent()['events'], [])

    def test_events_without_session_still_attach_to_open_game(self):
        timeline.ensure_game('legacy')
        result = timeline.record_event('heartbeat', 'beat')
        self.assertNotIn('unmatched', result)
        self.assertEqual(len(timeline.recent()['events']), 1)

    def test_events_attach_to_their_ended_session(self):
        first = timeline.ensure_game('ended-1')
        timeline.end_game('ended-1')
        result = timeline.record_event('death', 'late event', session_id='ended-1')
        self.assertEqual(result['gameId'], first['id'])

    def test_inference_reason_and_null_usage(self):
        timeline.ensure_game('inf-1')
        timeline.record_inference('req-1', 'started', session_id='inf-1')
        timeline.record_inference('req-1', 'failed', session_id='inf-1', reason='boom')
        row = _db_row('SELECT status, tokens_in, tokens_out, cost, reason'
                      ' FROM inference WHERE request_id=?', ('req-1',))
        self.assertEqual(row[0], 'failed')
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertEqual(row[4], 'boom')

    def test_cancelled_and_superseded_do_not_consume_budget(self):
        timeline.ensure_game('inf-2')
        timeline.record_inference('a', 'started', session_id='inf-2')
        timeline.record_inference('a', 'cancelled', session_id='inf-2')
        timeline.record_inference('b', 'superseded', session_id='inf-2')
        budget = timeline.current()['inference']
        self.assertEqual(budget['used'], 0)
        timeline.record_inference('c', 'failed', session_id='inf-2')
        self.assertEqual(timeline.current()['inference']['used'], 1)

    def test_ingest_event_honors_data_reason(self):
        timeline.ensure_game('inf-3')
        server.ingest_event({'kind': 'inference', 'requestId': 'r3', 'status': 'failed',
                             'sessionId': 'inf-3', 'data': {'reason': 'launch failed'}})
        row = _db_row('SELECT reason FROM inference WHERE request_id=?', ('r3',))
        self.assertEqual(row[0], 'launch failed')

    def test_coverage_without_intervals_is_partial(self):
        game = timeline.ensure_game('cov-1', champ='Nunu')
        time.sleep(0.02)
        timeline.end_game('cov-1')
        cov = timeline.coverage(game_id=game['id'])
        self.assertTrue(cov['partial'])
        self.assertEqual(cov['observedSeconds'], 0.0)
        self.assertEqual(len(cov['gaps']), 1)
        self.assertGreater(cov['totalSeconds'], 0)
        self.assertEqual(cov['observedRatio'], 0.0)

    def test_coverage_with_full_interval_is_complete(self):
        game = timeline.ensure_game('cov-2', champ='Nunu')
        time.sleep(0.02)
        timeline.end_game('cov-2')
        row = _db_row('SELECT started_at, ended_at FROM games WHERE id=?', (game['id'],))
        timeline.record_coverage('start', session_id='cov-2', at=row[0])
        timeline.record_coverage('stop', session_id='cov-2', at=row[1])
        cov = timeline.coverage(game_id=game['id'])
        self.assertFalse(cov['partial'])
        self.assertEqual(cov['gaps'], [])
        self.assertEqual(cov['observedSeconds'], round((row[1] - row[0]) / 1000.0, 3))

    def test_coverage_reports_gaps(self):
        game = timeline.ensure_game('cov-3', champ='Nunu')
        time.sleep(0.05)
        timeline.end_game('cov-3')
        row = _db_row('SELECT started_at, ended_at FROM games WHERE id=?', (game['id'],))
        start, end = row[0], row[1]
        self.assertGreater(end - start, 30)
        timeline.record_coverage('start', session_id='cov-3', at=start + 10)
        timeline.record_coverage('stop', session_id='cov-3', at=start + 20)
        cov = timeline.coverage(game_id=game['id'])
        self.assertTrue(cov['partial'])
        self.assertAlmostEqual(cov['gaps'][0]['seconds'], 0.01, delta=0.002)
        self.assertEqual(cov['gaps'][0]['start'], start)
        self.assertEqual(cov['observedSeconds'], 0.01)

    def test_coverage_intervals_backfill_when_game_registers(self):
        timeline.record_coverage('start', session_id='late', at=1)
        timeline.record_coverage('stop', session_id='late', at=1000)
        game = timeline.ensure_game('late')
        row = _db_row('SELECT game_id FROM intervals WHERE session_id=?', ('late',))
        self.assertEqual(row[0], game['id'])

    def test_ingest_event_coverage_kinds(self):
        timeline.ensure_game('cov-api')
        server.ingest_event({'kind': 'coverage_start', 'sessionId': 'cov-api'})
        server.ingest_event({'kind': 'coverage_stop', 'sessionId': 'cov-api'})
        cov = timeline.coverage(session_id='cov-api')
        self.assertTrue(cov['ok'])
        self.assertEqual(len(cov['intervals']), 1)
        self.assertEqual(timeline.current()['coverage']['sessionId'], 'cov-api')

    def test_unmatched_coverage_is_kept_for_late_registration(self):
        timeline.record_coverage('start', session_id='never-seen', at=1000)
        self.assertEqual(_db_row(
            "SELECT COUNT(*) FROM intervals WHERE session_id='never-seen'")[0], 1)


class HttpEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        timeline.configure(os.path.join(self.tmp.name, 'timeline.db'))
        timeline.init_db()
        self._orig = {name: getattr(server, name) for name in (
            'BUILD_FILE', 'DEATH_FILE', 'DEATH_META_FILE', 'COACH_FILE',
            'COACH_META_FILE', 'EPOCH_FILE')}
        server.BUILD_FILE = os.path.join(self.tmp.name, 'build_intent.txt')
        server.DEATH_FILE = os.path.join(self.tmp.name, 'death_latest.txt')
        server.DEATH_META_FILE = os.path.join(self.tmp.name, 'death_latest.json')
        server.COACH_FILE = os.path.join(self.tmp.name, 'coach_latest.txt')
        server.COACH_META_FILE = os.path.join(self.tmp.name, 'coach_latest.json')
        server.EPOCH_FILE = os.path.join(self.tmp.name, 'game_epoch.json')
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
        for name, value in self._orig.items():
            setattr(server, name, value)
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

    def test_plan_edit_revision_conflict_is_409(self):
        _status, raw = self._request('/api/plan/raw')
        status, body = self._request('/api/plan/edit', {'text': 'PLAN[Warwick]: C -> D'})
        self.assertEqual(status, 200)
        status, body = self._request(
            '/api/plan/edit', {'text': 'PLAN[Ahri]: E -> F', 'revision': raw['revision']})
        self.assertEqual(status, 409)
        self.assertEqual(body['error'], 'stale_revision')

    def test_plan_edit_accepts_matching_revision(self):
        _status, raw = self._request('/api/plan/raw')
        status, body = self._request(
            '/api/plan/edit', {'text': 'PLAN[Warwick]: C -> D', 'revision': raw['revision']})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])

    def test_text_plain_write_is_rejected(self):
        req = urllib.request.Request(
            self.base + '/api/events', data=json.dumps({'kind': 'heartbeat'}).encode('utf-8'),
            headers={'Content-Type': 'text/plain'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 415)

    def test_origin_requires_token(self):
        status, body = self._request('/api/events', {'kind': 'heartbeat'},
                                     headers={'Origin': 'http://127.0.0.1'})
        self.assertEqual(status, 403)
        self.assertEqual(body['error'], 'bad_token')

    def test_origin_with_token_is_allowed(self):
        status, body = self._request(
            '/api/events', {'kind': 'heartbeat'},
            headers={'Origin': 'http://127.0.0.1',
                     server.WRITE_TOKEN_HEADER: server._WRITE_TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])

    def test_action_endpoint_shape(self):
        status, body = self._request('/api/action')
        self.assertEqual(status, 200)
        self.assertIn('ok', body)
        self.assertIn('action', body)
        self.assertIn('status', body)
        self.assertIn(body['status'], ('ok', 'no_action', 'expired'))

    def test_coverage_and_token_endpoints(self):
        status, body = self._request('/api/timeline/current')
        self.assertEqual(status, 200)
        self.assertIn('coverage', body)
        self.assertIn('quarantine', body)
        for key in ('observedSeconds', 'gaps', 'partial'):
            self.assertIn(key, body['coverage'])
        status, body = self._request('/api/coverage')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        status, body = self._request('/api/token')
        self.assertEqual(status, 200)
        self.assertEqual(body['token'], server._WRITE_TOKEN)
        self.assertFalse(body['requiredWithOrigin'] is None)

    def test_death_endpoint_reports_mismatch(self):
        now = time.time()
        with open(server.DEATH_FILE, 'w', encoding='utf-8') as f:
            f.write('fresh text')
        with open(server.DEATH_META_FILE, 'w', encoding='utf-8') as f:
            json.dump({'schema': 'riftsense.v1', 'kind': 'death', 'status': 'ok',
                       'sessionId': 'old', 'observedAt': '2026-01-01T00:00:00Z',
                       'facts': ['fact'], 'hypotheses': [], 'doNow': 'old fact'}, f)
        os.utime(server.DEATH_FILE, (now, now))
        os.utime(server.DEATH_META_FILE, (now - 60, now - 60))
        status, body = self._request('/api/death')
        self.assertEqual(status, 200)
        self.assertTrue(body['mismatch'])
        self.assertIsNone(body['structured'])


class EpochMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_epoch = server.EPOCH_FILE
        server.EPOCH_FILE = os.path.join(self.tmp.name, 'game_epoch.json')

    def tearDown(self):
        server.EPOCH_FILE = self._orig_epoch
        self.tmp.cleanup()

    def _payload(self, game_time=100.0):
        return {
            'gameData': {'gameTime': game_time, 'gameMode': 'CLASSIC', 'mapNumber': 11},
            'activePlayer': {'riotId': 'Me#EUW', 'currentGold': 500.0},
            'allPlayers': [{'riotId': 'Me#EUW', 'championName': 'Nunu & Willump',
                            'team': 'ORDER', 'summonerName': 'Me'}],
        }

    def _stored(self):
        with open(server.EPOCH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_legacy_epoch_without_session_is_migrated(self):
        now = int(time.time() * 1000)
        with open(server.EPOCH_FILE, 'w', encoding='utf-8') as f:
            json.dump({'start': now - 100000, 'champ': 'Nunu & Willump',
                       'observedAt': now - 1000}, f)
        rec = server.game_context((self._payload(), None))
        self.assertTrue(rec['session'])
        self.assertEqual(self._stored()['session'], rec['session'])
        again = server.game_context((self._payload(), None))
        self.assertEqual(again['session'], rec['session'])

    def test_epoch_write_aborts_when_lock_unavailable(self):
        now = int(time.time() * 1000)
        with open(server.EPOCH_FILE, 'w', encoding='utf-8') as f:
            json.dump({'start': now - 100000, 'champ': 'Nunu & Willump',
                       'observedAt': now - 1000, 'gameTime': 100.0}, f)
        with mock.patch.object(server, '_acquire_file_lock', return_value=False):
            rec = server.game_context((self._payload(), None))
        self.assertNotIn('session', self._stored())
        self.assertTrue(rec['session'])


if __name__ == '__main__':
    unittest.main()
