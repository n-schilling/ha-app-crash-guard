#!/usr/bin/env python3
"""Manages the device tree lock for defective RAM areas on Raspberry Pi boards.

Runs in the privileged helper container (without the privilege Docker hides
/sys/firmware) with the host's /mnt/boot mounted at /mnt/boot.

cg-lock.py check   <overlay-name> <block-size> <block-address>...
cg-lock.py install <overlay-name> <block-size> <block-address>...
cg-lock.py render  <overlay-name> <block-size> <block-address>...
cg-lock.py remove

Sizes and addresses are hexadecimal. The overlay reserves each block under
/reserved-memory with no-map, so Linux never hands it out again. It is built
from the running device tree (cell sizes, SoC compatible) and compiled with dtc.

Prints exactly one JSON line:
  active        the lock is in effect in the running kernel (all nodes present)
  config        the dtoverlay line is in config.txt
  slot          slot the next boot loads from (os_prefix)
  slot_overlay  overlay file present and identical in the boot slot
  other_overlay the same for the other slot (None = slot missing)
  ready         config + slot_overlay: the lock takes effect on the next boot
  changed       install wrote something
render prints the overlay's path and hash instead. remove takes every lock
this tool wrote out of both slots and config.txt and prints:
  changed       something was removed
  config        a dtoverlay line of this tool is still in config.txt
  active        a locked block is still in effect (until the next boot)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

# CG_BOOT and CG_DT only for the tests, which fake both
BOOT = os.environ.get("CG_BOOT", "/mnt/boot")
CONFIG = os.path.join(BOOT, "config.txt")
DT = os.environ.get("CG_DT", "/sys/firmware/devicetree/base")
DT_RESERVED = os.path.join(DT, "reserved-memory")
WORK = "/tmp/crash-guard"
MARKER = "# Crash Guard: lock defective RAM areas (do not remove)"
# Overlays and config lines this tool owns; anything else is never touched
OWNED = re.compile(r"^badram-[0-9a-f-]+$")


def sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def cells(name: str) -> int:
    with open(os.path.join(DT_RESERVED, name), "rb") as f:
        return int.from_bytes(f.read(4), "big")


def soc_compatible() -> str:
    with open(os.path.join(DT, "compatible"), "rb") as f:
        return [c for c in f.read().decode().split("\0") if c][-1]


def split_cells(value: int, count: int) -> str:
    words = [(value >> (32 * i)) & 0xFFFFFFFF for i in reversed(range(count))]
    return " ".join(f"0x{w:x}" for w in words)


def render(name: str, size: int, blocks: list[int]) -> str:
    """Writes <name>.dts and <name>.dtbo to WORK and returns the .dtbo path."""
    ac, sc = cells("#address-cells"), cells("#size-cells")
    nodes = "".join(
        f"\t\t\tbadram@{addr:x} {{\n"
        f"\t\t\t\treg = <{split_cells(addr, ac)} {split_cells(size, sc)}>;\n"
        f"\t\t\t\tno-map;\n"
        f"\t\t\t}};\n"
        for addr in blocks
    )
    source = (
        "/dts-v1/;\n/plugin/;\n\n"
        f"/* Written by Crash Guard: defective RAM blocks, {size // 1024} KiB each */\n"
        "/ {\n"
        f'\tcompatible = "{soc_compatible()}";\n\n'
        "\tfragment@0 {\n"
        '\t\ttarget-path = "/reserved-memory";\n'
        "\t\t__overlay__ {\n"
        f"{nodes}"
        "\t\t};\n"
        "\t};\n"
        "};\n"
    )
    os.makedirs(WORK, exist_ok=True)
    dts, dtbo = os.path.join(WORK, f"{name}.dts"), os.path.join(WORK, f"{name}.dtbo")
    with open(dts, "w", encoding="ascii") as f:
        f.write(source)
    subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", dtbo, dts], check=True)
    return dtbo


def read_config() -> str:
    with open(CONFIG, encoding="ascii", errors="replace") as f:
        return f.read()


def boot_slot(cfg: str) -> str:
    m = re.search(r"^os_prefix=(\S+?)/?\s*$", cfg, re.M)
    return m.group(1) if m else ""


def has_line(cfg: str, name: str) -> bool:
    return re.search(rf"^dtoverlay={re.escape(name)}\s*$", cfg, re.M) is not None


def overlay_state(slot: str, name: str, want: str | None) -> bool | None:
    slot_dir = os.path.join(BOOT, slot)
    if not slot or not os.path.isdir(slot_dir):
        return None
    return sha(os.path.join(slot_dir, "overlays", f"{name}.dtbo")) == want


def status(name: str, want: str | None, blocks: list[int], changed: bool = False) -> dict:
    cfg = read_config()
    slot = boot_slot(cfg)
    other = {"slot-A": "slot-B", "slot-B": "slot-A"}.get(slot, "")
    st = {
        "active": all(os.path.isdir(os.path.join(DT_RESERVED, f"badram@{a:x}")) for a in blocks),
        "config": has_line(cfg, name),
        "slot": slot,
        "slot_overlay": bool(overlay_state(slot, name, want)),
        "other_overlay": overlay_state(other, name, want),
        "changed": changed,
    }
    st["ready"] = st["config"] and st["slot_overlay"]
    return st


def install(name: str, src: str, blocks: list[int]) -> dict:
    want = sha(src)
    changed = False
    # Both slots: the active one for the next boot, the other one in case an
    # update rolls back or the slot is still valid
    for slot in ("slot-A", "slot-B"):
        odir = os.path.join(BOOT, slot, "overlays")
        if not os.path.isdir(odir):
            continue
        if sha(os.path.join(odir, f"{name}.dtbo")) != want:
            shutil.copyfile(src, os.path.join(odir, f"{name}.dtbo"))
            changed = True
        # Overlays of an earlier set of blocks
        for old in os.listdir(odir):
            stem = old.removesuffix(".dtbo")
            if old.endswith(".dtbo") and OWNED.match(stem) and stem != name:
                os.remove(os.path.join(odir, old))
                changed = True
    cfg = read_config()
    lines = cfg.splitlines(keepends=True)
    kept = [
        line for line in lines
        if not (m := re.match(r"^dtoverlay=(\S+)\s*$", line)) or not OWNED.match(m.group(1)) or m.group(1) == name
    ]
    if len(kept) != len(lines):
        # Lines of an earlier set of blocks: rewritten atomically, so a crash
        # never leaves a half-written config
        cfg = "".join(kept)
        write_config(cfg)
        changed = True
    if not has_line(cfg, name):
        lines = cfg.splitlines(keepends=True)
        marker = next((i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER), None)
        if marker is not None:
            # A lock written before: the new line goes right under its marker
            lines.insert(marker + 1, f"dtoverlay={name}\n")
            cfg = "".join(lines)
            write_config(cfg)
        else:
            # HAOS ends config.txt with "[all]"; there the line applies to every board
            tail = "" if cfg.endswith("\n") else "\n"
            with open(CONFIG, "a", encoding="ascii") as f:
                f.write(f"{tail}[all]\n{MARKER}\ndtoverlay={name}\n")
        changed = True
    if changed:
        os.sync()
    return status(name, want, blocks, changed)


def write_config(cfg: str) -> None:
    """Rewrites config.txt through a temporary file and an atomic rename."""
    tmp = f"{CONFIG}.crash-guard"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(cfg)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG)


def remove() -> dict:
    """Takes out every overlay and config line this tool wrote, nothing else."""
    changed = False
    for slot in ("slot-A", "slot-B"):
        odir = os.path.join(BOOT, slot, "overlays")
        if not os.path.isdir(odir):
            continue
        for old in os.listdir(odir):
            if old.endswith(".dtbo") and OWNED.match(old.removesuffix(".dtbo")):
                os.remove(os.path.join(odir, old))
                changed = True
    lines = read_config().splitlines(keepends=True)
    kept: list[str] = []
    for line in lines:
        m = re.match(r"^dtoverlay=(\S+)\s*$", line)
        if (m and OWNED.match(m.group(1))) or line.rstrip("\n") == MARKER:
            # The "[all]" this tool wrote right before its marker goes too
            if line.rstrip("\n") == MARKER and kept and kept[-1].strip() == "[all]":
                kept.pop()
            continue
        kept.append(line)
    if len(kept) != len(lines):
        write_config("".join(kept))
        changed = True
    if changed:
        os.sync()
    cfg = read_config()
    active = os.path.isdir(DT_RESERVED) and any(n.startswith("badram@") for n in os.listdir(DT_RESERVED))
    return {"changed": changed, "active": active,
            "config": any(OWNED.match(m) for m in re.findall(r"^dtoverlay=(\S+)\s*$", cfg, re.M))}


def main() -> None:
    if sys.argv[1:] == ["remove"]:
        try:
            st = remove()
        except OSError as e:
            st = {"error": str(e)}
        print(json.dumps(st))
        return
    mode, name, size_hex, *addresses = sys.argv[1:]
    size = int(size_hex, 16)
    blocks = sorted(int(a, 16) for a in addresses)
    try:
        if not OWNED.match(name) or not blocks:
            raise ValueError(f"invalid lock: {name} {addresses}")
        src = render(name, size, blocks)
        if mode == "render":
            st = {"path": src, "sha256": sha(src)}
        elif mode == "install":
            st = install(name, src, blocks)
        else:
            st = status(name, sha(src), blocks)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        st = {"error": str(e)}
    print(json.dumps(st))


if __name__ == "__main__":
    main()
