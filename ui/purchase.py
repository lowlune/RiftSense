"""Deterministic purchase assistant for RiftSense.

Pure, dependency-free item logic: item-ID recipe graph, quantity-aware owned
component allocation, inventory-slot checks with stack/trinket rules, map
restrictions, a deterministic purchase frontier (feasible component
combinations), affordable alternatives and gold-to-next-purchase.

The model explains decisions; it never does the arithmetic. Every cost in
here comes straight from the Data Dragon catalog (``items.json``) keyed by
item ID, so duplicate display names and localized labels cannot corrupt it.
No floating-point cost is ever invented: catalog costs are whole gold, so
fractional *available gold* is floored for comparison and the result says so
with ``goldRounded``.
"""

import json
import math
import os

SLOT_COUNT = 6
CONSUMABLE_STACK = 5
FRONTIER_MAX_COMBOS = 6
FRONTIER_MAX_ITEMS = 3
FRONTIER_MAX_CANDIDATES = 8

_UNKNOWN_NAME = 'unknown item'


def _int(value):
    """Strict integer parsing for catalog data (IDs, costs, quantities)."""
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


def _gold_value(value):
    """Finite, non-negative gold as a float; ``None`` for anything else.

    Unlike item costs, current gold from the live client can legitimately be
    fractional, so this accepts ``800.5`` and ``"800.5"``. Rejects booleans,
    NaN, infinities, negatives, and empty/non-numeric strings.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


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


def inventory_entries(item_entries):
    """Preserve live-client slot entries as ``[{'id': id, 'qty': n}]``.

    Unlike :func:`inventory_summary` this keeps one record per client entry
    (one inventory slot each, client order preserved) so repeated components
    are not collapsed before slot accounting. Accepts an
    ``inventory_state``/``{'entries': [...]}`` dict as well. Malformed
    entries are ignored rather than guessed at.
    """
    if isinstance(item_entries, dict) and isinstance(item_entries.get('entries'), (list, tuple)):
        item_entries = item_entries['entries']
    entries = []
    if not isinstance(item_entries, (list, tuple)):
        return entries
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
        entries.append({'id': iid, 'qty': quantity})
    return entries


def inventory_summary(item_entries):
    """Collapse inventory entries to ``{item_id: stack_count}``.

    Accepts live-client style dicts (``id``/``itemID``/``itemId`` plus
    ``count``/``q``/``qty``/``quantity``) or an ``inventory_state`` dict.
    Duplicate IDs are summed; a missing or non-positive quantity counts as
    one. Anything malformed is ignored rather than guessed at.
    """
    counts = {}
    for entry in inventory_entries(item_entries):
        counts[entry['id']] = counts.get(entry['id'], 0) + entry['qty']
    return counts


def _owned_counts(owned_ids):
    counts = {}
    if isinstance(owned_ids, dict) and not isinstance(owned_ids.get('entries'), (list, tuple)):
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


def _owned_state(owned_ids):
    """Return ``(counts, entries)`` for any supported inventory input.

    ``entries`` preserves client slot boundaries when the caller supplied raw
    entries; otherwise one synthetic entry per distinct item ID is produced
    from the ``{id: qty}`` counts.
    """
    raw_entries = None
    if isinstance(owned_ids, dict) and isinstance(owned_ids.get('entries'), (list, tuple)):
        raw_entries = owned_ids
    elif isinstance(owned_ids, (list, tuple)) and any(
            isinstance(item, dict) for item in owned_ids):
        raw_entries = owned_ids
    if raw_entries is not None:
        entries = inventory_entries(raw_entries)
        counts = {}
        for entry in entries:
            counts[entry['id']] = counts.get(entry['id'], 0) + entry['qty']
        return counts, entries
    counts = _owned_counts(owned_ids)
    entries = [{'id': iid, 'qty': counts[iid]} for iid in sorted(counts)]
    return counts, entries


def _tags(catalog, item_id):
    if not isinstance(catalog, dict):
        return ()
    node = catalog.get(item_id)
    if not isinstance(node, dict):
        return ()
    return node.get('tags') or ()


def _is_trinket(catalog, item_id):
    return 'Trinket' in _tags(catalog, item_id)


def _is_stackable(catalog, item_id):
    return 'Consumable' in _tags(catalog, item_id)


def _entry_slots(catalog, item_id, qty):
    """Regular (non-trinket) slots occupied by ``qty`` copies in one entry.

    Trinkets use the separate trinket slot (0 regular slots). Consumables
    stack up to ``CONSUMABLE_STACK`` per slot. Everything else is
    non-stackable: each copy consumes a slot.
    """
    if qty <= 0:
        return 0
    if _is_trinket(catalog, item_id):
        return 0
    if _is_stackable(catalog, item_id):
        return (qty + CONSUMABLE_STACK - 1) // CONSUMABLE_STACK
    return qty


def _added_slots(catalog, counts, adds):
    """Slots needed to add ``adds`` (``{id: copies}``) to current ``counts``."""
    extra = 0
    held = dict(counts)
    for iid in sorted(adds):
        qty = adds[iid]
        if qty <= 0 or _is_trinket(catalog, iid):
            continue
        current = held.get(iid, 0)
        if _is_stackable(catalog, iid):
            free_room = current % CONSUMABLE_STACK
            overflow = max(0, qty - free_room)
            if overflow:
                extra += (overflow + CONSUMABLE_STACK - 1) // CONSUMABLE_STACK
        else:
            extra += qty
        held[iid] = current + qty
    return extra


def _freed_slots(catalog, counts, removed):
    """Slots freed by removing ``removed`` (``{id: copies}``) from inventory."""
    freed = 0
    held = dict(counts)
    for iid in sorted(removed):
        qty = removed[iid]
        if qty <= 0 or _is_trinket(catalog, iid):
            continue
        current = held.get(iid, 0)
        if current <= 0:
            continue
        if _is_stackable(catalog, iid):
            before = (current + CONSUMABLE_STACK - 1) // CONSUMABLE_STACK
            remaining = max(0, current - qty)
            after = (remaining + CONSUMABLE_STACK - 1) // CONSUMABLE_STACK
            freed += max(0, before - after)
            held[iid] = remaining
        else:
            used = min(qty, current)
            freed += used
            held[iid] = current - used
    return freed


def inventory_slots(item_entries, catalog=None):
    """Compute actual slot usage from preserved client entries.

    ``item_entries`` may be raw live-client entries, an ``inventory_state``
    dict, a ``{id: qty}`` mapping, or any iterable of IDs. Stack rules and
    trinket basics are applied. Returns
    ``{total, used, free, ok, trinkets, entries}`` where ``entries`` echoes
    the normalized per-slot entries.
    """
    counts, entries = _owned_state(item_entries)
    used = 0
    trinkets = 0
    for entry in entries:
        if _is_trinket(catalog, entry['id']):
            trinkets += 1
        else:
            used += _entry_slots(catalog, entry['id'], entry['qty'])
    free = max(0, SLOT_COUNT - used)
    return {
        'total': SLOT_COUNT,
        'used': used,
        'free': free,
        'ok': free > 0,
        'trinkets': trinkets,
        'entries': entries,
    }


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


def _consume_milestone(catalog, item_id, pool):
    """Spend one owned copy in ``chain_of(item_id)`` from ``pool``.

    Prefers the plan item itself, then successors in ascending ID order so
    allocation is deterministic. Returns the consumed ID or ``None``.
    """
    chain = chain_of(catalog, item_id)
    ordered = [item_id] + [candidate for candidate in chain if candidate != item_id]
    for candidate in ordered:
        if pool.get(candidate, 0) > 0:
            pool[candidate] -= 1
            if pool[candidate] <= 0:
                del pool[candidate]
            return candidate
    return None


def _milestone_owned(catalog, item_id, pool):
    for candidate in chain_of(catalog, item_id):
        if pool.get(candidate, 0) > 0:
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


def _component_offers(catalog, target_id, pool, gold, report):
    """Offer the next missing copy of every required component.

    Quantity-aware: the target's own allocation report says how many copies
    of each component still have to be bought, so a recipe needing two Tomes
    with one owned still offers a second Tome. ``goldToComplete`` is the cost
    to acquire one further copy given the owned copies already allocated
    elsewhere in the target recipe.
    """
    remaining_pool = dict(pool)
    for iid, qty in report['consumed'].items():
        remaining_pool[iid] = max(0, remaining_pool.get(iid, 0) - qty)
    offers = []
    for iid in sorted(report['required']):
        copies = report['required'][iid]
        if copies <= 0 or iid == target_id:
            continue
        node = catalog.get(iid)
        if node is None or not node['recommendable']:
            continue
        remaining, exact, _report = _analyze(catalog, iid, remaining_pool)
        if remaining is None:
            continue
        offers.append({
            'id': iid,
            'name': node['name'],
            'cost': node['cost'],
            'goldToComplete': remaining,
            'costExact': exact,
            'copiesNeeded': copies,
            'affordable': bool(exact and gold is not None and gold >= remaining),
        })
    offers.sort(key=lambda offer: (offer['goldToComplete'], offer['id']))
    return offers


def _frontier(catalog, target_id, pool, counts, gold, free_slots, offers):
    """Deterministic feasible component combinations for current gold/slots.

    Enumerates bounded combinations (up to ``FRONTIER_MAX_ITEMS`` copies from
    a capped candidate list), keeps only combinations that are both
    affordable and slot-feasible, and reports what each advances with a
    deterministic re-run of :func:`_analyze`.
    """
    candidates = [offer for offer in offers
                  if offer['costExact'] and offer['cost'] is not None
                  and offer['goldToComplete'] is not None]
    candidates = candidates[:FRONTIER_MAX_CANDIDATES]
    if not candidates or gold is None or gold <= 0:
        return []
    found = {}
    picked = []

    def record():
        if not picked:
            return
        key = tuple(sorted((offer['id'], copies) for offer, copies in picked))
        if key in found:
            return
        adds = {}
        for offer, copies in picked:
            adds[offer['id']] = adds.get(offer['id'], 0) + copies
        slot_cost = _added_slots(catalog, counts, adds)
        total = sum(offer['goldToComplete'] * copies for offer, copies in picked)
        if total > gold or slot_cost > free_slots:
            return
        combined = dict(pool)
        for iid, copies in adds.items():
            combined[iid] = combined.get(iid, 0) + copies
        after, exact, _report = _analyze(catalog, target_id, combined)
        found[key] = {
            'items': [
                {'id': offer['id'], 'name': offer['name'], 'copies': copies,
                 'cost': offer['cost'], 'goldToComplete': offer['goldToComplete']}
                for offer, copies in picked
            ],
            'totalCost': total,
            'slots': slot_cost,
            'affordable': True,
            'feasible': True,
            'targetRemainingAfter': after if exact else None,
            'completesTarget': bool(exact and after == 0),
        }

    def visit(start, copies):
        record()
        if copies >= FRONTIER_MAX_ITEMS or start >= len(candidates):
            return
        for index in range(start, len(candidates)):
            offer = candidates[index]
            room = min(max(1, offer.get('copiesNeeded') or 1),
                       FRONTIER_MAX_ITEMS - copies)
            for count in range(1, room + 1):
                picked.append((offer, count))
                visit(index + 1, copies + count)
                picked.pop()

    visit(0, 0)
    ordered = sorted(found.values(), key=lambda item: (
        item['targetRemainingAfter'] if item['targetRemainingAfter'] is not None
        else float('inf'),
        item['totalCost'],
        tuple((entry['id'], entry['copies']) for entry in item['items']),
    ))
    return ordered[:FRONTIER_MAX_COMBOS]


def _summary_text(name, remaining, exact, gold, affordable, suggested, gold_short):
    if remaining is None or not exact:
        return 'Next recall: cost unknown for %s.' % name
    if affordable:
        return 'Next recall: %s — %dg completes it now.' % (name, remaining)
    if gold is None:
        return 'Next recall: current gold unavailable. Target remaining: %dg.' % remaining
    if suggested is not None:
        copies = suggested.get('copiesNeeded') or 1
        if copies > 1:
            advance = 'Adds one of %d required %s copies.' % (copies, suggested['name'])
        else:
            advance = 'Completes one required component.'
        return 'Next recall: %s — %dg. %s Target remaining: %dg.' % (
            suggested['name'], suggested['goldToComplete'], advance, remaining)
    short = '' if gold_short is None else ' (%dg short)' % gold_short
    return 'Next recall: none affordable right now. Target remaining: %dg%s.' % (
        remaining, short)


def next_purchase(owned_ids, plan_ids, available_gold, catalog=None):
    """Deterministic next-buy advice for the first incomplete plan item.

    ``owned_ids`` is an ``{id: qty}`` mapping, raw live-client slot entries
    (preferred: this preserves repeated components and slot boundaries), an
    ``inventory_state`` dict, or any iterable of IDs. ``plan_ids`` is the plan
    in purchase order as IDs, names, or ``{'id', 'name'}`` dicts (the shape
    returned by ``/api/plan``). ``catalog`` is required for any real math;
    without it the result degrades to ``status='no_catalog'``.

    ``available_gold`` may be fractional; any finite non-negative value is
    accepted and floored only because catalog costs are whole gold. When the
    floor changed the value, ``goldRounded`` is true.

    Plan steps are ordered purchases: each step consumes one owned milestone
    copy, so ``[Long Sword, Long Sword]`` with one owned Sword still asks for
    a second one. Affordability and post-purchase inventory feasibility are
    computed separately. Returns ``{status, next, alternatives, frontier,
    summary, slots, inventory, gold, goldRounded, planProgress, reasons}``.
    """
    if catalog is None:
        catalog = {}
    if not isinstance(catalog, dict):
        catalog = {}

    gold_raw = _gold_value(available_gold)
    gold = None if gold_raw is None else int(math.floor(gold_raw))
    gold_rounded = bool(gold_raw is not None and gold_raw != gold)
    counts, entries = _owned_state(owned_ids)
    slot_info = inventory_slots({'entries': entries}, catalog)
    slots = {'total': SLOT_COUNT, 'used': slot_info['used'],
             'free': slot_info['free'], 'ok': slot_info['ok']}
    inventory = {
        'entries': [{
            'id': entry['id'],
            'qty': entry['qty'],
            'stackable': _is_stackable(catalog, entry['id']),
            'trinket': _is_trinket(catalog, entry['id']),
        } for entry in entries],
        'trinkets': slot_info['trinkets'],
    }
    result = {
        'status': 'ok',
        'next': None,
        'alternatives': [],
        'frontier': [],
        'summary': None,
        'slots': slots,
        'inventory': inventory,
        'gold': gold,
        'goldRaw': gold_raw,
        'goldRounded': gold_rounded,
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

    pool = dict(counts)
    unknown_plan = []
    unsupported_plan = []
    saw_known = False
    target_index = None
    completed_steps = 0
    for index, entry in enumerate(plan):
        iid = entry['id']
        if iid is None:
            if entry['name']:
                unknown_plan.append(entry['name'])
            continue
        node = catalog.get(iid)
        if node is None:
            unknown_plan.append(entry['name'] or str(iid))
            continue
        if not node['recommendable']:
            unsupported_plan.append(entry['name'] or str(iid))
            continue
        saw_known = True
        if target_index is not None:
            continue
        if _consume_milestone(catalog, iid, pool) is not None:
            completed_steps += 1
        else:
            target_index = index

    progress = {'completed': completed_steps,
                'position': None if target_index is None else target_index + 1,
                'total': len(plan)}

    if target_index is None:
        result['status'] = 'plan_complete' if saw_known else 'unknown_item'
        result['planProgress'] = progress
        reasons = []
        if saw_known:
            reasons.append('plan complete')
        for unknown_name in unknown_plan[:3]:
            reasons.append('unknown plan item: %s' % unknown_name)
        for unsupported_name in unsupported_plan[:3]:
            reasons.append('unsupported for Summoner\'s Rift: %s' % unsupported_name)
        if not reasons:
            reasons.append('no known plan items')
        if gold_rounded:
            reasons.insert(0, 'current gold %sg rounded down to %dg'
                              ' (item costs are whole gold)' % (gold_raw, gold))
        result['reasons'] = reasons
        result['summary'] = {
            'nextRecall': None,
            'targetRemaining': 0 if saw_known else None,
            'goldShort': None,
            'alternatives': [],
            'text': 'Plan complete.' if saw_known else 'No known plan items.',
        }
        return result

    target = plan[target_index]
    node = catalog[target['id']]
    name = node['name'] or target['name'] or str(target['id'])
    remaining, exact, report = _analyze(catalog, target['id'], pool)
    affordable_now = bool(
        exact and remaining is not None and gold is not None and gold >= remaining)
    gold_short = None
    if remaining is not None and gold is not None:
        gold_short = max(0, remaining - gold)

    components = _component_offers(catalog, target['id'], pool, gold, report)
    for offer in components:
        add = _added_slots(catalog, counts, {offer['id']: 1})
        offer['slotsAfter'] = slots['used'] + add
        offer['feasibleNow'] = slots['used'] + add <= SLOT_COUNT

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

    consumed_slots = _freed_slots(catalog, counts, report['consumed'])
    target_slot_cost = _entry_slots(catalog, target['id'], 1)
    slots_after_complete = max(0, slots['used'] - consumed_slots + target_slot_cost)
    feasible_complete = slots_after_complete <= SLOT_COUNT

    suggested = None
    for offer in components:
        if offer['affordable'] and offer['feasibleNow']:
            suggested = offer
            break

    if affordable_now:
        action = 'complete'
        slots_after_action = slots_after_complete
        feasible_now = feasible_complete
        slot_delta = target_slot_cost - consumed_slots
    elif suggested is not None:
        action = 'component'
        slots_after_action = suggested['slotsAfter']
        feasible_now = suggested['feasibleNow']
        slot_delta = slots_after_action - slots['used']
    else:
        action = 'none'
        slots_after_action = slots['used']
        feasible_now = False
        slot_delta = 0

    result['next'] = {
        'id': target['id'],
        'name': name,
        'cost': node['cost'],
        'goldToComplete': remaining,
        'costExact': exact,
        'affordableNow': affordable_now,
        'goldShort': gold_short,
        'position': target_index + 1,
        'action': action,
        'feasibleNow': feasible_now,
        'slotsAfter': slots_after_action,
        'slotsAfterComplete': slots_after_complete,
        'slotsFreed': consumed_slots,
        'slotDelta': slot_delta,
        'suggested': None if suggested is None else {
            'id': suggested['id'],
            'name': suggested['name'],
            'cost': suggested['cost'],
            'goldToComplete': suggested['goldToComplete'],
            'copiesNeeded': suggested['copiesNeeded'],
            'feasibleNow': suggested['feasibleNow'],
            'slotsAfter': suggested['slotsAfter'],
        },
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
        if _milestone_owned(catalog, iid, pool):
            continue
        alt_remaining, alt_exact, _alt_report = _analyze(catalog, iid, pool)
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
    if gold_rounded:
        reasons.append('current gold %sg rounded down to %dg'
                       ' (item costs are whole gold)' % (gold_raw, gold))
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
            if not feasible_complete:
                reasons.append('no slot available even after combining components')
        elif gold is None:
            reasons.append('current gold unavailable')
        elif gold_short is not None:
            reasons.append('%dg short of %s' % (gold_short, name))
    for offer in cheapest:
        reasons.append('component %s affordable now (%dg)' % (
            offer['name'], offer['goldToComplete']))
    if not slots['ok']:
        reasons.append('no free inventory slot')
    for alternative in alternatives:
        reasons.append('alternative: %s affordable now (%dg)' % (
            alternative['name'], alternative['goldToComplete']))

    frontier = _frontier(catalog, target['id'], pool, counts, gold,
                         slots['free'], components)
    summary_text = _summary_text(name, remaining, exact, gold, affordable_now,
                                 suggested, gold_short)
    if alternatives:
        summary_text += ' Alternatives: ' + ', '.join(
            '%s (%dg)' % (alt['name'], alt['goldToComplete']) for alt in alternatives) + '.'
    frontier_gold = None
    if frontier:
        frontier_gold = frontier[0]['targetRemainingAfter']

    result['alternatives'] = alternatives
    result['frontier'] = frontier
    result['planProgress'] = progress
    result['summary'] = {
        'nextRecall': None if action == 'none' and gold is not None else {
            'kind': action,
            'id': suggested['id'] if action == 'component' else target['id'],
            'name': suggested['name'] if action == 'component' else name,
            'cost': suggested['goldToComplete'] if action == 'component' else remaining,
            'affordable': affordable_now or action == 'component',
            'feasible': feasible_now,
        },
        'targetRemaining': remaining if exact else None,
        'targetRemainingAfterBestCombo': frontier_gold,
        'goldShort': gold_short,
        'goldRounded': gold_rounded,
        'alternatives': [{
            'id': alt['id'],
            'name': alt['name'],
            'goldToComplete': alt['goldToComplete'],
        } for alt in alternatives],
        'text': summary_text,
    }
    # ``/api/purchase`` spreads ``next`` through unchanged, so mirror these
    # two structures there while keeping the top-level fields for direct
    # module callers.
    result['next']['frontier'] = result['frontier']
    result['next']['summary'] = result['summary']
    result['reasons'] = reasons
    return result
