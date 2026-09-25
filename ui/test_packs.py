"""Tests for ui/packs.py and knowledge/packs/*.json."""

import contextlib
import hashlib
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

CORE_SECTIONS = ('DECISION RULES', 'MECHANICS', 'COUNTERS', 'ITEM NOTES')
ALL_SECTIONS = CORE_SECTIONS + ('DERIVED', 'HEURISTICS', 'STATISTICS', 'ABSTENTIONS')


def fixture_pack(**overrides):
    pack = {
        'schema': 'riftsense.pack.v2',
        'champion': 'Nunu & Willump',
        'aliases': ['Nunu', 'Willump'],
        'roles': ['jungle'],
        'patch': '26.19',
        'retrieved': '2026-09-23',
        'mechanics': [
            {'name': 'Q', 'value': '400/600/800/1000/1200 true damage',
             'source': 'https://wiki.example/q', 'verified': True},
            {'name': 'E total', 'value': '+108% AP derived, tick count unproven',
             'source': 'https://wiki.example/e', 'verified': False},
        ],
        'derived': [
            {'name': 'E max', 'value': '9 hits x 12% AP = +108% AP',
             'source': 'derived from https://wiki.example/e'},
        ],
        'heuristics': [
            {'name': 'Give drake', 'note': 'prefer Herald when 2+ levels behind',
             'source': 'heuristic'},
        ],
        'statistics': [
            {'name': 'Boot win rate', 'note': '55.03% vs 50.17% raw',
             'source': 'https://op.gg/example', 'sample': 'sample size not disclosed'},
        ],
        'itemNotes': [
            {'name': "Liandry's", 'note': '6% max HP burn',
             'source': 'https://wiki.example/liandry', 'verified': True},
        ],
        'decisionRules': [
            {'rule': 'Start dragon', 'preconditions': ['vision', 'priority'],
             'triggers': ['dragon'], 'source': 'https://wiki.example/dragon',
             'verified': True},
            {'rule': 'Gank heuristic', 'preconditions': [], 'triggers': ['gank'],
             'source': 'https://wiki.example/gank', 'verified': False},
        ],
        'counters': [
            {'name': 'Heartsteel', 'note': 'buy Liandry first',
             'source': 'https://wiki.example/heartsteel', 'verified': True},
        ],
        'abstentions': [
            {'when': 'secure call', 'missing': 'Smite stage', 'reason': 'burst unknown'},
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
            self.assertEqual(pack['schema'], 'riftsense.pack.v2')
            self.assertTrue(pack['file'].endswith('.json'))
            self.assertEqual(pack['_violations'], [])

    def test_fixture_dir_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'nunu.json'), 'w', encoding='utf-8') as f:
                json.dump(fixture_pack(), f)
            loaded = packs.load_packs(tmp)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded['nunuandwillump']['champion'], 'Nunu & Willump')

    def test_v1_pack_still_loads(self):
        legacy = fixture_pack(schema='riftsense.pack.v1')
        for key in ('derived', 'heuristics', 'statistics', 'abstentions'):
            legacy.pop(key)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'legacy.json'), 'w', encoding='utf-8') as f:
                json.dump(legacy, f)
            loaded = packs.load_packs(tmp)
        self.assertIn('nunuandwillump', loaded)

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

    def test_load_annotates_and_downgrades_violations(self):
        bad = fixture_pack()
        bad['derived'][0]['verified'] = True
        bad['decisionRules'][0].pop('source')
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'bad.json'), 'w', encoding='utf-8') as f:
                json.dump(bad, f)
            loaded = packs.load_packs(tmp)
        pack = loaded['nunuandwillump']
        self.assertGreaterEqual(len(pack['_violations']), 2)
        self.assertFalse(pack['derived'][0]['verified'])
        self.assertFalse(pack['decisionRules'][0]['verified'])

    def test_list_packs_shape(self):
        rows = packs.list_packs()
        champions = {row['champion'] for row in rows}
        self.assertIn('Nunu & Willump', champions)
        self.assertIn('Warwick', champions)
        for row in rows:
            self.assertIsInstance(row['roles'], list)
            self.assertTrue(row['patch'])
            self.assertTrue(row['id'])
            self.assertEqual(row['violations'], 0)


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

    def test_require_role_refuses_unscoped_calls(self):
        self.assertIsNone(packs.select_pack('Nunu', packs=self.packs, require_role=True))
        self.assertIsNotNone(packs.select_pack('Nunu', 'jungle', packs=self.packs,
                                               require_role=True))

    def test_real_pack_selection(self):
        self.assertIsNotNone(packs.select_pack('Nunu & Willump'))
        self.assertIsNotNone(packs.select_pack('nunu', 'jungle'))
        self.assertIsNotNone(packs.select_pack('Warwick', 'JGL'))
        self.assertIsNone(packs.select_pack('Warwick', 'support'))
        self.assertIsNone(packs.select_pack('Warwick', require_role=True))
        self.assertIsNotNone(packs.select_pack('Warwick', 'jungle', require_role=True))


