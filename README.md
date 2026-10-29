# Furbo Treat Launcher

Toss treats from a Furbo camera with Home Assistant: a button, an action for
your own automations, and a built-in daily treat schedule with fixed or random
timing, a minimum gap between treats, and chances for bonus double and triple
launches.

> Unofficial. Furbo has no public API; this uses the same private protocol as
> the Furbo app, so a Furbo app or firmware update could break it.

## How it works

```
Home Assistant  ──HTTP──▶  Furbo Treat Bridge add-on  ──P2P──▶  Furbo camera
 (integration)              (Furbo sign-in, treat API)
```

Furbo's cloud "toss treat" call answers success without tossing anything, so a
toss has to go over the camera's encrypted peer-to-peer link. That link needs a
closed-source library that cannot load inside Home Assistant itself, so it runs
in a small add-on next to it.

## Requirements

- Home Assistant OS or Supervised (for the add-on), 2025.4 or newer
- A 64-bit machine (amd64 or aarch64; a 32-bit Raspberry Pi OS will not work)
- [HACS](https://hacs.xyz)
- Your Furbo account email and password

Built for the **Furbo 360° Dog Camera** (FB0030), the model where tossing over
this protocol has been verified. Other 360° and Mini 360° models use the same
commands but are untested.

## Install

### 1. The bridge add-on

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/archieprichard/FurboTreatLauncher`
2. Install **Furbo Treat Bridge** (Home Assistant builds it on your machine;
   give it a few minutes).
3. In its **Configuration**, enter your Furbo **email** and **password**. Leave
   **api_token** blank to have one generated, or set any long random string.
4. **Start** it and open the **Log**. You'll see
   `Furbo emailed a verification code`, and the generated API token if you left
   it blank.

### 2. The integration

1. **HACS → ⋮ → Custom repositories**, add
   `https://github.com/archieprichard/FurboTreatLauncher` as an **Integration**.
2. Install **Furbo Treat Launcher** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Furbo Treat Launcher**.
   Keep host `127.0.0.1` and port `8792`, paste the API token, then enter the
   code Furbo emailed you.

You enter a code once. The bridge keeps the proof Furbo hands back and signs
itself in again when Furbo's daily session expires. If Furbo ever does ask for
a new code, Home Assistant asks you to re-authenticate the integration under
**Settings → Devices & services**, where you enter it.

## What you get

On the camera's device page:

| Entity | What it does |
| --- | --- |
| **Toss treat** | Tosses one treat now |
| **Play treat sound** | Plays the toss sound without a treat |
| **Treats today** | Every treat the bridge tossed today, from any source |
| **Treat schedule** | Turns the daily schedule on or off |
| **Treats per day** | How many scheduled treats a day |
| **Schedule window start / end** | The daily window, e.g. 08:00–18:00 |
| **Schedule mode** | *Fixed*: evenly spread. *Random*: new times every day |
| **Minimum time between treats** | Honored strictly; if the count doesn't fit, it gives as many as fit |
| **Double launch** / **Double launch chance** | After a scheduled treat, a chance of a second one 30 s later |
| **Triple launch** / **Triple launch chance** | After a double launch, a chance of a third one 30 s after that |
| **Next scheduled treat** | When the next one goes; attributes list today's plan and any problem |

### How the schedule behaves

- **Fixed** with 3 treats from 08:00 to 18:00 gives 08:00, 13:00 and 18:00.
- **Random** picks fresh times each day, at least the minimum gap apart.
- Changing a setting mid-day re-plans only what's left, counting treats
  already given.
- A restart keeps the day's plan; a random day isn't re-rolled.
- A treat that was due while Home Assistant was down is **skipped**, never
  made up later.
- A failed treat (camera briefly offline) is retried once, 2 minutes later.
- Double and triple launches are bonuses: they don't use up treats per day,
  but they do count toward the bridge's daily limit.
- Overnight windows (e.g. 22:00–06:00) aren't supported.

### Action

```yaml
action: furbo_treats.toss_treat
data:
  device_id: <your Furbo device>
  count: 2   # 1–5, two seconds apart
```

### Event

Every scheduled, double and triple launch fires `furbo_treats_scheduled_treat`
with `success`, and `error` when it failed. To get a phone notification when a
scheduled treat fails:

```yaml
triggers:
  - trigger: event
    event_type: furbo_treats_scheduled_treat
    event_data:
      success: false
actions:
  - action: notify.notify
    data:
      message: "Furbo treat failed: {{ trigger.event.data.error }}"
```

## Safety limits

Set in the add-on's configuration; they apply to every treat, however it was
triggered:

- **daily_limit** (default 20): the most treats per day. `0` turns it off.
- **min_interval** (default 5 s): the shortest time between treat requests.
  Keep it under 30 s if you use double or triple launches.

## Credits

The Furbo sign-in flow and the camera protocol details come from
[ha-furbo](https://github.com/willbeeching/ha-furbo), which reverse engineered
them and verified a treat toss on real hardware. This project is a
treat-focused rebuild; ha-furbo also does live video and camera settings.
