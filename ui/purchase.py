"""Deterministic purchase assistant for RiftSense.

Pure, dependency-free item logic: item-ID recipe graph, owned-component
subtraction, quantity handling, inventory-slot checks, map restrictions,
affordable alternatives and gold-to-next-purchase.

The model explains decisions; it never does the arithmetic. Every cost in
here comes straight from the Data Dragon catalog (``items.json``) keyed by
item ID, so duplicate display names and localized labels cannot corrupt it.
"""

import json
import os

SLOT_COUNT = 6

_UNKNOWN_NAME = 'unknown item'


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _gold_total(raw):
    if not isinstance(raw, dict):
        return None
    gold = raw.get('gold')
    if not isinstance(gold, dict):
        return None
    return _int(gold.get('total'))


def load_catalog(source):
    """Build a recipe graph keyed by item ID.

    ``source`` is either a path to a Data Dragon ``items.json`` file or an
    already-parsed catalog object. Every item is kept and queryable by ID;
    nodes flagged ``recommendable`` are the Summoner's Rift (``maps["11"]``)
    purchasable items that may be suggested. Each node exposes::

        {id, name, cost, from, into, maps, purchasable, tags, sr, recommendable}

    ``from`` preserves catalog order and duplicates (Data Dragon lists some
    components twice, e.g. Seeker's Armguard needs two Amplifying Tomes).
    """
    if isinstance(source, (str, bytes, os.PathLike)):
        with open(source, 'r', encoding='utf-8-sig') as handle:
            obj = json.load(handle)
    elif isinstance(source, dict):
        obj = source
    else:
        raise TypeError('catalog source must be a path or a parsed dict')
    data = obj.get('data') if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        return {}
    catalog = {}
    for key, raw in data.items():
        iid = _int(key)
        if iid is None or iid <= 0 or not isinstance(raw, dict):
            continue
        maps = raw.get('maps') if isinstance(raw.get('maps'), dict) else {}
        gold = raw.get('gold') if isinstance(raw.get('gold'), dict) else {}
        sr = maps.get('11') is True
        purchasable = gold.get('purchasable') is True
        froms = []
        for entry in raw.get('from') or []:
            fid = _int(entry)
            if fid is not None:
                froms.append(fid)
        intos = []
        for entry in raw.get('into') or []:
            tid = _int(entry)
            if tid is not None:
                intos.append(tid)
        tags = [tag for tag in (raw.get('tags') or []) if isinstance(tag, str)]
        catalog[iid] = {
            'id': iid,
            'name': str(raw.get('name') or ''),
            'cost': _gold_total(raw),
            'from': froms,
            'into': sorted(set(intos)),
            'maps': {str(k): bool(v) for k, v in maps.items()},
            'purchasable': purchasable,
            'tags': tags,
            'sr': sr,
            'recommendable': bool(sr and purchasable),
        }
    return catalog


def find_item_id(catalog, name):
    """Resolve a display name to an ID deterministically (lowest ID wins).

    Mirrors the wave-1 server lookup for plan parsing, but reads names from
    the catalog so it works standalone. Prefers Summoner's Rift purchasable
    variants when duplicates exist (see audit A4/G7).
    """
    if not isinstance(catalog, dict) or not isinstance(name, str):
        return None
    key = name.strip().lower()
    if not key:
        return None
    matches = []
    fallback = []
    for node in catalog.values():
        if node['name'].lower() != key:
            continue
        if node['recommendable']:
            matches.append(node['id'])
        else:
            fallback.append(node['id'])
    pool = matches or fallback
    return min(pool) if pool else None


