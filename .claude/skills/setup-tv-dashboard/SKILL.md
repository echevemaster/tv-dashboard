---
name: setup-tv-dashboard
description: "Trigger: set up tv-dashboard, configure TVs, first run, add a new TV, pair a TV, create scenes. Auto-configure tvs.json, pairing and scenes for LG webOS TVs."
license: MIT
metadata:
  author: "echevemaster"
  version: "1.0"
---

## Activation Contract

Load when the user wants to configure this repo for their own TVs: first-time setup, adding or re-pairing a TV, or generating scenes.

## Hard Rules

- Run every command from the repo root as `uv run app.py <flag>`. Discovery and pairing need LAN access, so run them outside any network sandbox.
- Never print, commit, or copy `keys.json` or `config.json`. Never write real IPs, MACs, or models into `app.py`, `README.md`, or `*.example.json`.
- Pairing needs a human at the TV. Tell the user to watch the screen BEFORE running `--pair`. Pair one TV at a time.
- Never overwrite or remove existing `tvs.json` or `scenes.json` entries without asking. Merge new entries.
- Do not run scenes, `power_on`, or `power_off` during setup.
- Use only `app_id` and `input_id` values returned by `--inspect`. Never guess them.

## Decision Gates

| Situation | Action |
|---|---|
| No `tvs.json` | Run the full setup (steps 1–8) |
| `tvs.json` exists and the user adds a TV | Discover, add only hosts not already present, pair only the new TVs |
| `--discover` returns `[]` | Ask the user to turn the TVs on and use the same network, retry once, then offer manual entry based on `tvs.example.json` |
| `--pair` fails with a timeout | Ask the user to turn the TV on and stand by the screen, retry once |
| `--pair` fails with `blacklisted certificate` | Stop and report: the TV firmware rejects the client; check the pinned `aiowebostv` version |

## Execution Steps

1. Check that `uv --version` works. If it doesn't, stop and point the user to the uv install docs.
2. Run `uv run app.py --discover` and show the TVs found (host, name, model).
3. Ask the user for a short id (`living`, `bedroom`) and a display name for each TV. Write `tvs.json` as `{id: {name, host, macs: []}}`.
4. Recommend a DHCP reservation in the router for each host.
5. For each TV, warn the user, then run `uv run app.py --pair <id>`. It saves the key and fills `macs`.
6. If the user wants scenes, run `uv run app.py --inspect <id>`, propose scenes using the step schema in the README, and write `scenes.json` after the user confirms.
7. Remind the user to enable *Turn on via Wi-Fi* / *Mobile TV On* on each TV for power-on.
8. Verify: start `uv run app.py` in the background, `GET http://127.0.0.1:8765/api/tvs`, confirm every TV is `online`, then stop the server.

## Output Contract

Return:
- A table with each TV's id, host, whether it paired, and the number of MACs recorded.
- The scenes created (name and steps summary), or "none".
- Pending manual steps (Wake-on-LAN setting, DHCP reservation).
- How to run it: `uv run app.py`, or `uv run app.py --lan` for phone access (PIN in `config.json`).

## References

- `../../../README.md`: config file formats, scene step schema, security model.
