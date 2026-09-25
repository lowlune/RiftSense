"""Versioned champion/role coaching packs.

Packs are declarative JSON in ``knowledge/packs/``. This module loads them,
selects one by champion (and optional role) with punctuation-insensitive
normalization, and renders a compact prompt block for the live coach.

Nothing here fetches the network or executes pack content: a pack is data.
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACKS_DIR = os.path.join(ROOT, 'knowledge', 'packs')
SCHEMA = 'riftsense.pack.v1'
DEFAULT_MAX_CHARS = 1800
UNVERIFIED_TAG = '[UNVERIFIED - do not quote as fact]'

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
}


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


def _valid_pack(obj):
    if not isinstance(obj, dict):
        return False
    if obj.get('schema') != SCHEMA:
        return False
    champion = obj.get('champion')
    if not isinstance(champion, str) or not champion.strip():
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


def load_packs(directory=None):
    """Load every valid pack into a dict keyed by normalized champion name.

    Invalid or unreadable files are skipped (the live path must never fail
    because one pack is malformed). Each pack gets a ``file`` field with the
    source file name.
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
        packs[key] = pack
    return packs


def select_pack(champ, role=None, packs=None):
    """Return the pack for ``champ`` (and ``role`` when given), else None.

    Champion matching is case-insensitive and ignores punctuation and ``&``
    vs ``and``. A role mismatch on a pack that declares roles returns None.
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
        matches.append(pack)
    if not matches:
        return None
    if role_key:
        for pack in matches:
            if role_key in _pack_roles(pack):
                return pack
    return matches[0]


def _truncate(lines, max_chars):
    text = '\n'.join(lines)
    if len(text) <= max_chars:
        return text
    marker = '\n...[truncated]'
    room = max_chars - len(marker)
    if room <= 0:
        return text[:max_chars]
    kept = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if kept else 0)
        if used + extra > room:
            break
        kept.append(line)
        used += extra
    if not kept:
        return text[:max_chars]
    return '\n'.join(kept) + marker


def _suffix(entry):
    return '' if entry.get('verified') is True else ' ' + UNVERIFIED_TAG


def pack_prompt(pack, max_chars=DEFAULT_MAX_CHARS):
    """Render the compact CHAMPION PACK block, at most ``max_chars`` long."""
    if not isinstance(pack, dict):
        return ''
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    if max_chars <= 0:
        return ''

    lines = ['=== CHAMPION PACK (versioned) ===']
    champion = _text(pack.get('champion')) or 'unknown champion'
    roles = [normalize_role(r) for r in (pack.get('roles') or [])
             if isinstance(r, str) and r.strip()] if isinstance(pack.get('roles'), list) else []
    meta = []
    if roles:
        meta.append('/'.join(roles))
    meta.append('patch ' + (_text(pack.get('patch')) or 'unknown'))
    retrieved = _text(pack.get('retrieved'))
    if retrieved:
        meta.append('retrieved ' + retrieved)
    lines.append(champion + ' | ' + ' | '.join(meta))

    rules = pack.get('decisionRules')
    if isinstance(rules, list):
        body = []
        for entry in rules:
            if not isinstance(entry, dict):
                continue
            rule = _text(entry.get('rule'))
            if not rule:
                continue
            pre = entry.get('preconditions')
            pre_text = '; '.join(_text(p) for p in pre
                                 if isinstance(p, str) and _text(p)) if isinstance(pre, list) else ''
            body.append('- ' + rule + (' [pre: ' + pre_text + ']' if pre_text else ' [pre: none stated]')
                        + _suffix(entry))
        if body:
            lines.append('DECISION RULES:')
            lines.extend(body)

    mechanics = pack.get('mechanics')
    if isinstance(mechanics, list):
        body = []
        for entry in mechanics:
            if not isinstance(entry, dict):
                continue
            name = _text(entry.get('name'))
            value = _text(entry.get('value'))
            if not name and not value:
                continue
            body.append('- ' + ((name + ': ') if name else '') + value + _suffix(entry))
        if body:
            lines.append('MECHANICS (patch-scoped):')
            lines.extend(body)

    counters = pack.get('counters')
    if isinstance(counters, list):
        body = []
        for entry in counters:
            if not isinstance(entry, dict):
                continue
            name = _text(entry.get('name'))
            note = _text(entry.get('note'))
            if not name and not note:
                continue
            body.append('- vs ' + name + ': ' + note + _suffix(entry) if name else '- ' + note + _suffix(entry))
        if body:
            lines.append('COUNTERS:')
            lines.extend(body)

    item_notes = pack.get('itemNotes')
    if isinstance(item_notes, list):
        body = []
        for entry in item_notes:
            if not isinstance(entry, dict):
                continue
            name = _text(entry.get('name'))
            note = _text(entry.get('note'))
            if not name and not note:
                continue
            body.append('- ' + ((name + ': ') if name else '') + note + _suffix(entry))
        if body:
            lines.append('ITEM NOTES:')
            lines.extend(body)

    return _truncate(lines, max_chars)


def list_packs(directory=None):
    """Summary rows for the UI: champion, roles, patch, retrieved, file."""
    rows = []
    for pack in load_packs(directory).values():
        rows.append({
            'champion': pack.get('champion'),
            'roles': pack.get('roles') if isinstance(pack.get('roles'), list) else [],
            'patch': pack.get('patch'),
            'retrieved': pack.get('retrieved'),
            'schema': pack.get('schema'),
            'file': pack.get('file'),
        })
    rows.sort(key=lambda row: str(row.get('champion') or '').lower())
    return rows


def _main(argv=None):
    parser = argparse.ArgumentParser(description='RiftSense champion packs')
    parser.add_argument('--prompt', action='store_true', help='print the prompt block for --champ')
    parser.add_argument('--list', action='store_true', help='list available packs')
    parser.add_argument('--champ', default='', help='champion name')
    parser.add_argument('--role', default='', help='role filter (jungle/top/middle/bottom/support)')
    parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS)
    args = parser.parse_args(argv)

    if args.list:
        for row in list_packs():
            print('%s | %s | patch %s | retrieved %s' % (
                row.get('champion'), '/'.join(row.get('roles') or []) or '-',
                row.get('patch') or '?', row.get('retrieved') or '?'))
        return 0
    if args.prompt and args.champ:
        pack = select_pack(args.champ, args.role or None)
        if pack:
            sys.stdout.write(pack_prompt(pack, args.max_chars))
            return 0
        return 1
    parser.print_usage(sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(_main())
