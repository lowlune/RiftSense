"""Versioned champion/role coaching packs.

Packs are declarative JSON in ``knowledge/packs/``. This module loads them,
validates the provenance contract, selects one by champion and role, and
renders a section-budgeted prompt block plus a content manifest describing
exactly what reached the model.

Nothing here fetches the network or executes pack content: a pack is data.
"""

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACKS_DIR = os.path.join(ROOT, 'knowledge', 'packs')
SCHEMA_V1 = 'riftsense.pack.v1'
SCHEMA_V2 = 'riftsense.pack.v2'
SCHEMA = SCHEMA_V2
SUPPORTED_SCHEMAS = (SCHEMA_V1, SCHEMA_V2)
MANIFEST_SCHEMA = 'riftsense.pack.manifest.v1'
CLI_SCHEMA = 'riftsense.pack.cli.v1'
DEFAULT_MAX_CHARS = 1800
UNVERIFIED_TAG = '[UNVERIFIED - do not quote as fact]'
GLOBAL_MARKER = '...[truncated]'
MIN_ENTRY_BYTES = 120

ROLE_ALIASES = {
    'jgl': 'jungle',
    'jg': 'jungle',
    'jung': 'jungle',
    'jungler': 'jungle',
    'top': 'top',
    'mid': 'middle',
    'middle': 'middle',
    'midlaner': 'middle',
    'bot': 'bottom',
    'adc': 'bottom',
    'carry': 'bottom',
    'bottom': 'bottom',
    'botlaner': 'bottom',
    'sup': 'support',
    'supp': 'support',
    'support': 'support',
    'utility': 'support',
    'utilities': 'support',
}

REQUIRED_FIELDS = {
    'mechanics': ('name', 'value', 'source'),
    'itemNotes': ('name', 'note', 'source'),
    'counters': ('name', 'note', 'source'),
    'decisionRules': ('rule', 'preconditions', 'source'),
    'derived': ('name', 'value', 'source'),
    'heuristics': ('name', 'note', 'source'),
    'statistics': ('name', 'note', 'source', 'sample'),
    'abstentions': ('when', 'missing', 'reason'),
}

ALWAYS_UNVERIFIED = ('derived', 'heuristics', 'statistics', 'abstentions')

DERIVED_WORDS = ('derived', 'arithmetic', 'calculated', 'computed', 'extrapolated')

SECTION_SPECS = (
    ('decisionRules', 'DECISION RULES:', 0.30),
    ('mechanics', 'MECHANICS (patch-scoped):', 0.24),
    ('counters', 'COUNTERS:', 0.14),
    ('itemNotes', 'ITEM NOTES:', 0.12),
    ('derived', 'DERIVED (math - do not quote as a value):', 0.06),
    ('heuristics', 'HEURISTICS (preferences, not facts):', 0.06),
    ('statistics', 'STATISTICS (sampled, not causal):', 0.04),
    ('abstentions', 'ABSTENTIONS (output ABSTAIN: when these are missing):', 0.04),
)


def normalize_name(value):
    """Case/punctuation-insensitive champion key ('Nunu & Willump' -> 'nunuandwillump')."""
    text = str(value or '').lower()
    text = text.replace('&', ' and ')
    return re.sub(r'[^a-z0-9]+', '', text)


def normalize_role(value):
    text = re.sub(r'[^a-z]+', '', str(value or '').lower())
    if not text:
        return ''
    return ROLE_ALIASES.get(text, text)


def _text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip()


def _nonempty(value):
    return isinstance(value, str) and value.strip() != ''


def _valid_pack(obj):
    if not isinstance(obj, dict):
        return False
    if obj.get('schema') not in SUPPORTED_SCHEMAS:
        return False
    if not _nonempty(obj.get('champion')):
        return False
    return True


def _pack_names(pack):
    names = []
    champion = pack.get('champion')
    if isinstance(champion, str):
        names.append(champion)
    aliases = pack.get('aliases')
    if isinstance(aliases, list):
        names.extend(alias for alias in aliases if isinstance(alias, str))
    return names