def chain_of(catalog, item_id):
    """Wave-1 successor chain: the item plus every later item reachable
    through ``into``, following only recommendable nodes.

    Owning any item in this set means the earlier plan item is complete.
    Sorted, deduplicated, never raises.
    """
    iid = _int(item_id)
    if not isinstance(catalog, dict) or iid is None or iid not in catalog:
        return []
    start = catalog[iid]
    seen = set()
    stack = []
    if start['sr']:
        seen.add(iid)
        for successor in start['into']:
            node = catalog.get(successor)
            if node is not None and node['recommendable']:
                stack.append(successor)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for successor in catalog[current]['into']:
            node = catalog.get(successor)
            if node is not None and node['recommendable']:
                stack.append(successor)
    return sorted(seen)


def inventory_summary(item_entries):
    """Collapse inventory entries to ``{item_id: stack_count}``.

    Accepts live-client style dicts (``id``/``itemID``/``itemId`` plus
    ``count``/``q``/``qty``/``quantity``). Duplicate IDs are summed; a
    missing or non-positive quantity counts as one. Anything malformed is
    ignored rather than guessed at.
    """
    counts = {}
    if not isinstance(item_entries, (list, tuple)):
        return counts
    for raw in item_entries:
        if not isinstance(raw, dict):
            continue
        iid = None
        for key in ('id', 'itemID', 'itemId'):
            iid = _int(raw.get(key))
            if iid is not None:
                break
        if iid is None or iid <= 0:
            continue
        quantity = None
        for key in ('count', 'q', 'qty', 'quantity'):
            quantity = _int(raw.get(key))
            if quantity is not None:
                break
        if quantity is None or quantity < 1:
            quantity = 1
        counts[iid] = counts.get(iid, 0) + quantity
    return counts


def _owned_counts(owned_ids):
    counts = {}
    if isinstance(owned_ids, dict):
        for key, value in owned_ids.items():
            iid = _int(key)
            if iid is None or iid <= 0:
                continue
            quantity = _int(value)
            if quantity is None or quantity < 1:
                quantity = 1
            counts[iid] = counts.get(iid, 0) + quantity
        return counts
    if owned_ids is None:
        return counts
    try:
        iterable = list(owned_ids)
    except TypeError:
        return counts
    for key in iterable:
        iid = _int(key)
        if iid is not None and iid > 0:
            counts[iid] = counts.get(iid, 0) + 1
    return counts


def _plan_entries(plan_ids, catalog):
    entries = []
    if not isinstance(plan_ids, (list, tuple)):
        return entries
    for raw in plan_ids:
        iid = None
        name = None
        if isinstance(raw, dict):
            iid = _int(raw.get('id'))
            if isinstance(raw.get('name'), str):
                name = raw['name'].strip() or None
        elif isinstance(raw, str):
            name = raw.strip() or None
        else:
            iid = _int(raw)
        if iid is None and name:
            iid = find_item_id(catalog, name)
        if iid is not None and name is None:
            node = catalog.get(iid)
            if node is not None:
                name = node['name'] or None
        entries.append({'id': iid, 'name': name})
    return entries


def _is_complete(catalog, item_id, owned):
    for candidate in chain_of(catalog, item_id):
        if owned.get(candidate, 0) > 0:
            return True
    return False


def _recipe_base(catalog, node, report):
    """Combine cost for ``node``: catalog total minus its component totals.

    Derived only from catalog numbers (never invented). Returns ``None``
    when any input cost is missing, which makes the result inexact.
    """
    if node['cost'] is None:
        report['unknown'].append(node['id'])
        return None
    parts = 0
    for component in node['from']:
        child = catalog.get(component)
        if child is None or child['cost'] is None:
            report['unknown'].append(component)
            return None
        parts += child['cost']
    return node['cost'] - parts


