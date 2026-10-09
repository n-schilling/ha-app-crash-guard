# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.3] - 2026-10-09

### Changed

- The log no longer starts with s6-overlay's warning that user bundles in `s6-rc.d` are deprecated; the service is now registered in `user-bundles.d`. The Debian base image ships s6-overlay 3.2.3.2 since the last build

## [1.5.2] - 2026-10-09

### Changed

- s6-overlay logs only warnings and errors, so the start and stop of the app no longer fill the log with `s6-rc: info` lines

## [1.5.1] - 2026-10-07

### Fixed

- The lock tool also knows the German marker line that versions before 1.1 wrote into `config.txt`: taking the lock out removes it, and new lines go under it

## 1.5.0 - 2026-10-07

### Added

- The RAM lock is taken out again when `bad_pages` is emptied with `ram_lock` on: the app's own overlays leave both boot slots and its lines leave `config.txt`, atomically; a notification says so. With `ram_lock` off the boot configuration is never touched
- Option `cases_to_keep` (20): case folders kept in `/share/crash_guard` and in the private data

### Changed

- Before, case folders in `/share/crash_guard` were never removed

### Fixed

- When the bad pages change, the new `dtoverlay` line goes under the existing marker in `config.txt` instead of a second `[all]` block with a second marker

## 1.4.0 - 2026-10-07

### Added

- Option `publish_evidence`, off by default: the logs of a crash (Home Assistant container log, `home-assistant.log.fault`, kernel and coredump messages) reach `/share/crash_guard` only when it is on; otherwise they stay in the app's private data, the last 20 cases

### Changed

- Before, crash logs always went to `/share/crash_guard`; turn on `publish_evidence` to keep that

### Security

- The hash helper reads file names NUL-separated and drops names with control characters, spaces or backslashes; a directory name with a newline could otherwise have smuggled a file of another container's layer into the hash list and the evidence
- The hash helper copies only regular files of the listed layers; each directory on the way is opened without following symlinks (cg-copy.py), so a directory swapped for a symlink cannot lead outside
- A `home-assistant.log.fault` that is a symlink, e.g. to `secrets.yaml`, is no longer copied into the evidence
- Helper containers run the image ID of the app's own container instead of an image found by container name

## 1.3.2 - 2026-10-07

### Added

- Documentation: what the app may do, right by right, and what limits the risk; a warning in the README

## 1.3.1 - 2026-10-07

### Fixed

- Moving to device discovery stopped on the empty lines between old discovery messages that end in a newline

## 1.3.0 - 2026-10-07

### Added

- Option `log_level`: `info`, `warning` or `error`

### Changed

- Device based MQTT discovery: one retained message announces the device with all its entities. The entities announced one by one by 1.2.0 are handed over with their entity IDs and history, and their old topics are removed

## 1.2.0 - 2026-10-07

### Added

- Counts crashes of every host program from the journal (*Program crashes since boot*, *Program crashes (window)*), taken over from the NVMe & Crash Monitor app with their unique IDs, so existing entities carry on; option `crash_window_hours`
- Option `discovery_prefix`
- amd64 support; the RAM lock stays Raspberry Pi only
- Icon, logo, German translation of the options
- Documentation: example automation, troubleshooting, known limitations, removing the app

### Changed

- With an MQTT broker, all entities come through MQTT discovery: one device, history and settings kept, unavailable when the app stops; the states set through the API by earlier versions are removed. Without a broker the app keeps setting plain states

## 1.1.9 - 2026-10-06

### Added

- Documentation: protection mode must be turned off

### Fixed

- With protection mode on, the app stops with a clear message (log and notification in Home Assistant) instead of "Own image not found"; the notification is removed on the next successful start

## 1.1.8 - 2026-10-06

### Changed

- ShellCheck findings fixed (quoting, declarations, unused loop variable); no change in behaviour

## 1.1.7 - 2026-10-06

### Added

- Tests and CI

### Changed

- Lock layout and the page frame numbers from badpages.log moved into /usr/local/lib/crash-guard/lock.sh, shared with the tests
- cg-lock.py takes the boot and device tree paths from CG_BOOT and CG_DT for the tests (no change in behaviour)
- config.yaml no longer repeats the default boot option

## 1.1.6 - 2026-10-06

### Removed

- The app itself no longer maps /share

### Security

- No helper binds /share/crash_guard any more, because Docker would follow a symlink put there onto the host; one helper binds the share root, checks crash_guard with cd -P and pwd -P, and is the only one that writes to /share
- Hashing and page location work entirely in the app's private /data; no privileged helper sees /share
- Helper containers are stopped after one hour at most; cg-pfn.py opens files non-blocking

## 1.1.5 - 2026-10-06

### Fixed

- Helper containers got a multi-line command split into one argument per line, so the log copies, the evidence import and compressing or removing case directories (1.1.3, 1.1.4) silently did nothing; arguments are now passed one by one

## 1.1.4 - 2026-10-06

### Security

- Logs and state files (events.log, drops.log, badpages.log, notification markers) live in the app's private /data; /share gets copies, refreshed hourly and after every check or crash by a helper container that can write nowhere else
- Crash evidence is collected in /data and copied into the case directory by such a helper; the main container opens no file under /share any more
- cg-pfn.py opens the mismatch file and its log without following symlinks and accepts regular files only

## 1.1.3 - 2026-10-06

### Security

- Case directories are compressed and removed by a helper container that sees only /share, and the main container writes crash evidence from inside the directory it entered once; a path swapped for a symlink in between can no longer redirect either

## 1.1.2 - 2026-10-06

### Security

- No more import of reference hashes from /share; a reference planted there later could otherwise be taken over
- The hash helper sees the private /data read-only; only building a reference may write, and only to /data's tmp folder
- Case directories under /share are created fresh and never reused when the path exists; the app checks they are still no symlink before it compresses or removes anything in them

## 1.1.1 - 2026-10-06

### Security

- The layer list and reference hashes the privileged helpers act on moved from /share to the app's private /data
- Page frame numbers from badpages.log are validated before any arithmetic
- The hash helper only reads Docker layer directories, the page helper only opens regular files inside the Docker layers, without following symlinks

## 1.1.0 - 2026-10-06

### Added

- Defective pages as option `bad_pages`; the lock overlay is built from it at runtime with dtc instead of being part of the image
- Entity IDs and names from the options `entity_prefix` and `name_prefix`; entities are now `…_memory_lock`, `…_cache_corrupted` and `…_crashes_handled`
- Notifications optional
- Documentation

### Changed

- Own image found through the app's slug, so the app also works when installed from a repository
- `config.txt` changes only by appending, or through an atomic rename
- English texts throughout

## 1.0.6 - 2026-10-06

### Added

- First version in this repository

[1.5.1]: https://github.com/n-schilling/ha-app-crash-guard/releases/tag/v1.5.1