def _pack_roles(pack):
    roles = pack.get('roles')
    if not isinstance(roles, list):
        return []
    return [normalize_role(role) for role in roles
            if isinstance(role, str) and normalize_role(role)]


def _pack_id(pack):
    key = normalize_name(pack.get('champion'))
    if key:
        return key
    file_name = pack.get('file')
    if isinstance(file_name, str) and file_name:
        return os.path.splitext(file_name)[0]
    return 'unknown'


def _looks_derived(*values):
    blob = ' '.join(str(value).lower() for value in values if value)
    return any(word in blob for word in DERIVED_WORDS)


def validate_pack(pack):
    """Validate and normalize a pack in place, returning a list of violations.

    Normalization: non-compliant ``verified`` flags are downgraded to ``False``,
    and each entry inherits the pack-level ``patch``/``retrieved`` provenance.
    Categories ``derived``/``heuristics``/``statistics``/``abstentions`` are
    never verifiable. The live path keeps loading annotated packs so one bad
    claim cannot take the coach offline.
    """
    violations = []
    if not isinstance(pack, dict):
        return [{'field': 'pack', 'message': 'pack must be a JSON object'}]
    if pack.get('schema') not in SUPPORTED_SCHEMAS:
        violations.append({'field': 'schema', 'message': 'unsupported schema %r' % (pack.get('schema'),)})
    if not _nonempty(pack.get('champion')):
        violations.append({'field': 'champion', 'message': 'champion is required'})
    if not _nonempty(pack.get('patch')):
        violations.append({'field': 'patch', 'message': 'patch is required for verified claims'})
    if not _nonempty(pack.get('retrieved')):
        violations.append({'field': 'retrieved', 'message': 'retrieved date is required for verified claims'})
    if not isinstance(pack.get('roles'), list):
        violations.append({'field': 'roles', 'message': 'roles must be a list'})
    scoped = _nonempty(pack.get('patch')) and _nonempty(pack.get('retrieved'))
    for category, fields in REQUIRED_FIELDS.items():
        entries = pack.get(category)
        if entries is None:
            if pack.get('schema') == SCHEMA_V2:
                violations.append({'field': category,
                                   'message': 'category list is required in %s' % SCHEMA_V2})
            continue
        if not isinstance(entries, list):
            violations.append({'field': category, 'message': 'category must be a list'})
            continue
        for index, entry in enumerate(entries):
            path = '%s[%d]' % (category, index)
            if not isinstance(entry, dict):
                violations.append({'field': path, 'message': 'entry must be an object'})
                continue
            for field in fields:
                if field not in entry:
                    violations.append({'field': path, 'message': 'missing required field %r' % field})
            if 'preconditions' in fields and not isinstance(entry.get('preconditions'), list):
                violations.append({'field': path, 'message': 'preconditions must be a list'})
            if 'sample' in fields and not _nonempty(entry.get('sample')):
                violations.append({'field': path, 'message': 'sample size/label is required for statistics'})
            if 'verified' in entry and not isinstance(entry.get('verified'), bool):
                violations.append({'field': path, 'message': 'verified must be a boolean'})
            if scoped:
                entry.setdefault('patch', pack.get('patch'))
                entry.setdefault('retrieved', pack.get('retrieved'))
            if category in ALWAYS_UNVERIFIED:
                if entry.get('verified') is True:
                    violations.append({'field': path,
                                       'message': 'category %s must not be marked verified' % category})
                entry['verified'] = False
                continue
            if entry.get('verified') is True:
                if not _nonempty(entry.get('source')):
                    violations.append({'field': path, 'message': 'verified claim requires a source'})
                    entry['verified'] = False
                    continue
                if category == 'mechanics' and _looks_derived(entry.get('name'), entry.get('value'),
                                                              entry.get('source')):
                    violations.append({'field': path,
                                       'message': 'derived arithmetic belongs in the derived category'})
                    entry['verified'] = False
                    continue
                if category == 'decisionRules':
                    preconditions = entry.get('preconditions')
                    if not isinstance(preconditions, list) or not any(_nonempty(p) for p in preconditions):
                        violations.append({'field': path,
                                           'message': 'verified decision rule requires preconditions'})
                        entry['verified'] = False
                        continue
                if not scoped:
                    violations.append({'field': path,
                                       'message': 'missing pack patch/retrieved - claim downgraded'})
                    entry['verified'] = False
            else:
                entry.setdefault('verified', False)
    return violations