class PackPromptTests(unittest.TestCase):
    def setUp(self):
        self.pack = fixture_pack()

    def test_none_pack_is_empty(self):
        self.assertEqual(packs.pack_prompt(None), '')
        self.assertEqual(packs.pack_prompt('not a pack'), '')

    def test_backward_compatible_return_type(self):
        text = packs.pack_prompt(self.pack, 2000)
        self.assertIsInstance(text, str)
        self.assertLessEqual(len(text), 2000)

    def test_header_and_sections(self):
        text = packs.pack_prompt(self.pack, 4000)
        self.assertIn('=== CHAMPION PACK (versioned) ===', text)
        self.assertIn('Nunu & Willump | jungle | patch 26.19 | retrieved 2026-09-23', text)
        for section in ALL_SECTIONS:
            self.assertIn(section, text)

    def test_unverified_flags_preserved(self):
        text = packs.pack_prompt(self.pack, 4000)
        self.assertIn(packs.UNVERIFIED_TAG, text)
        unverified_line = [line for line in text.splitlines() if line.startswith('- E total')][0]
        self.assertIn(packs.UNVERIFIED_TAG, unverified_line)
        verified_line = [line for line in text.splitlines() if line.startswith('- Q:')][0]
        self.assertNotIn('UNVERIFIED', verified_line)

    def test_preconditions_rendered(self):
        text = packs.pack_prompt(self.pack, 4000)
        self.assertIn('[pre: vision; priority]', text)

    def test_prompt_size_cap(self):
        for limit in (60, 200, 400, 1800):
            text = packs.pack_prompt(self.pack, limit)
            self.assertLessEqual(len(text), limit)
        full = packs.pack_prompt(self.pack, 1800)
        self.assertLessEqual(len(full), 1800)
        self.assertGreater(len(full), 0)

    def test_manifest_content(self):
        text, manifest = packs.pack_prompt(self.pack, 1800, with_manifest=True)
        self.assertIsInstance(text, str)
        self.assertIsInstance(manifest, dict)
        self.assertEqual(manifest['schema'], 'riftsense.pack.manifest.v1')
        self.assertEqual(manifest['id'], 'nunuandwillump')
        self.assertEqual(manifest['version'], '26.19')
        self.assertEqual(manifest['bytes'], len(text))
        self.assertEqual(manifest['sha256'], hashlib.sha256(text.encode('utf-8')).hexdigest()[:16])
        section_ids = [section['id'] for section in manifest['sections']]
        self.assertEqual(section_ids[0], 'header')
        for category, _header, _weight in packs.SECTION_SPECS:
            self.assertIn(category, section_ids)
        for section in manifest['sections'][1:]:
            self.assertGreater(section['bytes'], 0)
            self.assertGreaterEqual(section['entries'], 1)
            self.assertGreater(section['entries_total'], 0)

    def test_manifest_reports_budget_and_truncation(self):
        text, manifest = packs.pack_prompt(self.pack, 900, with_manifest=True)
        self.assertLessEqual(len(text), 900)
        self.assertTrue(manifest['truncated'])
        self.assertEqual(manifest['state_used'], False)
        self.assertTrue(all(section['budget'] is not None or section['id'] == 'header'
                            for section in manifest['sections']))

    def test_tiny_cap_drops_sections_but_respects_limit(self):
        for limit in (40, 80, 120):
            text, manifest = packs.pack_prompt(self.pack, limit, with_manifest=True)
            self.assertLessEqual(len(text), limit)
            self.assertTrue(manifest['truncated'])
            self.assertLessEqual(manifest['bytes'], limit)

    def test_pack_manifest_helper(self):
        manifest = packs.pack_manifest(self.pack, 1800)
        self.assertEqual(manifest['id'], 'nunuandwillump')
        self.assertEqual(manifest['bytes'], len(packs.pack_prompt(self.pack, 1800)))

    def test_state_prefers_relevant_rules(self):
        state = {'dragon': 'spawning in 20s'}
        text = packs.pack_prompt(self.pack, 4000, state)
        dragon_index = text.index('Start dragon')
        gank_index = text.index('Gank heuristic')
        self.assertLess(dragon_index, gank_index)
        text = packs.pack_prompt(self.pack, 4000, json.dumps({'gank': 'path is clear'}))
        self.assertLess(text.index('Gank heuristic'), text.index('Start dragon'))
        manifest = packs.pack_manifest(self.pack, 1800, state)
        self.assertTrue(manifest['state_used'])

    def test_derived_heuristics_statistics_always_unverified(self):
        text = packs.pack_prompt(self.pack, 4000)
        for prefix in ('- E max', '- Give drake', '- Boot win rate'):
            line = [entry for entry in text.splitlines() if entry.startswith(prefix)][0]
            self.assertIn(packs.UNVERIFIED_TAG, line)

    def test_real_prompt_fits_default_cap(self):
        pack = packs.select_pack('Nunu & Willump')
        text, manifest = packs.pack_prompt(pack, packs.DEFAULT_MAX_CHARS, with_manifest=True)
        self.assertLessEqual(len(text), packs.DEFAULT_MAX_CHARS)
        self.assertIn('...[truncated]', text)
        self.assertEqual(manifest['bytes'], len(text))
        for section in CORE_SECTIONS:
            self.assertIn(section, text)
        for category in ('decisionRules', 'mechanics', 'counters', 'itemNotes'):
            summary = [item for item in manifest['sections'] if item['id'] == category][0]
            self.assertGreater(summary['bytes'], 0)
            self.assertGreaterEqual(summary['entries'], 1)


