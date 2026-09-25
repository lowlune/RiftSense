# Champion/role packs

Versioned, human-reviewed coaching packs that are injected into the live
AutoCoach prompt (and served read-only by the UI). A pack is data, not code:
`ui/packs.py` loads and validates it, `ui/server.py` exposes it, `AutoCoach.ps1`
appends `pack_prompt()` output to the inference prompt, and the dashboard shows
the active pack badge.

Live path today:

```
knowledge/packs/<champion>.json
  -> ui/packs.py select_pack(champ, role) / pack_prompt(pack, max_chars, state)
  -> AutoCoach.ps1 Start-CoachRun -> === CHAMPION PACK (versioned) ===
  -> opencode agent (agent/lol-coach.md)
```

`pack_prompt()` is section-budgeted: it caps the block at 1800 characters by
default but keeps every section (rules, mechanics, counters, item notes,
derived math, heuristics, statistics, abstentions) represented, truncating
inside a section instead of dropping it. It also returns a content manifest
describing exactly what reached the model. `knowledge/nunu-modern.md` remains
the long-form reference dossier; packs are the curated runtime subset.

## Schema

Every pack is a single JSON object. `schema` is `"riftsense.pack.v2"`.
(`"riftsense.pack.v1"` packs still load, annotated with contract violations;
new packs must use v2.)

| Field | Required | Type | Meaning |
|---|---|---|---|
| `schema` | yes | string | `"riftsense.pack.v2"`; unknown schemas are skipped. |
| `champion` | yes | string | Canonical champion name, e.g. `"Nunu & Willump"`. |
| `aliases` | no | string[] | Extra names accepted by `select_pack()` (`"Nunu"`, `"WW"`). |
| `roles` | yes | string[] | `top`/`jungle`/`middle`/`bottom`/`support`. Empty matches any role. |
| `patch` | yes | string | Patch the numbers were checked against, e.g. `"26.19"`. |
| `retrieved` | yes | string | ISO date the values were read, e.g. `"2026-09-23"`. |
| `review` | no | object | Reviewer/date/scope attestation for the last re-check. |
| `sources` | no | string[] | Top-level source list (each entry also carries its own `source`). |
| `mechanics` | yes | object[] | `{name, value, source, verified}` - **quoted values only**. |
| `derived` | yes | object[] | `{name, value, source}` - arithmetic you produced. Never verified. |
| `heuristics` | yes | object[] | `{name, note, source}` - preferences/rules of thumb. Never verified. |
| `statistics` | yes | object[] | `{name, note, source, sample}` - sampled data. Never verified. |
| `itemNotes` | yes | object[] | `{name, note, source, verified}` - item stats, costs, interactions. |
| `counters` | yes | object[] | `{name, note, source, verified}` - enemy item/mechanic counterplay. |
| `decisionRules` | yes | object[] | `{rule, preconditions, source, verified}`; `triggers` optional. |
| `abstentions` | yes | object[] | `{when, missing, reason}` - when the agent must output `ABSTAIN:`. |

`verified` is a strict boolean and is only meaningful in `mechanics`,
`itemNotes`, `counters`, and `decisionRules`:

- `true` - the value is a quoted, patch-scoped fact from the cited source that
  the coach may treat as a fact. Requires a `source`, the pack-level `patch`
  and `retrieved`, and for decision rules non-empty `preconditions`.
- `false` - unvalidated, derived, third-party or statistical. The renderer
  marks it `[UNVERIFIED - do not quote as fact]`; it may be shown but never
  used as a secure/buy number.

`derived`, `heuristics`, `statistics`, and `abstentions` entries are **always**
rendered unverified; the loader forces `verified: false` and records a
violation if a pack claims otherwise. `statistics` must state `sample`.
The loader never rejects a whole pack for entry-level violations: it annotates
`pack['_violations']` and downgrades the offending claims, so the live path
stays up while the contract stays enforceable. Use `--validate` in CI.

Each `decisionRule` understands an optional `triggers` string[] naming the
observed-state fields that make the rule relevant (e.g. `"smite_stage"`). When
`pack_prompt(..., state=...)` is passed, rules are ordered by trigger and
precondition relevance before the section budget is applied.

Example (minimum v2 pack):

```json
{
  "schema": "riftsense.pack.v2",
  "champion": "Warwick",
  "roles": ["jungle"],
  "patch": "26.19",
  "retrieved": "2026-09-25",
  "mechanics": [
    {"name": "Q - Jaws of the Beast",
     "value": "6/7/8/9/10% of target max HP (+120% AD) (+100% AP)",
     "source": "https://wiki.leagueoflegends.com/en-us/Template:Data_Warwick/Jaws_of_the_Beast",
     "verified": true}
  ],
  "derived": [],
  "heuristics": [],
  "statistics": [],
  "itemNotes": [],
  "decisionRules": [
    {"rule": "Tap Q to chase, hold Q to follow a dash.",
     "preconditions": ["target has a dash", "Q is off cooldown"],
     "triggers": ["ult_ready"],
     "source": "https://wiki.leagueoflegends.com/en-us/Template:Data_Warwick/Jaws_of_the_Beast",
     "verified": true}
  ],
  "counters": [],
  "abstentions": [
    {"when": "gank path claim", "missing": "ward coverage",
     "reason": "state it as unknown instead of claiming the path is unwarded"}
  ]
}
```