def load_packs(directory=None):
    """Load every valid pack into a dict keyed by normalized champion name.

    Invalid or unreadable files are skipped (the live path must never fail
    because one pack is malformed). Each pack gets a ``file`` field with the
    source file name and a ``_violations`` list from :func:`validate_pack`.
    """
    directory = PACKS_DIR if directory is None else directory
    packs = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return packs
    for name in names:
        if not name.endswith('.json') or name.startswith('.'):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, 'r', encoding='utf-8-sig') as handle:
                obj = json.load(handle)
        except (OSError, ValueError):
            continue
        if not _valid_pack(obj):
            continue
        key = normalize_name(obj.get('champion'))
        if not key:
            continue
        pack = dict(obj)
        pack['file'] = name
        pack['_violations'] = validate_pack(pack)
        packs[key] = pack
    return packs


def select_pack(champ, role=None, packs=None, require_role=False):
    """Return the pack for ``champ`` (and ``role`` when given), else None.

    Champion matching is case-insensitive and ignores punctuation and ``&``
    vs ``and``. A role mismatch on a pack that declares roles returns None.
    With ``require_role=True`` a pack that declares roles is only returned
    when the caller supplied a matching role, so champion-only callers cannot
    silently receive a role-specific pack.
    """
    key = normalize_name(champ)
    if not key:
        return None
    if packs is None:
        packs = load_packs()
    role_key = normalize_role(role) if role else ''
    matches = []
    for pack in packs.values():
        if key not in [normalize_name(name) for name in _pack_names(pack)]:
            continue
        roles = _pack_roles(pack)
        if role_key and roles and role_key not in roles:
            continue
        if require_role and roles and not role_key:
            continue
        matches.append(pack)
    if not matches:
        return None
    if role_key:
        for pack in matches:
            if role_key in _pack_roles(pack):
                return pack
    return matches[0]


def _flatten_state(state):
    keys = set()
    values = []

    def add(key, value):
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                add(str(sub_key), sub_value)
        elif isinstance(value, list):
            for item in value:
                add(key, item)
        elif value is not None and value != '' and value is not False:
            keys.add(str(key).lower())
            values.append(str(value).lower())

    if isinstance(state, str):
        try:
            state = json.loads(state)
        except ValueError:
            return set(), [state.lower()]
    if isinstance(state, dict):
        for key, value in state.items():
            add(str(key), value)
    return keys, values


def _relevance(entry, state_keys, state_values):
    score = 0
    triggers = entry.get('triggers')
    if isinstance(triggers, str):
        triggers = [triggers]
    if not isinstance(triggers, list) and _nonempty(entry.get('when')):
        triggers = [entry.get('when')]
    if isinstance(triggers, list):
        for trigger in triggers:
            trigger = _text(trigger).lower()
            if not trigger:
                continue
            if any(trigger in key or key in trigger for key in state_keys):
                score += 3
            elif any(trigger in value for value in state_values):
                score += 2
    preconditions = entry.get('preconditions')
    if isinstance(preconditions, list):
        for precondition in preconditions:
            for word in re.findall(r'[a-z]{4,}', _text(precondition).lower()):
                if any(word in key or key in word for key in state_keys):
                    score += 1
                    break
                if any(word in value for value in state_values):
                    score += 1
                    break
    return score


def _section_entries(pack, category, state):
    entries = pack.get(category)
    if not isinstance(entries, list):
        return []
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if state and category in ('decisionRules', 'mechanics', 'heuristics', 'abstentions'):
        state_keys, state_values = _flatten_state(state)
        if state_keys or state_values:
            decorated = []
            for order, entry in enumerate(entries):
                decorated.append((-_relevance(entry, state_keys, state_values), order, entry))
            decorated.sort()
            entries = [entry for _, _, entry in decorated]
    return entries


def _suffix(entry):
    return '' if entry.get('verified') is True else ' ' + UNVERIFIED_TAG