def _analyze(catalog, item_id, owned):
    """Remaining gold for ``item_id`` net of owned parts.

    Walks the recipe tree depth-first in catalog order, consuming owned
    copies (quantities matter) as they are found. The result is exact for
    Data Dragon data because ``total = combine cost + component totals``,
    including components listed twice (audit G8/A4). Returns
    ``(remaining, exact, report)`` where ``report`` records which owned
    parts were consumed, which nodes had to be bought, and any nodes whose
    cost is unknown or that cannot be purchased.
    """
    available = dict(owned)
    report = {'consumed': {}, 'required': {}, 'unknown': [], 'notPurchasable': []}

    def walk(current):
        node = catalog.get(current)
        if node is None:
            report['unknown'].append(current)
            return 0
        if available.get(current, 0) > 0:
            available[current] -= 1
            report['consumed'][current] = report['consumed'].get(current, 0) + 1
            return 0
        report['required'][current] = report['required'].get(current, 0) + 1
        if not node['from']:
            if node['cost'] is None:
                report['unknown'].append(current)
                return 0
            if not node['purchasable']:
                report['notPurchasable'].append(current)
            return node['cost']
        base = _recipe_base(catalog, node, report)
        total = 0 if base is None else base
        for component in node['from']:
            total += walk(component)
        return total

    remaining = walk(item_id)
    return remaining, not report['unknown'], report


def _component_offers(catalog, target_id, owned, gold):
    """Every recommendable, not-yet-owned component in the recipe tree of
    ``target_id`` with the exact remaining gold to acquire it."""
    offers = []
    visited = set()
    stack = list(catalog[target_id]['from'])
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        node = catalog.get(current)
        if node is None:
            continue
        stack.extend(node['from'])
        if not node['recommendable']:
            continue
        if _is_complete(catalog, current, owned):
            continue
        remaining, exact, _report = _analyze(catalog, current, owned)
        if remaining is None:
            continue
        offers.append({
            'id': current,
            'name': node['name'],
            'cost': node['cost'],
            'goldToComplete': remaining,
            'costExact': exact,
            'affordable': bool(exact and gold is not None and gold >= remaining),
        })
    offers.sort(key=lambda offer: (offer['goldToComplete'], offer['id']))
    return offers


def _slots(owned):
    used = len(owned)
    free = max(0, SLOT_COUNT - used)
    return {'total': SLOT_COUNT, 'used': used, 'free': free, 'ok': free > 0}


