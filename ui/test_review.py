import os
import sqlite3
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
