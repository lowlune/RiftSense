# Champion/role packs

Versioned, human-reviewed coaching packs that are injected into the live
AutoCoach prompt (and served read-only by the UI). A pack is data, not code:
`ui/packs.py` loads it, `ui/server.py` exposes it, `AutoCoach.ps1` appends
`pack_prompt()` output to the inference prompt, and the dashboard shows the
active pack badge.

Live path today:

```
knowledge/packs/<champion>.json
  -> ui/packs.py select_pack(champ, role) / pack_prompt(pack, max_chars)
  -> AutoCoach.ps1 Start-CoachRun -> === CHAMPION PACK (versioned) ===
  -> opencode agent (agent/lol-coach.md)
```

`pack_prompt()` caps the block at 1800 characters by default, so packs must
lead with the rules that matter most. `knowledge/nunu-modern.md` remains the
long-form reference dossier; packs are the curated runtime subset.

## Schema

Every pack is a single JSON object. `schema` is `"riftsense.pack.v1"`.

| Field | Required | Type | Meaning |
|---|---|---|---|
| `schema` | yes | string | Must be `"riftsense.pack.v1"`; unknown schemas are skipped. |
| `champion` | yes | string | Canonical champion name, e.g. `"Nunu & Willump"`. |
| `aliases` | no | string[] | Extra names accepted by `select_pack()` (`"Nunu"`, `"WW"`). |
| `roles` | yes | string[] | `top`/`jungle`/`middle`/`bottom`/`support`. Empty matches any role. |
| `patch` | yes | string | Patch the numbers were checked against, e.g. `"26.19"`. |
| `retrieved` | yes | string | ISO date the values were read, e.g. `"2026-09-23"`. |
| `sources` | no | string[] | Top-level source list (each entry also carries its own `source`). |
| `mechanics` | yes | object[] | `{name, value, source, verified}`. |
| `itemNotes` | yes | object[] | `{name, note, source, verified}` - item stats, costs, interactions. |
| `decisionRules` | yes | object[] | `{rule, preconditions, source, verified}`; `preconditions` is a non-empty string[] for any rule that tells the player to do something. |
| `counters` | yes | object[] | `{name, note, source, verified}` - enemy item/mechanic counterplay. |

`verified` is a strict boolean:

- `true` - the value is a quoted, patch-scoped number from the cited source
  that `AutoCoach` may treat as a fact.
- `false` - unvalidated, derived, third-party or statistical. The renderer
  marks it `[UNVERIFIED - do not quote as fact]`; it may be shown but never
  used as a secure/buy number.

Example (minimum valid pack):

```json
{
  "schema": "riftsense.pack.v1",
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
  "itemNotes": [],
  "decisionRules": [
    {"rule": "Tap Q to chase, hold Q to follow a dash.",
     "preconditions": ["target has a dash", "Q is off cooldown"],
     "source": "https://wiki.leagueoflegends.com/en-us/Template:Data_Warwick/Jaws_of_the_Beast",
     "verified": true}
  ],
  "counters": []
}
```

## Contribution rules

1. **Patch-scoped.** Put the patch you verified against in `patch` and the
   date you read it in `retrieved`. Packs are not re-validated at runtime; if
   a patch changes a number, either update it or flip `verified` to `false`.
2. **Every number needs a source.** Use a stable URL (wiki data template,
   Riot patch notes, Riot Data Dragon, official guide) and cite it on the
   same object as the number. No sourced-from-memory values.
3. **Preconditions are mandatory** for any `decisionRule` that recommends an
   action. If the rule depends on a field the live client does not expose
   (positions, vision, enemy cooldowns), write the precondition anyway and
   have the agent label it unknown.
4. **No unsourced statistics.** Raw win-rate/pick-rate comparisons must carry
   the sample size, patch, source and the selection-bias caveat, and must be
   `verified: false`. Never present them as causal.
5. **Mark uncertainty, don't delete it.** Derived arithmetic, third-party
   practice notes, and open questions (e.g. whether Dragon Slayer reduction
   applies to true damage) belong in the pack with `verified: false` and a
   "do not use as a number" note - that is what keeps the coach honest.
6. **Keep it short.** The prompt block is capped (1800 chars by default).
   Lead with mechanics, rules and counters; move essays back to
   `knowledge/nunu-modern.md`.
7. **ASCII only.** The pack is passed through Windows PowerShell into the
   prompt; avoid em dashes, smart quotes and non-ASCII symbols.
8. **No execution.** Packs must stay declarative JSON. Never ask the loader
   to run code, fetch URLs at runtime, or grant the agent tools.

## Selection

`select_pack(champ, role=None)` normalizes names case-insensitively and
punctuation-insensitively (`Nunu & Willump`, `Nunu and Willump`, `nunu`,
`WW` all match a pack with the right alias) and filters by normalized role
(`JGL` -> `jungle`, `SUP` -> `support`, `ADC` -> `bottom`). Unknown champion
or a role the pack does not declare returns `None`; the prompt then omits the
champion-pack section instead of inventing one.

CLI used by `AutoCoach.ps1`:

```
python ui/packs.py --prompt --champ "Nunu & Willump" --max-chars 1800
python ui/packs.py --list
```

## Tests

`ui/test_packs.py` covers exact/normalized selection, missing champion,
role filtering, prompt size cap and unverified-flag preservation. Run it from
the repository root:

```
python -m unittest discover -s ui -p 'test_*.py'
```
