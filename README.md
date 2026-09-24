# RiftSense

A local, real-time League of Legends coaching dashboard. It reads Riot's official Live Client Data API on your own machine, thinks about the game with an LLM, and renders a compact dashboard: coach readout, death reports, objective timers, gold, matchup bars and your build path.

## What it does

- **Live coach readout every 60 seconds** - threat model, buy-now / buy-next / trap with reasons, objective plan with timers, what to avoid, where to ward, and a single `DO NOW` call.
- **Instant death reports** - detected within ~5 seconds, shown as a pending card that fills in with WHY / NOW / NEXT / DO NOW.
- **Per-game cost meter** - exactly what the coaching has cost in the current game.
- **Matchup bars** - item-gold comparison against each direct lane opponent.
- **Build tracker** - per-champion plans rendered as item icons (owned / next / future), auto-detected from your live inventory.
- **Extras** - champ select helper (`Get-DraftData.ps1`) and profile/pool/match-history reader (`Get-MyProfile.ps1`).

## Architecture

- `AutoCoach.ps1` - PowerShell loop that polls the Live Client Data API, detects your deaths, and invokes an [opencode](https://opencode.ai) agent with the game state inline (single-shot inference, low reasoning variant for speed and cost).
- `agent/lol-coach.md` - the coach agent (system prompt, output format, verified timers/numbers).
- `Get-LiveGame.ps1` - compact game-state formatter (players, items, gold, timers, events).
- `ui/server.py` + `ui/index.html` - small stdlib-only Python server + dashboard (no build step, no npm).
- `build_intent.txt` - per-champion build plans (`PLAN[Warwick]: ...`).

Everything stays on your machine. No accounts, no telemetry.

## Requirements

- Windows, League of Legends (the game must be running for data)
- [opencode](https://opencode.ai) with a model provider configured (the agent defaults to `opencode-go/deepseek-v4.1-flash` - change it in `agent/lol-coach.md`)
- Python 3.10+ (standard library only)
- PowerShell 5.1+ (uses `curl.exe`, present on Windows 10+)

## Setup

1. Copy `agent/lol-coach.md` into your opencode config: `~/.config/opencode/agent/lol-coach.md` (on Windows: `C:\Users\<you>\.config\opencode\agent\lol-coach.md`)
2. Run `Start-AutoCoach.cmd` - starts the tick loop and death reports
3. Run `Start-Ui.cmd` - starts the dashboard at http://127.0.0.1:7777
4. Optional: edit `build_intent.txt` to set your own per-champion plans

Champion and item data are downloaded automatically from Data Dragon on first run.

## Compliance

Uses only Riot's official local interfaces: the Live Client Data API (`127.0.0.1:2999`) and the League client's local API. No game process memory is read, no gameplay is automated, and nothing beyond what Riot already exposes to local companion apps is used. Personal fan project - not endorsed by Riot Games. See Riot's Third-Party Applications policy.

## License

MIT - see `LICENSE`.
