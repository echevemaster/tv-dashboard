# TV Dashboard

A small local web dashboard to control LG webOS TVs on your home network: see what is playing, launch apps, control playback and volume, use a virtual remote, switch inputs, send on-screen messages, power TVs on and off, and run one-click scenes.

It runs on your own machine and talks to the TVs directly over the LAN. Nothing goes through the cloud.

## Requirements

- One or more LG webOS TVs on the same network
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 and the dependencies automatically)

## Setup

> **Using Claude Code?** Ask it to "set up tv-dashboard". The bundled `setup-tv-dashboard` skill finds your TVs, writes `tvs.json`, pairs each TV and can generate scenes.

1. **Find your TVs.** With the TVs on, run:

   ```bash
   uv run app.py --discover
   ```

2. **Describe your TVs.** Copy the example and edit it using the hosts you found:

   ```bash
   cp tvs.example.json tvs.json
   ```

   Each entry has an id (used in scenes), a display `name`, the TV's `host` IP, and its `macs`. Leave `macs` empty: pairing fills them in.

   Give each TV a fixed IP (a DHCP reservation in your router) so the `host` doesn't change.

3. **Pair each TV once.** Run the command below and accept the prompt on the TV screen:

   ```bash
   uv run app.py --pair living
   ```

   The pairing key is stored in `keys.json`, and the TV's wired and Wi-Fi MACs are saved to `tvs.json`.

4. **(Optional) Create scenes.** List a TV's app ids and input ids, then write `scenes.json`, using `scenes.example.json` as a starting point:

   ```bash
   uv run app.py --inspect living
   cp scenes.example.json scenes.json
   ```

5. **Run it:**

   ```bash
   uv run app.py          # only this machine: http://127.0.0.1:8765
   uv run app.py --lan    # also from your phone: http://<this-machine-ip>:8765
   ```

## Powering TVs on

Power on uses Wake-on-LAN. Enable it on each TV under *Settings → General → Devices → Turn on via Wi-Fi* (called *Mobile TV On* on some models). Without it the TV ignores the wake signal.

Magic packets go to `255.255.255.255` and to the subnet broadcast, which is guessed as a `/24`. If your network isn't a `/24`, set it in `config.json`:

```json
{ "pin": "<your random PIN>", "broadcast": "10.0.255.255" }
```

## Scenes

`scenes.json` is a list of scenes. Each scene runs its steps in order and stops at the first failure. A failed `power_off` is skipped, so turning off a TV that is already off doesn't stop the scene.

| Step | Fields | Description |
|---|---|---|
| `power_on` | `tv` | Send the Wake-on-LAN signal |
| `wait_online` | `tv`, `timeout` (s, default 45) | Wait until the TV accepts connections |
| `launch` | `tv`, `app_id` | Open an app (e.g. `netflix`, `youtube.leanback.v4`) |
| `input` | `tv`, `input_id` | Switch input (e.g. `HDMI_1`) |
| `set_volume` | `tv`, `value` | Set the volume |
| `toast` | `tv`, `message` | Show an on-screen message |
| `power_off` | `tv` | Turn the TV off |

App ids and input ids are listed by `uv run app.py --inspect <tv_id>`. Changes to `scenes.json` apply without a restart.

## Security

- By default the server only listens on `127.0.0.1`.
- With `--lan`, any client that is not this machine has to enter a PIN (6 digits, generated on the first run and stored in `config.json`; you can set a longer one). Each client gets 5 attempts every 5 minutes, and all clients together get 20. Sessions last 30 days and are lost on restart.
- Every state-changing request needs a custom header. Cross-site pages can't send it, so a malicious website open in your browser can't control your TVs.
- Requests with an unexpected `Host` header are rejected, which blocks DNS rebinding. Responses forbid framing (clickjacking) and use a strict Content-Security-Policy.
- **Don't put the dashboard behind a reverse proxy or tunnel.** Requests from this machine skip the PIN. Requests carrying proxy headers (`X-Forwarded-For`, `Forwarded`…) lose that exemption, but a proxy that strips them would expose the dashboard without a PIN.
- `keys.json`, `config.json`, `tvs.json` and `scenes.json` are git-ignored and written with owner-only permissions (`0600`). Don't share `keys.json`: anyone who has it can control your TVs.
- The LAN is trusted for transport: the dashboard uses plain HTTP, and the TVs use self-signed certificates.

## Known limitations

- Some newer LG firmware rejects older client certificates (`403 blacklisted certificate`). This project uses [aiowebostv](https://github.com/home-assistant-libs/aiowebostv), which is maintained by Home Assistant and kept up to date.
- The webOS API doesn't allow changing the region or privacy settings, or installing apps.

## License

[MIT](LICENSE)