def _entry_lines(category, entry):
    if category == 'decisionRules':
        rule = _text(entry.get('rule'))
        if not rule:
            return []
        preconditions = entry.get('preconditions')
        pre_text = '; '.join(_text(p) for p in preconditions if _nonempty(p)) \
            if isinstance(preconditions, list) else ''
        return ['- ' + rule + (' [pre: ' + pre_text + ']' if pre_text else ' [pre: none stated]')
                + _suffix(entry)]
    if category == 'mechanics' or category == 'derived':
        name = _text(entry.get('name'))
        value = _text(entry.get('value'))
        if not name and not value:
            return []
        return ['- ' + ((name + ': ') if name else '') + value + _suffix(entry)]
    if category == 'heuristics':
        name = _text(entry.get('name'))
        note = _text(entry.get('note'))
        if not name and not note:
            return []
        return ['- ' + ((name + ': ') if name else '') + note + _suffix(entry)]
    if category == 'statistics':
        name = _text(entry.get('name'))
        note = _text(entry.get('note'))
        sample = _text(entry.get('sample'))
        if not name and not note:
            return []
        label = name + (' [sample: ' + sample + ']' if sample else '')
        return ['- ' + label + ': ' + note + _suffix(entry)]
    if category == 'counters':
        name = _text(entry.get('name'))
        note = _text(entry.get('note'))
        if not name and not note:
            return []
        return ['- vs ' + name + ': ' + note + _suffix(entry) if name else '- ' + note + _suffix(entry)]
    if category == 'itemNotes':
        name = _text(entry.get('name'))
        note = _text(entry.get('note'))
        if not name and not note:
            return []
        return ['- ' + ((name + ': ') if name else '') + note + _suffix(entry)]
    if category == 'abstentions':
        when = _text(entry.get('when'))
        missing = _text(entry.get('missing'))
        reason = _text(entry.get('reason'))
        if not when:
            return []
        return ['- ' + when + ': ABSTAIN (' + (missing or 'reason not stated') + ') - ' + reason]
    return []


def _section_plan(pack, state):
    specs = []
    for category, header, weight in SECTION_SPECS:
        entries = _section_entries(pack, category, state)
        lines = []
        for entry in entries:
            lines.extend(_entry_lines(category, entry))
        specs.append({'id': category, 'header': header, 'lines': lines,
                      'total': len(entries), 'weight': weight})
    return specs


def _allocate_budgets(specs, available):
    budgets = {}
    remaining = max(0, available)
    active = [spec for spec in specs if spec['lines']]
    active.sort(key=lambda spec: (-spec['weight'],))
    for spec in active:
        first = spec['lines'][0]
        need = len(spec['header']) + 1 + min(len(first), MIN_ENTRY_BYTES)
        if need <= remaining:
            budgets[spec['id']] = need
            remaining -= need
        else:
            budgets[spec['id']] = 0
    demand = {}
    for spec in specs:
        if not spec['lines']:
            demand[spec['id']] = 0
            continue
        demand[spec['id']] = len(spec['header']) + sum(1 + len(line) for line in spec['lines'])
    for _ in range(4):
        pending = [spec for spec in active if budgets.get(spec['id'], 0) < demand[spec['id']]]
        if not pending or remaining <= 0:
            break
        weight_sum = sum(spec['weight'] for spec in pending) or 1.0
        added = 0
        for spec in pending:
            share = int(remaining * spec['weight'] / weight_sum)
            gap = demand[spec['id']] - budgets.get(spec['id'], 0)
            add = min(gap, share)
            budgets[spec['id']] = budgets.get(spec['id'], 0) + add
            added += add
        remaining -= added
        if added == 0:
            break
    if remaining > 0:
        for spec in active:
            gap = demand[spec['id']] - budgets.get(spec['id'], 0)
            add = min(gap, remaining)
            budgets[spec['id']] = budgets.get(spec['id'], 0) + add
            remaining -= add
            if remaining <= 0:
                break
    return budgets


def _clip_line(line, room):
    if room <= 3:
        return line[:max(0, room)]
    head = line[:room - 3].rsplit(' ', 1)[0] or line[:room - 3]
    return head + '...'


