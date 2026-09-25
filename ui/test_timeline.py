import os
import sqlite3
import tempfile
import threading
import unittest

from ui import timeline


class TimelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        timeline.configure(os.path.join(self.tmp.name, 'timeline.db'))
        timeline.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_created(self):
        con = sqlite3.connect(timeline.DB_PATH)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertIn('games', names)
        self.assertIn('events', names)
        self.assertIn('advice', names)
        self.assertIn('inference', names)

    def test_record_and_recent_ordering(self):
        timeline.ensure_game('ses-1', champ='Nunu & Willump', mode='CLASSIC', map_number=11)
        timeline.record_event('death', 'died at 5:00', {'killer': 'Zed'}, t_game=300, session_id='ses-1')
        timeline.record_event('item', 'BotRK complete', t_game=420, session_id='ses-1')
        timeline.record_advice('coach', 'take dragon', t_game=430, session_id='ses-1')
        data = timeline.recent(10)
        self.assertTrue(data['ok'])
        self.assertEqual([e['kind'] for e in data['events']], ['death', 'item'])
        self.assertEqual(len(data['advice']), 1)
        self.assertEqual(data['advice'][0]['text'], 'take dragon')

    def test_game_lifecycle_switches_session(self):
        first = timeline.ensure_game('ses-1', champ='Nunu & Willump')
        second = timeline.ensure_game('ses-2', champ='Warwick', force_new=True)
        self.assertNotEqual(first['id'], second['id'])
        current = timeline.current()
        self.assertEqual(current['game']['champ'], 'Warwick')
        timeline.end_game('ses-2')
        self.assertIsNone(timeline.current()['game'])

    def test_inference_budget_counts_and_updates(self):
        timeline.ensure_game('ses-9')
        timeline.record_inference('req-1', 'started', session_id='ses-9')
        timeline.record_inference('req-1', 'ok', session_id='ses-9', tokens_in=10, tokens_out=5)
        timeline.record_inference('req-2', 'failed', session_id='ses-9')
        budget = timeline.current()['inference']
        self.assertEqual(budget['used'], 2)
        self.assertEqual(budget['remaining'], budget['limit'] - 2)

    def test_concurrent_read_while_writing(self):
        timeline.ensure_game('ses-c')
        errors = []

        def writer():
            try:
                for i in range(40):
                    timeline.record_event('heartbeat', 'beat %d' % i, t_game=i, session_id='ses-c')
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        for _ in range(20):
            timeline.recent(5)
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(timeline.recent(100)['events']), 40)

    def test_malformed_input_is_sanitized(self):
        timeline.ensure_game('ses-bad')
        result = timeline.record_event('not-a-kind', 'x' * 5000, object(), t_game='nope', session_id='ses-bad')
        self.assertTrue(result['ok'])
        self.assertEqual(result['kind'], 'status')
        advice = timeline.record_advice('not-a-kind', 'y' * 9000, session_id='ses-bad')
        self.assertEqual(advice['kind'], 'note')
        data = timeline.recent(5)
        self.assertLessEqual(len(data['events'][0]['label']), 300)
        self.assertLessEqual(len(data['advice'][0]['text']), timeline.MAX_TEXT)
        self.assertIsNone(data['events'][0]['t_game'])


if __name__ == '__main__':
    unittest.main()