class ValidationTests(unittest.TestCase):
    def test_real_packs_are_clean(self):
        for pack in packs.load_packs().values():
            self.assertEqual(pack['_violations'], [], pack.get('champion'))

    def test_real_decision_rules_have_full_provenance(self):
        for pack in packs.load_packs().values():
            for entry in pack['decisionRules']:
                self.assertTrue(entry.get('rule'))
                self.assertTrue(entry.get('source'))
                self.assertTrue(entry.get('patch'))
                self.assertTrue(entry.get('retrieved'))
                self.assertIsInstance(entry.get('preconditions'), list)
                self.assertIsInstance(entry.get('verified'), bool)

    def test_verified_categories_are_separated(self):
        for pack in packs.load_packs().values():
            for category in packs.ALWAYS_UNVERIFIED:
                for entry in pack.get(category) or []:
                    self.assertIs(entry.get('verified'), False, (pack['champion'], category))
            for entry in pack['mechanics']:
                if entry.get('verified') is True:
                    blob = json.dumps(entry).lower()
                    for word in packs.DERIVED_WORDS:
                        self.assertNotIn(word, blob, (pack['champion'], entry.get('name')))

    def test_missing_required_fields_reported(self):
        pack = fixture_pack()
        del pack['decisionRules'][0]['preconditions']
        pack['mechanics'][0].pop('source')
        violations = packs.validate_pack(pack)
        messages = ' '.join(violation['message'] for violation in violations)
        self.assertIn('preconditions', messages)
        self.assertIn('source', messages)
        self.assertFalse(pack['mechanics'][0]['verified'])

    def test_verified_rule_without_preconditions_downgraded(self):
        pack = fixture_pack()
        pack['decisionRules'][0]['preconditions'] = []
        violations = packs.validate_pack(pack)
        self.assertTrue(violations)
        self.assertFalse(pack['decisionRules'][0]['verified'])

    def test_derived_mechanics_downgraded(self):
        pack = fixture_pack()
        pack['mechanics'][0]['value'] = 'derived arithmetic: 9 x 12% AP'
        violations = packs.validate_pack(pack)
        self.assertTrue(violations)
        self.assertFalse(pack['mechanics'][0]['verified'])

    def test_missing_patch_retrieved_downgrades_verified_claims(self):
        pack = fixture_pack()
        del pack['patch']
        del pack['retrieved']
        violations = packs.validate_pack(pack)
        fields = {violation['field'] for violation in violations}
        self.assertIn('patch', fields)
        self.assertIn('retrieved', fields)
        for category in ('mechanics', 'itemNotes', 'counters', 'decisionRules'):
            for entry in pack[category]:
                self.assertFalse(entry.get('verified'))

    def test_v2_requires_category_lists(self):
        pack = fixture_pack()
        del pack['derived']
        violations = packs.validate_pack(pack)
        self.assertIn('derived', {violation['field'] for violation in violations})

    def test_statistics_requires_sample(self):
        pack = fixture_pack()
        pack['statistics'][0].pop('sample')
        violations = packs.validate_pack(pack)
        self.assertTrue(any('sample' in violation['message'] for violation in violations))
        self.assertFalse(pack['statistics'][0]['verified'])


