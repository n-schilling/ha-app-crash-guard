# Crash Guard

Defective RAM cells corrupt data in the page cache. Home Assistant then crashes when it imports a corrupted file, often right after a start, and a container restart does not help, because the corrupted copy stays in the cache. Crash Guard handles that:

1. **Catches crashes.** When the Home Assistant container exits with a signal (SIGSEGV, SIGBUS, SIGILL, SIGABRT, SIGFPE), it saves evidence, drops the page cache and starts Home Assistant again, at most `max_restarts_per_hour` times an hour.
2. **Checks the cache.** At `drop_times` it hashes all Home Assistant program files through the page cache and compares them with a reference built from the current image. Corrupted pages are located by physical address and logged to `badpages.log`, with a copy in `/share/crash_guard`.
3. **Locks defective RAM.** For each page in `bad_pages` it reserves the surrounding 2 MB block in the device tree, so Linux never uses it again, and restores that lock after Home Assistant OS updates.
4. **Counts every crash.** Crashes of any program on the host, logged by systemd-coredump, are counted since boot and within `crash_window_hours`, whatever their cause.
5. **Reports.** Entities show the state, and notifications go to `notify_service`.

## Read this first: what the app may do

Crash Guard needs far more rights than a usual app. Each one serves a feature above, but together they amount to **full control over the host**. Install it only if you need it, and read the code if you are unsure: everything it does is in this repository.

| Right | What it allows | What Crash Guard uses it for |
|---|---|---|
| Docker API (`docker_api`, protection mode off) | Everything Docker can do on the host, which is as much as root | Watching the Home Assistant container for crashes, reading the files of its image, starting short-lived helper containers |
| Privileged helper containers | Root on the host for the helper's single command | Dropping the page cache, reading the physical addresses of corrupted pages, reading and writing the boot partition for the RAM lock |
| Supervisor API as manager (`hassio_role: manager`) | Managing Home Assistant Core and reading other apps' state | Starting Home Assistant after a crash, waiting for running updates and backups, reading its own image |
| Home Assistant API (`homeassistant_api`) | Calling any action and setting any state in Home Assistant | Notifications through `notify_service`, the entities when no MQTT broker is available |
| Host journal (`journald`) | Reading all system logs of the host | Counting program crashes, saving kernel and coredump messages as evidence |
| Home Assistant configuration, read-only | Reading all files of the configuration, including `secrets.yaml` | Saving `home-assistant.log.fault` as evidence after a crash; only a regular file, never a symlink |
| Boot partition (only with `ram_lock` on) | Changing what the host loads at boot | Adding one `dtoverlay=` line and its overlay file to `config.txt`; nothing else is changed |

What limits the risk:

- The helper containers have no network, a read-only root file system and see only the paths they need; each runs one command and is removed after at most an hour.
- The app talks to nothing outside the host except the MQTT broker; it sends no data anywhere.
- Everything it writes stays in its private `/data` and in `/share/crash_guard`; values it reads back from `/share` are validated, because other apps can write there. Logs that may hold secrets reach `/share` only with `publish_evidence`.
- `config.txt` is only appended to, or replaced in one atomic step, and the RAM lock is off unless you turn it on. A broken boot configuration can still keep the host from starting: keep a way to edit the boot partition from another computer.

Home Assistant shows the app with security rating 1 because of these rights; that is expected.

## Turn off protection mode

**Crash Guard does not run in protection mode.** Home Assistant installs every app with protection mode on, and in that mode the Supervisor withholds the Docker API that Crash Guard needs to watch the Home Assistant container and to start its helper containers.

After installing, open Crash Guard under **Settings → Apps**, turn off **Protection mode** and start the app. With protection mode still on, the app stops right away, logs

> Please deactivate the protection mode for this app: …

and shows the same message as a notification in Home Assistant. The notification goes away by itself once the app has started.

## Requirements and limits

- Home Assistant OS on aarch64 or amd64. The RAM lock needs a Raspberry Pi (it uses `config.txt` overlays; tested on a Raspberry Pi 5); everything else works on any Home Assistant OS host
- An MQTT broker known to the Supervisor, e.g. the Mosquitto broker app, for full entities; without one the app sets plain states (see below)
- Protection mode turned off (see above): the app needs Docker API access and runs short-lived privileged helper containers: to drop the page cache, to read physical page addresses, and to read and write the boot partition for the RAM lock. Use it only when you need it.
- A RAM lock is a workaround. The fix for defective RAM is replacing the board.

## Options

