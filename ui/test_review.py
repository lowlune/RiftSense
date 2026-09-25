import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from ui import review, timeline

ITEM_NAMES = {1001: 'Boots of Speed', 3153: 'Hextech Rocketbelt', 3089: "Rabadon's Deathcap"}
ITEM_INTO = {1001: [3153], 3153: [], 3089: []}
ITEM_COSTS = {1001: 300, 3153: 2500, 3089: 3600}


def review_kwargs():
    return {'item_names': ITEM_NAMES, 'item_into': ITEM_INTO, 'item_costs': ITEM_COSTS}


class ReviewTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        timeline.configure(os.path.join(self.tmp.name, 'timeline.db'))
        timeline.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ended_game_with_events(self):
        game = timeline.ensure_game('ses-1', champ='Nunu & Willump', mode='CLASSIC',
                                    map_number=11)
        game_id = game['id']
        timeline.record_event('death', 'died 3:00 to Zed',
                              {'clock': '3:00', 'killer': 'Zed'}, t_game=180,
                              session_id='ses-1')
        timeline.record_event('death', 'died 8:20 to Lee Sin',
                              {'clock': '8:20', 'killer': 'Lee Sin'}, t_game=500,
                              session_id='ses-1')
        timeline.record_event('death', 'died 9:40 to Ahri',
                              {'clock': '9:40', 'killer': 'Ahri'}, t_game=580,
                              session_id='ses-1')
        timeline.record_event('death', 'died 21:40 to Garen',
                              {'clock': '21:40', 'killer': 'Garen'}, t_game=1300,
                              session_id='ses-1')
        timeline.record_event('item', 'inventory changed: 1001x1', t_game=90,
                              session_id='ses-1')
        timeline.record_event('item', 'inventory changed: 1001x1,3153x1', t_game=600,
                              session_id='ses-1')
        timeline.record_event('objective', 'dragon up soon', {'secondsLeft': 20},
                              t_game=270, session_id='ses-1')
        timeline.record_advice('coach', 'reset and buy', t_game=200, session_id='ses-1')
        timeline.record_inference('req-1', 'started', session_id='ses-1')
        timeline.record_inference('req-1', 'ok', session_id='ses-1',
                                  tokens_in=10, tokens_out=5)
        timeline.end_game('ses-1')

        result = review.build_review(game_id, **review_kwargs())
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['game']['id'], game_id)
        self.assertEqual(result['game']['champ'], 'Nunu & Willump')
        self.assertIsNotNone(result['game']['endedAt'])

        texts = [obs['text'] for obs in result['observations']]
        self.assertTrue(any(obs['kind'] == 'deaths' for obs in result['observations']))
        deaths_text = next(t for t in texts if t.startswith('Deaths:'))
        self.assertIn('4 recorded', deaths_text)
        self.assertIn('3:00 to Zed', deaths_text)
        self.assertIn('21:40 to Garen', deaths_text)
        completed_text = next(t for t in texts if t.startswith('Items completed:'))
        self.assertIn('10:00 Hextech Rocketbelt', completed_text)
        self.assertTrue(any(t.startswith('Advice: 1 entries') for t in texts))
        self.assertTrue(any('Inference: 1 requests' in t for t in texts))

        self.assertEqual(len(result['priorities']), 3)
        ids = [p['id'] for p in result['priorities']]
        self.assertIn('early_deaths', ids)
        self.assertIn('death_cluster', ids)
        for priority in result['priorities']:
            self.assertTrue(priority['title'])
            self.assertTrue(priority['focus'])
            self.assertTrue(priority['evidence'])
        early = next(p for p in result['priorities'] if p['id'] == 'early_deaths')
        self.assertIn('3 of 4 recorded deaths before 10:00', early['evidence'][0])

        self.assertTrue(result['unavailable'])
        self.assertTrue(any('Damage dealt' in u for u in result['unavailable']))

    def test_latest_ended_game_defaults_to_most_recent(self):
        first = timeline.ensure_game('ses-a', champ='Warwick', mode='CLASSIC')
        timeline.record_event('death', 'died 2:00 to Zed', {'clock': '2:00', 'killer': 'Zed'},
                              t_game=120, session_id='ses-a')
        timeline.end_game('ses-a')
        second = timeline.ensure_game('ses-b', champ='Ahri', mode='CLASSIC', force_new=True)
        timeline.end_game('ses-b')

        result = review.build_review(**review_kwargs())
        self.assertTrue(result['ok'])
        self.assertEqual(result['game']['id'], second['id'])
        self.assertNotEqual(first['id'], second['id'])

    def test_empty_db_returns_no_review(self):
        result = review.build_review()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_review')
        self.assertIsNone(result['game'])
        self.assertEqual(result['priorities'], [])
        self.assertTrue(result['unavailable'])

    def test_missing_or_unfinished_game_returns_no_review(self):
        timeline.ensure_game('ses-open', champ='Nunu & Willump', mode='CLASSIC')
        result = review.build_review()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_review')

        result = review.build_review(9999)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_review')

        result = review.build_review('not-a-number')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'no_review')

    def test_game_with_no_deaths(self):
        timeline.ensure_game('ses-clean', champ='Warwick', mode='CLASSIC', map_number=11)
        timeline.record_event('item', 'inventory changed: 1001x1', t_game=100,
                              session_id='ses-clean')
        timeline.end_game('ses-clean')

        result = review.build_review(**review_kwargs())
        self.assertTrue(result['ok'])
        deaths_text = next(obs['text'] for obs in result['observations']
                           if obs['kind'] == 'deaths')
        self.assertEqual(deaths_text, 'Deaths: none recorded.')
        ids = [p['id'] for p in result['priorities']]
        self.assertEqual(len(ids), 3)
        self.assertIn('no_deaths', ids)
        self.assertNotIn('early_deaths', ids)

    def test_recurring_early_deaths_across_games(self):
        for session in ('ses-r1', 'ses-r2'):
            timeline.ensure_game(session, champ='Nunu & Willump', mode='CLASSIC')
            timeline.record_event('death', 'died 1:40 to Zed', {'killer': 'Zed'},
                                  t_game=100, session_id=session)
            timeline.record_event('death', 'died 6:00 to Lee Sin', {'killer': 'Lee Sin'},
                                  t_game=360, session_id=session)
            timeline.end_game(session)
        timeline.ensure_game('ses-r3', champ='Nunu & Willump', mode='CLASSIC')
        timeline.end_game('ses-r3')

        result = review.build_review(**review_kwargs())
        ids = [p['id'] for p in result['priorities']]
        self.assertIn('recurring_early_deaths', ids)
        recurring = next(p for p in result['priorities']
                         if p['id'] == 'recurring_early_deaths')
        self.assertIn('2 of the last 2 games', recurring['evidence'][0])

    def test_malformed_rows_are_survivable(self):
        con = sqlite3.connect(timeline.DB_PATH)
        try:
            cur = con.execute(
                'INSERT INTO games (session_id, started_at, ended_at, champ, mode, map)'
                ' VALUES (?,?,?,?,?,?)',
                ('bad-session', None, 1700000000000, 'Ahri', 'CLASSIC', 11))
            game_id = cur.lastrowid
            con.execute(
                'INSERT INTO events (game_id, t_game, at_ts, kind, label, json)'
                ' VALUES (?,?,?,?,?,?)',
                (game_id, None, 1, 'death', 'died ??? to ???', '{not json'))
            con.execute(
                'INSERT INTO events (game_id, t_game, at_ts, kind, label, json)'
                ' VALUES (?,?,?,?,?,?)',
                (game_id, -5, 1, 'item', 'inventory changed: garbage, 12x', None))
            con.execute(
                'INSERT INTO events (game_id, t_game, at_ts, kind, label, json)'
                ' VALUES (?,?,?,?,?,?)',
                (game_id, 100, 1, 'item', None, None))
            con.execute(
                'INSERT INTO events (game_id, t_game, at_ts, kind, label, json)'
                ' VALUES (?,?,?,?,?,?)',
                (game_id, None, 1, 'objective', None, None))
            con.execute(
                'INSERT INTO events (game_id, t_game, at_ts, kind, label, json)'
                ' VALUES (?,?,?,?,?,?)',
                (game_id, 50, 1, 'mystery', 'weird row', None))
            con.execute(
                'INSERT INTO advice (game_id, t_game, at_ts, kind, text, session_id, meta_json)'
                ' VALUES (?,?,?,?,?,?,?)',
                (game_id, None, 1, 'weird', None, None, None))
            con.execute(
                'INSERT INTO inference (game_id, request_id, started_at, finished_at, status,'
                ' tokens_in, tokens_out, cost) VALUES (?,?,?,?,?,?,?,?)',
                (game_id, None, None, None, None, None, None, None))
            con.commit()
        finally:
            con.close()

        result = review.build_review(**review_kwargs())
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['priorities']), 3)
        self.assertTrue(any('no game clock' in item for item in result['unavailable']))
        self.assertTrue(any(obs['kind'] == 'deaths' for obs in result['observations']))