class CliTests(unittest.TestCase):
    def test_prompt_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--prompt', '--champ', 'Nunu', '--max-chars', '200'])
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.getvalue()), 200)

    def test_select_cli_require_role(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--select', '--champ', 'Warwick', '--require-role'])
        self.assertEqual(code, 1)
        self.assertIn('no_pack', out.getvalue())

    def test_prompt_cli_json_contract(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--prompt', '--champ', 'Warwick', '--role', 'JGL',
                                '--max-chars', '1200', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['pack']['id'], 'warwick')
        self.assertEqual(payload['pack']['patch'], '26.19')
        self.assertEqual(payload['manifest']['bytes'], len(payload['text']))
        self.assertLessEqual(len(payload['text']), 1200)

    def test_prompt_cli_missing_pack(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--prompt', '--champ', 'Teemo', '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['status'], 'no_pack')

    def test_select_cli_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--select', '--champ', 'Nunu', '--role', 'jungle', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['pack']['id'], 'nunuandwillump')
        self.assertEqual(payload['pack']['roles'], ['jungle'])

    def test_validate_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = packs._main(['--validate', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['ok'])
        self.assertTrue(all(not row['violations'] for row in payload['packs']))

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
            self.assertEqual(pack['schema'], 'riftsense.pack.v2')
            for field in ('champion', 'roles', 'patch', 'retrieved', 'mechanics', 'derived',
                          'heuristics', 'statistics', 'itemNotes', 'decisionRules',
                          'counters', 'abstentions'):
                self.assertIn(field, pack, pack.get('champion'))
            for entry in pack['decisionRules']:
                self.assertIsInstance(entry.get('preconditions'), list)
                self.assertIsInstance(entry.get('verified'), bool)
                self.assertTrue(entry.get('source'))
            for category in ('derived', 'heuristics', 'statistics'):
                for entry in pack[category]:
                    self.assertFalse(entry.get('verified', False),
                                     (pack['champion'], category))

    def test_packs_are_ascii(self):
        for name in sorted(os.listdir(REAL_DIR)):
            if not name.endswith('.json'):
                continue
            with open(os.path.join(REAL_DIR, name), 'rb') as f:
                self.assertLess(max(f.read()), 128, name)

    def test_nunu_q_healing_disambiguated(self):
        pack = packs.select_pack('Nunu & Willump')
        text = packs.pack_prompt(pack, 20000)
        self.assertIn('60% of the non-champion heal formula', text)
        self.assertIn('39/57/75/93/111', text)

    def test_nunu_derived_and_heuristic_separated(self):
        with open(os.path.join(REAL_DIR, 'nunu.json'), encoding='utf-8') as handle:
            pack = json.load(handle)
        mechanics_names = ' '.join(entry['name'] for entry in pack['mechanics'])
        self.assertNotIn('AP arithmetic', mechanics_names)
        self.assertTrue(any('E max total' in entry['name'] for entry in pack['derived']))
        rules = ' '.join(entry['rule'] for entry in pack['decisionRules'])
        self.assertNotIn('risk-preference', rules)
        self.assertTrue(any('risk-preference' in entry['note'] for entry in pack['heuristics']))

    def test_nunu_corrected_numbers_and_uncertainty(self):
        pack = packs.select_pack('Nunu & Willump')
        text = packs.pack_prompt(pack, 20000)
        self.assertIn('+108% AP', text)
        self.assertIn('Dragon Slayer', text)
        self.assertIn(packs.UNVERIFIED_TAG, text)
        self.assertNotIn('Solstice Sleigh: your W/E slow triggers', text)

    def test_warwick_contested_claims_are_unverified(self):
        with open(os.path.join(REAL_DIR, 'warwick.json'), encoding='utf-8') as handle:
            pack = json.load(handle)
        suppression = [entry for entry in pack['mechanics']
                       if entry['name'] == 'R vs suppression removal'][0]
        self.assertIs(suppression['verified'], False)
        refund = [entry for entry in pack['mechanics']
                  if entry['name'] == 'Q tap cooldown refund condition'][0]
        self.assertIs(refund['verified'], False)
        cleanse = [entry for entry in pack['counters'] if entry['name'] == 'Cleanse / QSS'][0]
        self.assertIs(cleanse['verified'], False)
        self.assertIn('does not interrupt the channel', suppression['value'])

    def test_real_prompt_represents_all_categories(self):
        pack = packs.select_pack('Nunu & Willump')
        text, manifest = packs.pack_prompt(pack, packs.DEFAULT_MAX_CHARS, with_manifest=True)
        for section in ALL_SECTIONS:
            self.assertIn(section, text)
            summary = [item for item in manifest['sections'] if item['id'] != 'header'
                       and item['header'].startswith(section)][0]
            self.assertGreater(summary['bytes'], 0)
        self.assertFalse([section for section in manifest['sections']
                          if section['id'] != 'header' and section['dropped']])


if __name__ == '__main__':
    unittest.main()