| Option | Default | Meaning |
|---|---|---|
| `drop_times` | `04:30`, `16:30` | Times of the scheduled cache check |
| `drop_on_schedule` | `false` | Also drop the cache at those times; a drop can move a defective page to freshly started programs, so it is off by default |
| `verify_on_drop` | `true` | With `drop_on_schedule`, hash before and after the drop |
| `check_interval` | `10` | Seconds between two crash checks |
| `max_restarts_per_hour` | `3` | Crashes handled per hour at most |
| `notify_service` | | Notification service such as `notify.mobile_app_phone`; empty sends none |
| `bad_pages` | | Page frame numbers of defective pages, e.g. `0x1cc447`, taken from `badpages.log` |
| `ram_lock` | `false` | Write the lock into the boot configuration and restore it; off only reports its state |
| `entity_prefix` | `crash_guard` | Prefix of the entity IDs |
| `name_prefix` | `Crash Guard` | Name of the device; the entity names start with it |
| `crash_window_hours` | `24` | Hours the program crash window looks back |
| `publish_evidence` | `false` | Also copy the logs of a crash (Home Assistant container log, `home-assistant.log.fault`, kernel and coredump messages) to `/share/crash_guard`. Other apps, e.g. Samba, can read `/share`, and logs may hold secrets; off keeps them in the app's private data |
| `discovery_prefix` | `homeassistant` | MQTT discovery prefix |
| `log_level` | `info` | How much the app writes to its log: `info`, `warning` or `error` |

## Entities

| Entity | Meaning |
|---|---|
| `binary_sensor.<prefix>_memory_lock` | On while the RAM lock is in effect; attributes show the boot slot, overlays, config line and pages outside the lock. Only with `bad_pages` |
| `sensor.<prefix>_cache_corrupted` | Corrupted files found at the last cache check |
| `sensor.<prefix>_crashes_handled` | Home Assistant crashes handled within the last 24 hours |
| `sensor.<prefix>_crashes_boot` | Crashes of any host program since the host booted |
| `sensor.<prefix>_crashes_window` | Crashes of any host program within `crash_window_hours`; attributes list the last ten with process and signal |

With an MQTT broker the entities belong to one device named after `name_prefix`, keep their history and settings, and turn unavailable when the app stops. Without a broker they are plain states set through the Home Assistant API: Home Assistant forgets them on a restart, and the app sends them again as soon as Home Assistant is back.

## Example automation

Notify when any program on the host crashed, with its name:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.crash_guard_crashes_window
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.state | int(0) > trigger.from_state.state | int(0) }}"
actions:
  - action: notify.notify
    data:
      message: >-
        {% set c = state_attr('sensor.crash_guard_crashes_window', 'last')[0] %}
        {{ c.process }} crashed with {{ c.signal }} at {{ c.time }}.
```

## Troubleshooting

- **The app stops right after the start.** Protection mode is on; see above.
- **Program crashes stay at 0.** Only crashes that systemd-coredump logs are counted, which covers Home Assistant OS.
- **The memory lock entity is missing.** It only exists while `bad_pages` holds at least one page.
- **The memory lock reports an error on a PC.** The RAM lock only works on a Raspberry Pi; leave `ram_lock` off.

## Setting up the RAM lock

1. Let the app run until `badpages.log` (in `/share/crash_guard`) shows a defective page, ideally the same page more than once.
2. Add its page frame number to `bad_pages` and turn on `ram_lock`.
3. The app writes a device tree overlay (`badram-<pages>.dtbo`) to both boot slots and a `dtoverlay=` line to `config.txt`. The lock takes effect with the next host reboot; the memory lock entity then turns on.
4. Home Assistant OS updates replace the boot slot; the app restores the overlay within an hour, and the next reboot activates it again.

To remove the lock, turn off `ram_lock`, delete the `dtoverlay=badram-…` line from `config.txt` and reboot.

## Known limitations

- The security rating is 1: the app needs the Docker API and starts privileged helper containers. That is the price of dropping the page cache and reading physical addresses.
- The cache check covers the files of the Home Assistant image only, not other apps.
- Home Assistant OS only: the app relies on its paths for Docker layers and the boot partition.

## Removing the app

With MQTT, the entities stay in Home Assistant after the app is removed. Delete the device under **Settings → Devices & services → MQTT**. A RAM lock stays in `config.txt`; remove it as described above.

## Files

The app keeps its logs, state and reference hashes in its private data folder. `/share/crash_guard` holds what you can read:

- `events.log`, `drops.log`, `badpages.log`: copies of the logs, refreshed hourly and after every check or crash
- one folder per case (`<time>-check`, `<time>-crash`, …) with the hash lists, the differing files and the hashes that differed

The logs of a crash (container log, `home-assistant.log.fault`, kernel and coredump messages) go into the case folder only with `publish_evidence`. Without it they stay in the app's private data under `crash_guard/cases/<case>`, the last 20 cases; you can read them through a backup of the app, or turn on `publish_evidence` while you need them.

`/share` is writable by other apps, so the app never acts on anything it finds there: the copies are written by a helper container that can write nowhere but `/share`.

## Support

Questions, bugs and ideas: open an issue at https://github.com/n-schilling/ha-app-crash-guard/issues. Please add the app version and the app log.
