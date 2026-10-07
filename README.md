# Crash Guard app for Home Assistant

[![Release](https://img.shields.io/github/v/release/n-schilling/ha-app-crash-guard)](https://github.com/n-schilling/ha-app-crash-guard/releases)
[![CI](https://github.com/n-schilling/ha-app-crash-guard/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/n-schilling/ha-app-crash-guard/actions/workflows/ci.yaml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Home Assistant app](https://img.shields.io/badge/Home%20Assistant-app-41BDF5?logo=homeassistant&logoColor=white)
![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64](https://img.shields.io/badge/amd64-yes-green.svg)

![Crash Guard](crash_guard/logo.png)

A Home Assistant app (formerly add-on) for everything about crashes on a Home Assistant OS host:

- counts crashes of every host program, whatever their cause, as sensors;
- catches crashes of Home Assistant itself, saves evidence and brings it back up;
- checks the page cache for corrupted files, the typical trace of defective RAM;
- on a Raspberry Pi, locks defective RAM pages away from Linux through the device tree, so the host keeps running until the board is replaced.

> [!WARNING]
> Crash Guard needs the Docker API, privileged helper containers, the Supervisor API as manager, the host journal and, for the RAM lock, write access to the boot configuration. Together that is full control over the host. Read [what the app may do](crash_guard/DOCS.md#read-this-first-what-the-app-may-do) before installing it.

## Installation

1. Add the repository to your Home Assistant instance:

   [![Add the repository to My Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fn-schilling%2Fha-app-crash-guard)

   Or add it manually: **Settings → Apps → Install app → ⋮ → Repositories** (before Home Assistant 2026.2: **Settings → Add-ons → Add-on store**), then paste:

   ```text
   https://github.com/n-schilling/ha-app-crash-guard
   ```

2. Open the app from the app store.
3. Install **Crash Guard**, turn off its **Protection mode**, review the options and start it. Crash Guard needs the Docker API, which protection mode withholds; with it on, the app stops with a message saying so.

See [the documentation](crash_guard/DOCS.md) for how it works, what it touches, its entities and troubleshooting. Changes are listed in the [changelog](crash_guard/CHANGELOG.md).

## Support

Questions, bugs and ideas: [open an issue](https://github.com/n-schilling/ha-app-crash-guard/issues/new/choose) in this repository. Security problems: see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