class ReviewAuditTests(unittest.TestCase):
    """Regressions for B9 (coverage-gated review), B10 (reminders vs
    outcomes), B12 (anchored history and versions)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        timeline.configure(os.path.join(self.tmp.name, 'timeline.db'))
        timeline.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_deaths_without_coverage_withholds_praise(self):
        game = timeline.ensure_game('ses-nocov', champ='Warwick', mode='CLASSIC',
                                    map_number=11, force_new=True)
        timeline.record_event('item', 'inventory changed: 1001x1', t_game=100,
                              session_id='ses-nocov')
        timeline.end_game('ses-nocov')

        result = review.build_review(game['id'], **review_kwargs())
        self.assertTrue(result['ok'])
        prios = {p['id']: p for p in result['priorities']}
        self.assertIn('no_deaths', prios)
        self.assertNotEqual(prios['no_deaths']['title'], 'Keep the clean death record')
        self.assertIn('coverage incomplete', prios['no_deaths']['title'].lower())
        blob = ' '.join('%s %s' % (p['title'], p['focus'])
                        for p in result['priorities']).lower()
        self.assertNotIn('kept the game safe', blob)
        self.assertNotIn('played safely', blob)
        self.assertIsNone(result['coverage'])
        self.assertEqual(result['dataQuality']['level'], 'unavailable')
        self.assertTrue(any('Collection coverage was not recorded' in u
                            for u in result['unavailable']))
        deaths = next(o['text'] for o in result['observations']
                      if o['kind'] == 'deaths')
        self.assertEqual(deaths, 'Deaths: none recorded.')
        self.assertFalse(result['versions']['available'])
        self.assertTrue(any('catalog and game-rules version' in u
                            for u in result['unavailable']))

    def test_zero_deaths_with_adequate_coverage_keeps_record(self):
        game = timeline.ensure_game('ses-covered', champ='Warwick', mode='CLASSIC',
                                    force_new=True)
        timeline.record_event('item', 'inventory changed: 1001x1', t_game=100,
                              session_id='ses-covered')
        timeline.end_game('ses-covered')

        fake = lambda game_id: {'intervals': [[0, 1800]], 'adequate': True,
                                'observedRatio': 0.9}
        with mock.patch.object(review.timeline, 'coverage', fake, create=True):
            result = review.build_review(game['id'], **review_kwargs())
        self.assertTrue(result['ok'])
        self.assertIsNotNone(result['coverage'])
        self.assertTrue(result['coverage']['adequate'])
        self.assertEqual(result['coverage']['source'], 'timeline.coverage')
        self.assertEqual(result['dataQuality']['level'], 'ok')
        prios = {p['id']: p for p in result['priorities']}
        self.assertIn('no_deaths', prios)
        self.assertEqual(prios['no_deaths']['title'], 'Keep the clean death record')
        self.assertTrue(any('95%' in evidence or '90%' in evidence
                            for evidence in prios['no_deaths']['evidence']))

    def test_objective_reminders_and_outcomes_are_separate(self):
        game = timeline.ensure_game('ses-obj', champ='Ahri', mode='CLASSIC',
                                    force_new=True)
        timeline.record_event('objective', 'dragon up soon', {'secondsLeft': 30},
                              t_game=300, session_id='ses-obj')
        timeline.record_event('objective', 'baron killed at 21:00', {'outcome': True},
                              t_game=1260, session_id='ses-obj')
        timeline.end_game('ses-obj')

        result = review.build_review(game['id'], **review_kwargs())
        obs = {o['kind']: o['text'] for o in result['observations']}
        self.assertIn('Objective-window reminders recorded', obs['objective_reminders'])
        self.assertIn('dragon up soon', obs['objective_reminders'])
        self.assertNotIn('killed', obs['objective_reminders'])
        self.assertIn('Objective outcomes recorded', obs['objective_outcomes'])
        self.assertIn('baron killed at 21:00', obs['objective_outcomes'])

    def test_history_is_anchored_to_the_reviewed_game(self):
        first = timeline.ensure_game('ses-hist-a', champ='A', force_new=True)
        for _ in range(2):
            timeline.record_event('death', 'died 1:40 to Zed',
                                  {'killer': 'Zed'}, t_game=100, session_id='ses-hist-a')
        timeline.end_game('ses-hist-a')

        second = timeline.ensure_game('ses-hist-b', champ='B', force_new=True)
        for _ in range(2):
            timeline.record_event('death', 'died 1:50 to Ahri',
                                  {'killer': 'Ahri'}, t_game=110, session_id='ses-hist-b')
        timeline.end_game('ses-hist-b')

        third = timeline.ensure_game('ses-hist-c', champ='C', force_new=True)
        timeline.end_game('ses-hist-c')

        reviewed_b = review.build_review(second['id'], **review_kwargs())
        ids_b = [p['id'] for p in reviewed_b['priorities']]
        self.assertIn('early_deaths', ids_b)
        self.assertNotIn('recurring_early_deaths', ids_b)
        self.assertTrue(any('before the reviewed game' in u
                            for u in reviewed_b['unavailable']))

        reviewed_c = review.build_review(third['id'], **review_kwargs())
        ids_c = [p['id'] for p in reviewed_c['priorities']]
        self.assertIn('recurring_early_deaths', ids_c)
        self.assertGreater(first['id'], 0)

    def test_intervals_table_gates_zero_death_praise(self):
        game = timeline.ensure_game('ses-int', champ='Warwick', mode='CLASSIC',
                                    force_new=True)
        timeline.end_game('ses-int')
        con = sqlite3.connect(timeline.DB_PATH)
        try:
            con.execute('CREATE TABLE intervals (game_id INTEGER, start_ms INTEGER,'
                        ' end_ms INTEGER)')
            con.execute('INSERT INTO intervals (game_id, start_ms, end_ms) VALUES (?,?,?)',
                        (game['id'], 0, 600000))
            con.execute('UPDATE games SET started_at=?, ended_at=? WHERE id=?',
                        (1000000, 2000000, game['id']))
            con.commit()
        finally:
            con.close()

        partial = review.build_review(game['id'], **review_kwargs())
        self.assertIsNotNone(partial['coverage'])
        self.assertFalse(partial['coverage']['adequate'])
        self.assertEqual(partial['coverage']['observedRatio'], 0.6)
        self.assertEqual(partial['dataQuality']['level'], 'partial')
        partial_prios = {p['id']: p for p in partial['priorities']}
        self.assertNotEqual(partial_prios['no_deaths']['title'],
                            'Keep the clean death record')

        con = sqlite3.connect(timeline.DB_PATH)
        try:
            con.execute('UPDATE intervals SET end_ms=? WHERE game_id=?',
                        (900000, game['id']))
            con.commit()
        finally:
            con.close()

        adequate = review.build_review(game['id'], **review_kwargs())
        self.assertTrue(adequate['coverage']['adequate'])
        self.assertEqual(adequate['coverage']['source'], 'intervals:intervals')
        self.assertEqual(adequate['dataQuality']['level'], 'ok')
        adequate_prios = {p['id']: p for p in adequate['priorities']}
        self.assertEqual(adequate_prios['no_deaths']['title'],
                         'Keep the clean death record')

    def test_millisecond_api_intervals_are_scaled(self):
        game = timeline.ensure_game('ses-ms', champ='Ahri', mode='CLASSIC',
                                    force_new=True)
        timeline.end_game('ses-ms')
        con = sqlite3.connect(timeline.DB_PATH)
        try:
            con.execute('UPDATE games SET started_at=?, ended_at=? WHERE id=?',
                        (1000000, 2000000, game['id']))
            con.commit()
        finally:
            con.close()

        fake = lambda game_id: {'intervals': [{'startMs': 0, 'endMs': 600000}]}
        with mock.patch.object(review.timeline, 'coverage', fake, create=True):
            result = review.build_review(game['id'], **review_kwargs())
        self.assertEqual(result['coverage']['observedRatio'], 0.6)
        self.assertEqual(result['coverage']['intervals'][0]['end'], 600.0)
        self.assertFalse(result['coverage']['adequate'])

    def test_recorded_catalog_rules_version_is_surfaced(self):
        game = timeline.ensure_game('ses-ver', champ='Ahri', mode='CLASSIC',
                                    force_new=True)
        timeline.end_game('ses-ver')
        con = sqlite3.connect(timeline.DB_PATH)
        try:
            con.execute('ALTER TABLE games ADD COLUMN catalog_version TEXT')
            con.execute('ALTER TABLE games ADD COLUMN rules_version TEXT')
            con.execute('UPDATE games SET catalog_version=?, rules_version=? WHERE id=?',
                        ('16.19.1', '2026-09', game['id']))
            con.commit()
        finally:
            con.close()

        result = review.build_review(game['id'], **review_kwargs())
        self.assertTrue(result['versions']['available'])
        self.assertEqual(result['versions']['catalog'], '16.19.1')
        self.assertEqual(result['versions']['rules'], '2026-09')
        self.assertFalse(any('catalog and game-rules version' in u
                             for u in result['unavailable']))


if __name__ == '__main__':
    unittest.main()