def next_purchase(owned_ids, plan_ids, available_gold, catalog=None):
    """Deterministic next-buy advice for the first incomplete plan item.

    ``owned_ids`` is an ``{id: qty}`` mapping (as returned by
    ``inventory_summary``) or any iterable of IDs. ``plan_ids`` is the plan
    in purchase order as IDs, names, or ``{'id', 'name'}`` dicts (the shape
    returned by ``/api/plan``). ``catalog`` is required for any real math;
    without it the result degrades to ``status='no_catalog'``.

    Returns ``{status, next, alternatives, slots, reasons}``. Every number
    comes from the catalog; if a component cost is missing the result
    carries ``costExact=False`` instead of guessing.
    """
    if catalog is None:
        catalog = {}
    if not isinstance(catalog, dict):
        catalog = {}
    owned = _owned_counts(owned_ids)
    gold = _int(available_gold)
    result = {
        'status': 'ok',
        'next': None,
        'alternatives': [],
        'slots': _slots(owned),
        'reasons': [],
    }
    if not catalog:
        result['status'] = 'no_catalog'
        result['reasons'].append('item catalog unavailable')
        return result

    plan = _plan_entries(plan_ids, catalog)
    if not plan:
        result['status'] = 'no_plan'
        result['reasons'].append('no build plan')
        return result

    unknown_plan = []
    unsupported_plan = []
    saw_known = False
    target_index = None
    for index, entry in enumerate(plan):
        iid = entry['id']
        if iid is None:
            if entry['name']:
                unknown_plan.append(entry['name'])
            continue
        if iid not in catalog:
            unknown_plan.append(entry['name'] or str(iid))
            continue
        if not catalog[iid]['recommendable']:
            unsupported_plan.append(entry['name'] or str(iid))
            continue
        saw_known = True
        if not _is_complete(catalog, iid, owned):
            target_index = index
            break

    if target_index is None:
        result['status'] = 'plan_complete' if saw_known else 'unknown_item'
        if saw_known:
            result['reasons'].append('plan complete')
        for name in unknown_plan[:3]:
            result['reasons'].append('unknown plan item: %s' % name)
        for name in unsupported_plan[:3]:
            result['reasons'].append('unsupported for Summoner\'s Rift: %s' % name)
        if not result['reasons']:
            result['reasons'].append('no known plan items')
        return result

    target = plan[target_index]
    node = catalog[target['id']]
    name = node['name'] or target['name'] or str(target['id'])
    remaining, exact, report = _analyze(catalog, target['id'], owned)
    affordable_now = bool(
        exact and remaining is not None and gold is not None and gold >= remaining)
    gold_short = None
    if remaining is not None and gold is not None:
        gold_short = max(0, remaining - gold)

    components = _component_offers(catalog, target['id'], owned, gold)
    affordable_components = [offer for offer in components if offer['affordable']]
    cheapest = []
    if affordable_components:
        lowest = min(offer['goldToComplete'] for offer in affordable_components)
        cheapest = [offer for offer in affordable_components
                    if offer['goldToComplete'] == lowest][:3]

    owned_components = []
    for iid in sorted(report['consumed']):
        consumed_node = catalog.get(iid)
        if consumed_node is None:
            continue
        owned_components.append({
            'id': iid,
            'name': consumed_node['name'],
            'cost': consumed_node['cost'],
            'qty': report['consumed'][iid],
        })

    result['next'] = {
        'id': target['id'],
        'name': name,
        'cost': node['cost'],
        'goldToComplete': remaining,
        'costExact': exact,
        'affordableNow': affordable_now,
        'goldShort': gold_short,
        'components': components,
        'cheapestAffordable': [{
            'id': offer['id'],
            'name': offer['name'],
            'cost': offer['cost'],
            'goldToComplete': offer['goldToComplete'],
        } for offer in cheapest],
        'ownedComponents': owned_components,
    }

    alternatives = []
    for entry in plan[target_index + 1:]:
        iid = entry['id']
        if iid is None or iid not in catalog:
            continue
        if not catalog[iid]['recommendable']:
            continue
        if _is_complete(catalog, iid, owned):
            continue
        alt_remaining, alt_exact, _alt_report = _analyze(catalog, iid, owned)
        if alt_remaining is None or not alt_exact:
            continue
        if gold is None or alt_remaining > gold:
            continue
        alt_node = catalog[iid]
        alternatives.append({
            'id': iid,
            'name': alt_node['name'] or entry['name'] or str(iid),
            'cost': alt_node['cost'],
            'goldToComplete': alt_remaining,
            'affordableNow': True,
        })
        if len(alternatives) >= 2:
            break

    reasons = []
    for unknown_name in unknown_plan[:3]:
        reasons.append('unknown plan item: %s' % unknown_name)
    for unsupported_name in unsupported_plan[:3]:
        reasons.append('unsupported for Summoner\'s Rift: %s' % unsupported_name)
    if remaining is None or not exact:
        reasons.append('component costs unavailable for %s' % name)
    else:
        if remaining > 0:
            reasons.append('%dg to complete' % remaining)
        else:
            reasons.append('ready to finish %s' % name)
        if affordable_now:
            reasons.append('%s affordable now' % name)
        elif gold is None:
            reasons.append('current gold unavailable')
        elif gold_short is not None:
            reasons.append('%dg short of %s' % (gold_short, name))
    for offer in cheapest:
        reasons.append('component %s affordable now (%dg)' % (
            offer['name'], offer['goldToComplete']))
    if not result['slots']['ok']:
        reasons.append('no free inventory slot')
    for alternative in alternatives:
        reasons.append('alternative: %s affordable now (%dg)' % (
            alternative['name'], alternative['goldToComplete']))

    result['alternatives'] = alternatives
    result['reasons'] = reasons
    return result