def _render_section(header, lines, budget):
    if budget <= 0 or not lines:
        return [], 0, 0, bool(lines)
    if budget <= len(header):
        return [header[:budget]], len(header[:budget]), 0, True
    out = [header]
    used = len(header)
    shown = 0
    for line in lines:
        cost = 1 + len(line)
        if used + cost <= budget:
            out.append(line)
            used += cost
            shown += 1
        else:
            break
    truncated = shown < len(lines)
    if truncated:
        reserve = 1 + len(GLOBAL_MARKER)
        while shown > 0 and used + reserve > budget:
            used -= 1 + len(out[-1])
            out.pop()
            shown -= 1
        if shown == 0:
            room = budget - len(header) - reserve
            if room > 30:
                clipped = _clip_line(lines[0], room)
                if UNVERIFIED_TAG in lines[0] and UNVERIFIED_TAG not in clipped:
                    extra = len(UNVERIFIED_TAG) + 1
                    if room > extra + 10:
                        clipped = _clip_line(lines[0], room - extra) + ' ' + UNVERIFIED_TAG
                out.append(clipped)
                used = len(header) + 1 + len(clipped)
                shown = 1
        if used + reserve <= budget:
            out.append(GLOBAL_MARKER)
            used += reserve
    return out, used, shown, truncated


def _hard_cap(text, max_chars):
    if len(text) <= max_chars:
        return text
    if max_chars <= len(GLOBAL_MARKER):
        return text[:max_chars]
    lines = text.split('\n')
    while lines and len('\n'.join(lines)) + 1 + len(GLOBAL_MARKER) > max_chars:
        lines.pop()
    if not lines:
        return text[:max_chars]
    return '\n'.join(lines) + '\n' + GLOBAL_MARKER


def _render_pack(pack, max_chars, state):
    if not isinstance(pack, dict):
        return '', _manifest(pack, max_chars, state, '', [], False)
    champion = _text(pack.get('champion')) or 'unknown champion'
    roles = [normalize_role(role) for role in (pack.get('roles') or [])
             if isinstance(role, str) and role.strip()] if isinstance(pack.get('roles'), list) else []
    meta = []
    if roles:
        meta.append('/'.join(roles))
    meta.append('patch ' + (_text(pack.get('patch')) or 'unknown'))
    retrieved = _text(pack.get('retrieved'))
    if retrieved:
        meta.append('retrieved ' + retrieved)
    header_text = '=== CHAMPION PACK (versioned) ===\n' + champion + ' | ' + ' | '.join(meta)
    specs = _section_plan(pack, state)
    if max_chars <= 0:
        return '', _manifest(pack, max_chars, state, '', specs, False)
    if len(header_text) >= max_chars:
        text = header_text[:max_chars]
        return text, _manifest(pack, max_chars, state, text, specs, True)
    active = [spec for spec in specs if spec['lines']]
    available = max_chars - len(header_text) - (1 + len(GLOBAL_MARKER)) - len(active)
    budgets = _allocate_budgets(specs, max(0, available))
    chunks = [header_text]
    section_manifest = []
    truncated_any = False
    for spec in specs:
        lines, used, shown, truncated = _render_section(spec['header'], spec['lines'],
                                                        budgets.get(spec['id'], 0))
        if lines and used > 0:
            chunks.append('\n'.join(lines))
            section_manifest.append({
                'id': spec['id'],
                'header': spec['header'],
                'bytes': used,
                'lines': len(lines),
                'entries': shown,
                'entries_total': spec['total'],
                'truncated': truncated,
                'budget': budgets.get(spec['id'], 0),
                'dropped': False,
            })
        else:
            section_manifest.append({
                'id': spec['id'],
                'header': spec['header'],
                'bytes': 0,
                'lines': 0,
                'entries': 0,
                'entries_total': spec['total'],
                'truncated': bool(spec['lines']),
                'budget': budgets.get(spec['id'], 0),
                'dropped': bool(spec['lines']),
            })
        truncated_any = truncated_any or truncated
    text = '\n'.join(chunks)
    if truncated_any and len(text) + 1 + len(GLOBAL_MARKER) <= max_chars:
        text = text + '\n' + GLOBAL_MARKER
    text = _hard_cap(text, max_chars)
    return text, _manifest(pack, max_chars, state, text, specs, truncated_any,
                           section_manifest, len(header_text))


