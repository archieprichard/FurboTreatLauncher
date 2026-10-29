# Furbo Treat Bridge

Signs in to your Furbo account and sends treat commands to your Furbo camera
over its encrypted peer-to-peer link, for the **Furbo Treat Launcher**
integration.

## Setup

1. Enter your Furbo **email** and **password**.
2. Leave **api_token** blank to have one generated (it's printed in the log),
   or set your own long random string. The integration needs the same value.
3. Start the add-on. Furbo emails you a verification code; enter it when you
   add the Furbo Treat Launcher integration.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `email`, `password` | | Your Furbo account. Stored only in this add-on. |
| `api_token` | generated | Shared secret for the integration. |
| `min_interval` | 5 | Shortest time between treat requests, in seconds. Keep under 30 for double and triple launches. |
| `daily_limit` | 20 | Most treats per day, from any source. `0` turns the limit off. |
| `reset_session` | off | Turn on, restart, then turn off again: forgets the Furbo sign-in (after a password change or switching accounts). |
| `log_level` | info | `debug` for troubleshooting. |

## Notes

- The add-on uses the host network: the camera link hole-punches over UDP.
  The API listens on port **8792**.
- When Home Assistant builds this add-on, it downloads ThroughTek's TUTK
  library from the docker-wyze-bridge project, pinned to one exact file and
  checked against a SHA-256. That library is closed source and is not part of
  this repository.
- Your Furbo password is used only to sign in to Furbo and never leaves this
  add-on. The stored session lives in the add-on's private data folder.
