"""Tests for ui/packs.py and knowledge/packs/*.json."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import packs  # noqa: E402

REAL_DIR = os.path.join(ROOT, 'knowledge', 'packs')


def fixture_pack(**overrides):
    pack = {
        'schema': 'riftsense.pack.v1',
        'champion': 'Nunu & Willump',
        'aliases': ['Nunu', 'Willump'],
        'roles': ['jungle'],
        'patch': '26.19',
        'retrieved': '2026-09-23',
        'mechanics': [
            {'name': 'Q', 'value': '400/600/800/1000/1200 true damage', 'source': 'fixture', 'verified': True},
            {'name': 'E total', 'value': '+108% AP derived, tick count unproven', 'source': 'fixture', 'verified': False},
        ],
        'itemNotes': [
            {'name': "Liandry's", 'note': '6% max HP burn', 'source': 'fixture', 'verified': True},
        ],
        'decisionRules': [
            {'rule': 'Start dragon', 'preconditions': ['vision', 'priority'], 'source': 'fixture', 'verified': True},
            {'rule': 'Gank heuristic', 'preconditions': [], 'source': 'fixture', 'verified': False},
        ],
        'counters': [
            {'name': 'Heartsteel', 'note': 'buy Liandry first', 'source': 'fixture', 'verified': True},
        ],
    }
    pack.update(overrides)
    return pack


class LoadPacksTests(unittest.TestCase):
    def test_real_packs_load(self):
        loaded = packs.load_packs()
        self.assertIn('nunuandwillump', loaded)
        self.assertIn('warwick', loaded)
        for pack in loaded.values():
            self.assertEqual(pack['schema'], 'riftsense.pack.v1')
            self.assertTrue(pack['file'].endswith('.json'))

    def test_fixture_dir_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'nunu.json'), 'w', encoding='utf-8') as f:
                json.dump(fixture_pack(), f)
            loaded = packs.load_packs(tmp)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded['nunuandwillump']['champion'], 'Nunu & Willump')

    def test_invalid_and_foreign_schema_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'broken.json'), 'w', encoding='utf-8') as f:
                f.write('{not json')
            with open(os.path.join(tmp, 'other.json'), 'w', encoding='utf-8') as f:
                json.dump({'schema': 'other.v9', 'champion': 'Zed'}, f)
            with open(os.path.join(tmp, 'ok.json'), 'w', encoding='utf-8') as f:
                json.dump(fixture_pack(), f)
            loaded = packs.load_packs(tmp)
        self.assertEqual(list(loaded), ['nunuandwillump'])

    def test_missing_directory_is_empty(self):
        self.assertEqual(packs.load_packs(os.path.join(ROOT, 'no_such_dir')), {})

    def test_list_packs_shape(self):
        rows = packs.list_packs()
        champions = {row['champion'] for row in rows}
        self.assertIn('Nunu & Willump', champions)
        self.assertIn('Warwick', champions)
        for row in rows:
            self.assertIsInstance(row['roles'], list)
            self.assertTrue(row['patch'])


class SelectPackTests(unittest.TestCase):
    def setUp(self):
        self.packs = {'nunuandwillump': fixture_pack()}

    def test_exact_name(self):
        self.assertEqual(packs.select_pack('Nunu & Willump', packs=self.packs)['champion'],
                         'Nunu & Willump')

    def test_normalized_names(self):
        for name in ('nunu & willump', 'NUNU AND WILLUMP', '  Nunu &Willump  ', 'Nunu', 'willump'):
            self.assertIsNotNone(packs.select_pack(name, packs=self.packs), name)

    def test_role_normalization(self):
        self.assertIsNotNone(packs.select_pack('Nunu & Willump', 'jungle', packs=self.packs))
        self.assertIsNotNone(packs.select_pack('Nunu & Willump', 'JGL', packs=self.packs))
        self.assertIsNone(packs.select_pack('Nunu & Willump', 'middle', packs=self.packs))

    def test_empty_roles_match_any_role(self):
        pack = fixture_pack(roles=[])
        result = packs.select_pack('Nunu', 'top', packs={'nunuandwillump': pack})
        self.assertIsNotNone(result)

    def test_missing_champion_returns_none(self):
        for name in ('Teemo', '', '   ', None):
            self.assertIsNone(packs.select_pack(name, packs=self.packs), repr(name))

    def test_real_pack_selection(self):
        self.assertIsNotNone(packs.select_pack('Nunu & Willump'))
        self.assertIsNotNone(packs.select_pack('nunu', 'jungle'))
        self.assertIsNotNone(packs.select_pack('Warwick', 'JGL'))
        self.assertIsNone(packs.select_pack('Warwick', 'support'))


class PackPromptTests(unittest.TestCase):
    def setUp(self):
        self.pack = fixture_pack()

    def test_none_pack_is_empty(self):
        self.assertEqual(packs.pack_prompt(None), '')
        self.assertEqual(packs.pack_prompt('not a pack'), '')

    def test_header_and_sections(self):
        text = packs.pack_prompt(self.pack, 2000)
        self.assertIn('=== CHAMPION PACK (versioned) ===', text)
        self.assertIn('Nunu & Willump | jungle | patch 26.19 | retrieved 2026-09-23', text)
        self.assertIn('MECHANICS', text)
        self.assertIn('DECISION RULES', text)
        self.assertIn('COUNTERS', text)
        self.assertIn('ITEM NOTES', text)

    def test_unverified_flags_preserved(self):
        text = packs.pack_prompt(self.pack, 2000)
        self.assertIn(packs.UNVERIFIED_TAG, text)
        unverified_line = [line for line in text.splitlines() if line.startswith('- E total')][0]
        self.assertIn(packs.UNVERIFIED_TAG, unverified_line)
        verified_line = [line for line in text.splitlines() if line.startswith('- Q:')][0]
        self.assertNotIn('UNVERIFIED', verified_line)

    def test_preconditions_rendered(self):
        text = packs.pack_prompt(self.pack, 2000)
        self.assertIn('[pre: vision; priority]', text)

    def test_prompt_size_cap(self):
        for limit in (60, 200, 400, 1800):
            self.assertLessEqual(len(packs.pack_prompt(self.pack, limit)), limit)
        full = packs.pack_prompt(self.pack, 1800)
        self.assertLessEqual(len(full), 1800)
        self.assertGreater(len(full), 0)

    def test_real_prompt_fits_default_cap(self):
        pack = packs.select_pack('Nunu & Willump')
        text = packs.pack_prompt(pack)
        self.assertLessEqual(len(text), packs.DEFAULT_MAX_CHARS)
        self.assertIn('DECISION RULES', text)
        self.assertIn('...[truncated]', text)


class CliTests(unittest.TestCase):
    def test_prompt_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--prompt', '--champ', 'Nunu', '--max-chars', '200'])
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.getvalue()), 200)

    def test_prompt_cli_missing_pack(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = packs._main(['--prompt', '--champ', 'Teemo'])
        self.assertEqual(code, 1)

    def test_list_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--list'])
        self.assertEqual(code, 0)
        self.assertIn('Warwick', out.getvalue())


class RealPackContentTests(unittest.TestCase):
    def _real(self):
        packs_list = []
        for name in sorted(os.listdir(REAL_DIR)):
            if not name.endswith('.json'):
                continue
            with open(os.path.join(REAL_DIR, name), encoding='utf-8') as handle:
                packs_list.append(json.load(handle))
        return packs_list

    def test_required_fields_and_types(self):
        for pack in self._real():
            for field in ('champion', 'roles', 'patch', 'retrieved', 'mechanics',
                          'itemNotes', 'decisionRules', 'counters'):
                self.assertIn(field, pack, pack.get('champion'))
            for entry in pack['mechanics']:
                self.assertIsInstance(entry.get('value'), str)
                self.assertTrue(entry.get('source'))
                self.assertIsInstance(entry.get('verified'), bool)
            for entry in pack['decisionRules']:
                self.assertIsInstance(entry.get('preconditions'), list)
                self.assertIsInstance(entry.get('verified'), bool)

    def test_packs_are_ascii(self):
        for name in sorted(os.listdir(REAL_DIR)):
            if not name.endswith('.json'):
                continue
            with open(os.path.join(REAL_DIR, name), 'rb') as f:
                self.assertLess(max(f.read()), 128, name)

    def test_nunu_corrected_numbers_and_uncertainty(self):
        pack = packs.select_pack('Nunu & Willump')
        text = packs.pack_prompt(pack, 10000)
        self.assertIn('+108% AP', text)
        self.assertIn('Dragon Slayer', text)
        self.assertIn(packs.UNVERIFIED_TAG, text)
        self.assertNotIn('Solstice Sleigh: your W/E slow triggers', text)


if __name__ == '__main__':
    unittest.main()