def _manifest(pack, max_chars, state, text, specs, truncated, sections=None, header_bytes=None):
    violations = []
    if isinstance(pack, dict):
        raw = pack.get('_violations')
        if isinstance(raw, list):
            violations = raw
    if sections is None:
        sections = [{'id': spec['id'], 'header': spec['header'], 'bytes': 0, 'lines': 0,
                     'entries': 0, 'entries_total': spec['total'],
                     'truncated': bool(spec['lines']), 'budget': 0,
                     'dropped': bool(spec['lines'])} for spec in specs]
    if header_bytes is None:
        header_bytes = 0
        if text:
            header_bytes = len(text.split('\n')[0])
    entries = [{'id': 'header', 'header': '', 'bytes': header_bytes, 'lines': 1,
                'entries': 1, 'entries_total': 1, 'truncated': False, 'budget': None,
                'dropped': False}]
    entries.extend(sections)
    manifest = {
        'schema': MANIFEST_SCHEMA,
        'id': _pack_id(pack) if isinstance(pack, dict) else 'unknown',
        'version': _text(pack.get('patch')) if isinstance(pack, dict) else 'unknown',
        'file': pack.get('file') if isinstance(pack, dict) else None,
        'champion': pack.get('champion') if isinstance(pack, dict) else None,
        'roles': _pack_roles(pack) if isinstance(pack, dict) else [],
        'patch': _text(pack.get('patch')) if isinstance(pack, dict) else 'unknown',
        'retrieved': _text(pack.get('retrieved')) if isinstance(pack, dict) else '',
        'pack_schema': pack.get('schema') if isinstance(pack, dict) else None,
        'max_chars': max_chars,
        'bytes': len(text),
        'lines': (text.count('\n') + 1) if text else 0,
        'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()[:16] if text else '',
        'truncated': bool(truncated),
        'state_used': bool(state),
        'sections': entries,
        'dropped': [section['id'] for section in sections if section.get('dropped')],
        'violations': violations,
    }
    return manifest


def pack_prompt(pack, max_chars=DEFAULT_MAX_CHARS, state=None, with_manifest=False):
    """Render the compact CHAMPION PACK block, at most ``max_chars`` long.

    Backward compatible: with ``with_manifest=False`` (default) this returns
    the block text exactly as before. With ``with_manifest=True`` it returns
    ``(text, manifest)`` where the manifest lists the pack id/version, the
    section names and the byte counts that actually reached the model.
    ``state`` is an optional observed-state dict/JSON used to prefer rules
    whose preconditions match the current game.
    """
    if not isinstance(pack, dict):
        if with_manifest:
            return '', _manifest(pack, DEFAULT_MAX_CHARS, state, '', [], False)
        return ''
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    text, manifest = _render_pack(pack, max_chars, state)
    if with_manifest:
        return text, manifest
    return text


def pack_manifest(pack, max_chars=DEFAULT_MAX_CHARS, state=None):
    """Return only the content manifest for :func:`pack_prompt`."""
    return pack_prompt(pack, max_chars, state, with_manifest=True)[1]


def list_packs(directory=None):
    """Summary rows for the UI: champion, roles, patch, retrieved, file, validity."""
    rows = []
    for pack in load_packs(directory).values():
        violations = pack.get('_violations') or []
        rows.append({
            'champion': pack.get('champion'),
            'roles': pack.get('roles') if isinstance(pack.get('roles'), list) else [],
            'patch': pack.get('patch'),
            'retrieved': pack.get('retrieved'),
            'schema': pack.get('schema'),
            'file': pack.get('file'),
            'id': _pack_id(pack),
            'violations': len(violations),
        })
    rows.sort(key=lambda row: str(row.get('champion') or '').lower())
    return rows


