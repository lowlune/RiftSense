"""Tests for ui/purchase.py.

Uses the real Data Dragon catalog (``items.json`` next to the repo root)
when it is present; otherwise exercises small deterministic fixtures so the
suite still runs without downloaded assets.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import purchase  # noqa: E402

REAL_ITEMS = os.path.join(ROOT, 'items.json')


def ddragon_item(iid, name, total, from_=None, purchasable=True, maps11=True,
                 base=None, tags=None, into=None):
    gold = {'total': total, 'purchasable': purchasable}
    gold['base'] = total if base is None else base
    return {
        'id': iid,
        'name': name,
        'gold': gold,
        'from': from_,
        'into': into,
        'maps': {'11': maps11, '12': True},
        'tags': tags or [],
    }


def make_catalog(items):
    return purchase.load_catalog({'data': {str(it['id']): it for it in items}})


def fixture_catalog():
    return make_catalog([
        ddragon_item(100, 'Dagger', 300, into=[200]),
        ddragon_item(101, 'Long Sword', 350, into=[200]),
        ddragon_item(102, 'Ruby Crystal', 400, into=[200, 201]),
        ddragon_item(200, 'Big Sword', 1100, from_=[101, 102]),
        ddragon_item(201, 'Tank Item', 900, from_=[102, 103]),
        ddragon_item(103, 'Cloth Armor', 300),
        ddragon_item(300, 'Deep Item', 2200, from_=[200, 201]),
        ddragon_item(400, 'Arena Only', 100, purchasable=True, maps11=False),
    ])


def load_real_catalog():
    if not os.path.exists(REAL_ITEMS):
        return None
    try:
        return purchase.load_catalog(REAL_ITEMS)
    except Exception:
        return None


REAL = load_real_catalog()


class LoadCatalogTests(unittest.TestCase):
    def test_fixture_structure(self):
        catalog = fixture_catalog()
        self.assertEqual(set(catalog), {100, 101, 102, 103, 200, 201, 300, 400})
        node = catalog[200]
        self.assertEqual(node['name'], 'Big Sword')
        self.assertEqual(node['cost'], 1100)
        self.assertEqual(node['from'], [101, 102])
        self.assertEqual(node['into'], [])
        self.assertTrue(node['purchasable'])
        self.assertTrue(node['sr'])
        self.assertTrue(node['recommendable'])
        self.assertEqual(node['maps']['11'], True)

    def test_accepts_parsed_dict_and_path_equivalently(self):
        parsed = {'data': {'1001': ddragon_item(1001, 'Boots', 300)}}
        from_dict = purchase.load_catalog(parsed)
        self.assertIn(1001, from_dict)
        self.assertEqual(from_dict[1001]['recommendable'], True)

    def test_all_items_queryable_but_only_sr_purchasable_recommended(self):
        catalog = fixture_catalog()
        self.assertIn(400, catalog)
        self.assertFalse(catalog[400]['recommendable'])
        self.assertFalse(catalog[400]['sr'])
        # Non-SR and non-purchasable items must never be offered.
        for node in catalog.values():
            if node['recommendable']:
                self.assertTrue(node['sr'] and node['purchasable'])

    def test_bad_source_types(self):
        with self.assertRaises(TypeError):
            purchase.load_catalog(12345)
        self.assertEqual(purchase.load_catalog({'nope': True}), {})

    def test_find_item_id_prefers_recommendable_then_lowest(self):
        catalog = make_catalog([
            ddragon_item(900, 'Same Name', 100, purchasable=False, maps11=False),
            ddragon_item(500, 'Same Name', 200, purchasable=True, maps11=True),
            ddragon_item(300, 'Same Name', 300, purchasable=True, maps11=True),
        ])
        self.assertEqual(purchase.find_item_id(catalog, 'same name'), 300)
        self.assertIsNone(purchase.find_item_id(catalog, 'no such thing'))
        self.assertIsNone(purchase.find_item_id(catalog, None))


class InventorySummaryTests(unittest.TestCase):
    def test_duplicates_counts_and_aliases(self):
        entries = [
            {'id': 1001, 'count': 1},
            {'id': 1001, 'count': 2},
            {'itemID': 1038, 'q': 3},
            {'itemId': 2003, 'qty': 2},
            {'id': 2055, 'quantity': 4},
            {'id': 3070},
            {'id': '3089', 'count': '2'},
            'garbage',
            None,
            {'id': 0},
            {'id': -5},
            {'id': True},
        ]
        self.assertEqual(purchase.inventory_summary(entries), {
            1001: 3, 1038: 3, 2003: 2, 2055: 4, 3070: 1, 3089: 2,
        })

    def test_bad_container(self):
        self.assertEqual(purchase.inventory_summary(None), {})
        self.assertEqual(purchase.inventory_summary({'id': 1}), {})


class NextPurchaseFixtureTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fixture_catalog()

    def test_empty_plan(self):
        result = purchase.next_purchase({}, [], 5000, self.catalog)
        self.assertEqual(result['status'], 'no_plan')
        self.assertIsNone(result['next'])
        self.assertIn('no build plan', result['reasons'])

    def test_no_catalog_degrades(self):
        result = purchase.next_purchase({}, [200], 5000, None)
        self.assertEqual(result['status'], 'no_catalog')

    def test_unknown_item_names(self):
        result = purchase.next_purchase({}, ['Mystery Widget'], 5000, self.catalog)
        self.assertEqual(result['status'], 'unknown_item')
        self.assertIsNone(result['next'])
        self.assertIn('unknown plan item: Mystery Widget', result['reasons'])

        mixed = purchase.next_purchase({}, ['Mystery Widget', 200], 5000, self.catalog)
        self.assertEqual(mixed['status'], 'ok')
        self.assertEqual(mixed['next']['id'], 200)
        self.assertIn('unknown plan item: Mystery Widget', mixed['reasons'])

    def test_incomplete_plan_affordable_component(self):
        result = purchase.next_purchase({}, [200], 400, self.catalog)
        self.assertEqual(result['status'], 'ok')
        nxt = result['next']
        self.assertEqual(nxt['id'], 200)
        self.assertEqual(nxt['cost'], 1100)
        self.assertEqual(nxt['goldToComplete'], 1100)
        self.assertEqual(nxt['affordableNow'], False)
        self.assertEqual(nxt['goldShort'], 700)
        self.assertIn('1100g to complete', result['reasons'])
        self.assertIn('700g short of Big Sword', result['reasons'])
        offers = {offer['id']: offer for offer in nxt['components']}
        self.assertTrue(offers[101]['affordable'])
        self.assertTrue(offers[102]['affordable'])
        self.assertEqual([c['id'] for c in nxt['cheapestAffordable']], [101])
        self.assertIn('component Long Sword affordable now (350g)', result['reasons'])

    def test_full_item_affordable_now(self):
        result = purchase.next_purchase({}, [200], 1500, self.catalog)
        nxt = result['next']
        self.assertTrue(nxt['affordableNow'])
        self.assertEqual(nxt['goldShort'], 0)
        self.assertIn('Big Sword affordable now', result['reasons'])

    def test_owned_component_subtraction(self):
        result = purchase.next_purchase({101: 1}, [200], 800, self.catalog)
        nxt = result['next']
        self.assertEqual(nxt['cost'], 1100)
        self.assertEqual(nxt['goldToComplete'], 750)
        self.assertTrue(nxt['affordableNow'])
        self.assertEqual(nxt['goldShort'], 0)
        self.assertEqual([c['id'] for c in nxt['ownedComponents']], [101])
        self.assertIn('750g to complete', result['reasons'])

    def test_owned_quantities_consume_each_copy(self):
        catalog = make_catalog([
            ddragon_item(10, 'Tome', 400),
            ddragon_item(11, 'Cloak', 300),
            ddragon_item(20, 'Two Tome Item', 1100, from_=[10, 11, 10],
                         base=0),
        ])
        result = purchase.next_purchase({10: 1}, [20], 0, catalog)
        # One owned tome covers one of the two required copies.
        self.assertEqual(result['next']['goldToComplete'], 700)
        result2 = purchase.next_purchase({10: 2, 11: 1}, [20], 0, catalog)
        self.assertEqual(result2['next']['goldToComplete'], 0)
        self.assertIn('ready to finish Two Tome Item', result2['reasons'])

    def test_plan_complete_via_successor_chain(self):
        result = purchase.next_purchase({200: 1}, [101], 5000, self.catalog)
        self.assertEqual(result['status'], 'plan_complete')
        self.assertIsNone(result['next'])
        self.assertIn('plan complete', result['reasons'])

    def test_recipe_tree_traversal_reaches_deep_components(self):
        result = purchase.next_purchase({}, [300], 10000, self.catalog)
        offer_ids = {offer['id'] for offer in result['next']['components']}
        self.assertEqual(offer_ids, {101, 102, 103, 200, 201})
        for offer in result['next']['components']:
            self.assertEqual(offer['cost'], self.catalog[offer['id']]['cost'])
        cheapest = result['next']['cheapestAffordable']
        self.assertEqual([c['id'] for c in cheapest], [103])

    def test_unknown_costs_are_not_invented(self):
        catalog = make_catalog([
            ddragon_item(1, 'Mystery Part', None),
            ddragon_item(2, 'Target', 500, from_=[1]),
        ])
        self.assertIsNone(catalog[1]['cost'])
        result = purchase.next_purchase({}, [2], 9999, catalog)
        nxt = result['next']
        self.assertFalse(nxt['costExact'])
        self.assertFalse(nxt['affordableNow'])
        self.assertIn('component costs unavailable for Target', result['reasons'])

    def test_alternatives_capped_at_two_and_only_affordable(self):
        catalog = make_catalog([
            ddragon_item(1, 'Target', 5000),
            ddragon_item(2, 'Alt Cheap', 400),
            ddragon_item(3, 'Alt Cheaper', 300),
            ddragon_item(4, 'Alt Cheapest', 200),
            ddragon_item(5, 'Alt Too Dear', 900),
        ])
        result = purchase.next_purchase({}, [1, 2, 3, 4, 5], 500, catalog)
        self.assertEqual([alt['id'] for alt in result['alternatives']], [2, 3])
        self.assertEqual(result['next']['id'], 1)
        self.assertIn('alternative: Alt Cheap affordable now (400g)', result['reasons'])

    def test_alternative_skips_non_sr_variant(self):
        catalog = make_catalog([
            ddragon_item(1, 'Target', 5000),
            ddragon_item(2, 'Arena Cheap', 100, maps11=False),
            ddragon_item(3, 'Real Alt', 200),
        ])
        result = purchase.next_purchase({}, [1, 2, 3], 500, catalog)
        self.assertEqual([alt['id'] for alt in result['alternatives']], [3])

    def test_slots(self):
        result = purchase.next_purchase({}, [200], 2000, self.catalog)
        self.assertEqual(result['slots'], {'total': 6, 'used': 0, 'free': 6, 'ok': True})

        full = purchase.next_purchase({1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1},
                                      [200], 2000, self.catalog)
        self.assertEqual(full['slots']['free'], 0)
        self.assertFalse(full['slots']['ok'])
        self.assertIn('no free inventory slot', full['reasons'])

    def test_inventory_summary_output_feeds_next_purchase(self):
        owned = purchase.inventory_summary([{'id': 101, 'count': 1}])
        result = purchase.next_purchase(owned, [200], 800, self.catalog)
        self.assertEqual(result['next']['goldToComplete'], 750)
        self.assertTrue(result['next']['affordableNow'])

    def test_plan_entry_names_and_dicts(self):
        result = purchase.next_purchase({}, ['Long Sword'], 400, self.catalog)
        self.assertEqual(result['next']['id'], 101)
        by_dict = purchase.next_purchase(
            {}, [{'id': 200, 'name': 'Big Sword'}], 0, self.catalog)
        self.assertEqual(by_dict['next']['id'], 200)

    def test_owned_ids_accept_list_and_set(self):
        from_list = purchase.next_purchase([101], [200], 800, self.catalog)
        from_set = purchase.next_purchase({101}, [200], 800, self.catalog)
        self.assertEqual(from_list['next']['goldToComplete'],
                         from_set['next']['goldToComplete'])


class ChainTests(unittest.TestCase):
    def test_chain_follows_into_not_from(self):
        catalog = fixture_catalog()
        chain = purchase.chain_of(catalog, 101)
        self.assertEqual(chain, [101, 200])
        self.assertNotIn(102, chain)
        self.assertNotIn(103, chain)

    def test_chain_of_unknown_and_non_sr(self):
        catalog = fixture_catalog()
        self.assertEqual(purchase.chain_of(catalog, 999999), [])
        self.assertEqual(purchase.chain_of(catalog, 400), [])


@unittest.skipUnless(REAL, 'items.json not present in repo root')
class RealCatalogTests(unittest.TestCase):
    def test_recommendable_filter_matches_audit_g7(self):
        by_id = REAL
        liandrys = by_id[6653]
        self.assertEqual(liandrys['name'], "Liandry's Torment")
        self.assertTrue(liandrys['recommendable'])
        self.assertEqual(liandrys['cost'], 3000)
        self.assertEqual(liandrys['from'], [3147, 2508])
        arena_variant = by_id.get(773136)
        self.assertIsNotNone(arena_variant)
        self.assertFalse(arena_variant['recommendable'])
        self.assertFalse(arena_variant['sr'])

    def test_real_owned_component_math(self):
        catalog = REAL
        result = purchase.next_purchase({3147: 1}, [6653], 800, catalog)
        nxt = result['next']
        self.assertEqual(nxt['cost'], catalog[6653]['cost'])
        self.assertEqual(nxt['goldToComplete'],
                         catalog[6653]['cost'] - catalog[3147]['cost'])
        combine = catalog[6653]['cost'] - sum(
            catalog[f]['cost'] for f in catalog[6653]['from'])
        self.assertEqual(nxt['goldToComplete'],
                         combine + catalog[2508]['cost'])
        self.assertEqual([c['id'] for c in nxt['ownedComponents']], [3147])
        self.assertIn('%dg to complete' % nxt['goldToComplete'], result['reasons'])

    def test_real_duplicate_from_components(self):
        catalog = REAL
        seekers = catalog.get(2420)
        if seekers is None or seekers['from'].count(1052) < 2:
            self.skipTest('Seeker\'s Armguard duplicate components not in catalog')
        result = purchase.next_purchase({1052: 1}, [2420], 0, catalog)
        self.assertEqual(result['next']['goldToComplete'],
                         seekers['cost'] - catalog[1052]['cost'])

    def test_real_recipe_traversal_costs_come_from_catalog(self):
        catalog = REAL
        first = purchase.next_purchase({}, [6653], 350, catalog)
        self.assertEqual(first['next']['cost'], catalog[6653]['cost'])
        for offer in first['next']['components']:
            self.assertEqual(offer['cost'], catalog[offer['id']]['cost'])
            self.assertTrue(catalog[offer['id']]['recommendable'])

    def test_server_chain_delegation_matches_wave1(self):
        try:
            import server  # noqa: F401
        except Exception as exc:  # pragma: no cover
            self.skipTest('server module unavailable: %s' % exc)
        if not getattr(server, 'PURCHASE_CATALOG', None):
            self.skipTest('server has no purchase catalog')
        rows = [1001, 3147, 6653, 2420, 1038]
        for iid in rows:
            expected = server.chain_of(iid)
            got = purchase.chain_of(REAL, iid)
            self.assertEqual(got, expected, 'chain mismatch for %d' % iid)


class FakeServerPayloadTests(unittest.TestCase):
    def test_json_round_trip_safe(self):
        catalog = fixture_catalog()
        result = purchase.next_purchase({101: 1}, [200], 800, catalog)
        blob = json.dumps(result)
        self.assertIn('goldToComplete', blob)


if __name__ == '__main__':
    unittest.main(verbosity=2)