## Selection

`select_pack(champ, role=None, require_role=False)` normalizes names
case-insensitively and punctuation-insensitively (`Nunu & Willump`,
`Nunu and Willump`, `nunu`, `WW` all match a pack with the right alias) and
filters by normalized role (`JGL` -> `jungle`, `SUP` -> `support`,
`ADC` -> `bottom`). Unknown champion or a role the pack does not declare
returns `None`; the prompt then omits the champion-pack section instead of
inventing one.

For the inference path use `require_role=True` (CLI `--require-role`) so a
champion-only call cannot silently receive a role-specific pack. Pass the role
the UI selected so the badge and the injected block cannot disagree.

## CLI and JSON contract for AutoCoach

```
python ui/packs.py --prompt --champ "Nunu & Willump" --role jungle --max-chars 1800
python ui/packs.py --prompt --champ "Nunu & Willump" --role jungle --require-role --json
python ui/packs.py --select --champ "Warwick" --role jungle --json
python ui/packs.py --list [--json]
python ui/packs.py --validate [--json]
```

`--prompt` without `--json` prints the block text (exit 0) or nothing (exit 1).
`--prompt --json` prints one object with a stable shape:

```json
{
  "schema": "riftsense.pack.cli.v1",
  "ok": true,
  "champ": "Nunu & Willump",
  "role": "jungle",
  "pack": {"id": "nunuandwillump", "schema": "riftsense.pack.v2",
           "file": "nunu.json", "champion": "Nunu & Willump",
           "roles": ["jungle"], "patch": "26.19", "retrieved": "2026-09-23",
           "violations": 0},
  "text": "=== CHAMPION PACK (versioned) ===\n...",
  "manifest": {"schema": "riftsense.pack.manifest.v1", "id": "nunuandwillump",
               "version": "26.19", "bytes": 1678, "sha256": "f47bea2a01c5e471",
               "max_chars": 1800, "truncated": true, "state_used": false,
               "sections": [{"id": "header", "bytes": 94, "entries": 1,
                             "entries_total": 1, "truncated": false,
                             "budget": null, "dropped": false}, ...],
               "dropped": [], "violations": []}
}
```

A missing selection prints `{"ok": false, "status": "no_pack", ...}` and exits 1.
Publish `pack.id`, `pack.patch`, `manifest.sha256`, and the manifest alongside
the result so callers can display/transport exactly what was injected.

`--state-file <path>` or `--state-json '<json>'` passes observed state used to
prefer relevant rules. The accepted shape is a flat object, for example:

```json
{"clock": "12:34", "level": 9, "gold": 1200, "items": ["Liandry's Torment"],
 "enemy_items": ["Heartsteel"], "death_count": 1, "smite_stage": "unleashed",
 "q_rank": 5, "dragon_slayer_stacks": 1,
 "enemy_jungler_position": "top", "vision": "pit warded", "ult_ready": true,
 "events": ["dragon"]}
```

## Contribution rules

1. **Patch-scoped.** Put the patch you verified against in `patch` and the
   date you read it in `retrieved`. Packs are not re-validated at runtime; if
   a patch changes a number, either update it or flip `verified` to `false`.
2. **Every number needs a source.** Use a stable URL (wiki data template,
   Riot patch notes, Riot Data Dragon, official guide) and cite it on the
   same object as the number. Put arithmetic you performed in `derived`.
3. **Preconditions are mandatory** for any `decisionRule` that recommends an
   action. If the rule depends on a field the live client does not expose
   (positions, vision, enemy cooldowns), write the precondition anyway and
   have the agent label it unknown.
4. **No unsourced statistics.** Raw win-rate/pick-rate comparisons are
   `statistics` with `sample`, patch, source and the selection-bias caveat;
   they are never verified and never causal.
5. **Mark uncertainty, don't delete it.** Derived arithmetic, third-party
   practice notes, and open questions belong in the pack with the correct
   category - that is what keeps the coach honest.
6. **Abstain instead of guessing.** Add an `abstentions` entry for any call
   the pack cannot support without unobserved state; the agent then outputs
   `ABSTAIN: <missing field>` and the reason is recorded for the ledger.
7. **Keep it short.** The prompt block is capped (1800 chars by default) and
   every section gets a slice; lead each category with its most important
   entry.
8. **ASCII only.** The pack is passed through Windows PowerShell into the
   prompt; avoid em dashes, smart quotes and non-ASCII symbols.
9. **No execution.** Packs must stay declarative JSON. Never ask the loader
   to run code, fetch URLs at runtime, or grant the agent tools.

## Tests

`ui/test_packs.py` covers selection (including role and `--require-role`),
manifest content and byte accounting, section budget coverage, required-field
enforcement, verified-category separation, abstentions, and backward-compatible
`pack_prompt()`. Run it from the repository root:

```
python -m unittest discover -s ui -p 'test_*.py'
```