def _load_state(args):
    raw = ''
    if args.state_file:
        try:
            with open(args.state_file, 'r', encoding='utf-8-sig') as handle:
                raw = handle.read()
        except OSError:
            return None
    elif args.state_json:
        raw = args.state_json
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _pack_summary(pack):
    return {
        'id': _pack_id(pack),
        'schema': pack.get('schema'),
        'file': pack.get('file'),
        'champion': pack.get('champion'),
        'roles': _pack_roles(pack),
        'patch': pack.get('patch'),
        'retrieved': pack.get('retrieved'),
        'violations': len(pack.get('_violations') or []),
    }


def _main(argv=None):
    parser = argparse.ArgumentParser(description='RiftSense champion packs')
    parser.add_argument('--prompt', action='store_true', help='print the prompt block for --champ')
    parser.add_argument('--select', action='store_true', help='resolve the pack for --champ/--role')
    parser.add_argument('--validate', action='store_true', help='validate every pack provenance contract')
    parser.add_argument('--list', action='store_true', help='list available packs')
    parser.add_argument('--champ', default='', help='champion name')
    parser.add_argument('--role', default='', help='role filter (jungle/top/middle/bottom/support)')
    parser.add_argument('--require-role', action='store_true',
                        help='refuse role-scoped packs when --role is missing or mismatched')
    parser.add_argument('--state-json', default='', help='observed state as inline JSON')
    parser.add_argument('--state-file', default='', help='observed state JSON file path')
    parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args(argv)
    state = _load_state(args)

    if args.validate:
        rows = []
        for pack in load_packs().values():
            rows.append({'file': pack.get('file'), 'champion': pack.get('champion'),
                         'id': _pack_id(pack), 'violations': pack.get('_violations') or []})
        bad = any(row['violations'] for row in rows)
        if args.json:
            print(json.dumps({'schema': CLI_SCHEMA, 'ok': not bad, 'packs': rows}, indent=2))
        else:
            for row in rows:
                status = 'ok' if not row['violations'] else '%d violation(s)' % len(row['violations'])
                print('%s | %s | %s' % (row['file'], row['champion'], status))
                for violation in row['violations']:
                    print('  %s: %s' % (violation.get('field'), violation.get('message')))
        return 1 if bad else 0
    if args.list:
        rows = list_packs()
        if args.json:
            print(json.dumps({'schema': CLI_SCHEMA, 'ok': bool(rows), 'count': len(rows),
                              'packs': rows}, indent=2))
        else:
            for row in rows:
                print('%s | %s | patch %s | retrieved %s | violations %d' % (
                    row.get('champion'), '/'.join(row.get('roles') or []) or '-',
                    row.get('patch') or '?', row.get('retrieved') or '?', row.get('violations', 0)))
        return 0
    if args.prompt or args.select:
        if not args.champ:
            parser.print_usage(sys.stderr)
            return 2
        pack = select_pack(args.champ, args.role or None, require_role=args.require_role)
        if pack is None:
            if args.json:
                print(json.dumps({'schema': CLI_SCHEMA, 'ok': False, 'status': 'no_pack',
                                  'champ': args.champ, 'role': args.role or None}))
            else:
                print('no_pack: %s' % (args.champ or '?'))
            return 1
        if args.select:
            payload = {'schema': CLI_SCHEMA, 'ok': True, 'champ': args.champ,
                       'role': args.role or None, 'pack': _pack_summary(pack)}
            if args.json:
                print(json.dumps(payload))
            else:
                print('%s | %s | patch %s' % (pack.get('champion'),
                                              '/'.join(_pack_roles(pack)) or '-',
                                              pack.get('patch') or '?'))
            return 0
        try:
            max_chars = int(args.max_chars)
        except (TypeError, ValueError):
            max_chars = DEFAULT_MAX_CHARS
        text, manifest = pack_prompt(pack, max_chars, state, with_manifest=True)
        if args.json:
            print(json.dumps({'schema': CLI_SCHEMA, 'ok': True, 'champ': args.champ,
                              'role': args.role or None, 'pack': _pack_summary(pack),
                              'text': text, 'manifest': manifest}, indent=2))
        else:
            sys.stdout.write(text)
        return 0
    parser.print_usage(sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(_main())
