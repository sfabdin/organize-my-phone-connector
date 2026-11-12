"""
Is App Library placement visible in ANY icon state format version?

Removing an app from the layout does not remove it from the Home Screen - iOS
puts it straight back. That means "hidden from Home Screen" is a property the
format version we read (2) does not express.

This asks the phone for every format version it will answer to and compares
what comes back, looking for a flag that distinguishes a Home Screen app from
an App-Library-only one.

READ ONLY. This script never writes to your phone.

    python tools/probe_formats.py

Copyright (C) 2026 Organize My Phone
Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import layout_ops as L  # noqa: E402
from bridge import _maybe_await, _Session, get_bridge

VERSIONS = ["1", "2", "3", "4", "5"]


def all_keys(state) -> Counter:
    """Every field name appearing on any object, with counts."""
    seen: Counter = Counter()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                seen[key] += 1
                walk(value)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(state)
    return seen


def leaf_ids(state) -> set:
    try:
        return {L.identity(i) for i in L.iter_leaves(state)}
    except Exception:
        return set()


async def fetch(version: str):
    """One get_icon_state at a given format version."""
    bridge = get_bridge()
    connect, springboard_cls = bridge._load()
    async with _Session(connect) as lockdown:
        springboard = springboard_cls(lockdown=lockdown)
        try:
            return await _maybe_await(springboard.get_icon_state(version))
        except TypeError:
            # Older signatures take no argument at all.
            if version != "2":
                raise
            return await _maybe_await(springboard.get_icon_state())


async def main() -> int:
    config.ensure_directories()
    out = config.SNAPSHOT_DIR / f"format-probe-{datetime.now():%Y%m%d-%H%M%S}.json"
    results = {}

    print("\nAsking the phone for each icon state format version.")
    print("Nothing is written to your phone.\n")

    for version in VERSIONS:
        try:
            state = await fetch(version)
        except Exception as exc:
            print(f"  version {version}:  refused  ({type(exc).__name__})")
            continue

        keys = all_keys(state)
        ids = leaf_ids(state)
        results[version] = {"keys": dict(keys), "objectCount": len(ids), "state": state}
        print(f"  version {version}:  {len(ids):3} objects, {len(keys):2} distinct field names")

    if not results:
        print("\nNo format version answered. Is the phone connected and unlocked?")
        return 1

    print("\n" + "=" * 62)

    baseline = results.get("2") or next(iter(results.values()))
    base_keys = set(baseline["keys"])
    base_ids = leaf_ids(baseline["state"])

    interesting = False
    for version, data in results.items():
        if data is baseline:
            continue
        extra = set(data["keys"]) - base_keys
        missing = base_keys - set(data["keys"])
        count_delta = len(leaf_ids(data["state"])) - len(base_ids)

        if extra or count_delta:
            interesting = True
            print(f"\n  Version {version} differs from version 2:")
            if extra:
                print(f"    fields only in this version: {sorted(extra)}")
            if missing:
                print(f"    fields absent from this version: {sorted(missing)}")
            if count_delta:
                print(f"    {count_delta:+d} objects compared with version 2")

    # Any field that looks like a visibility or placement flag is the prize.
    WORDS = ("hidden", "visible", "library", "applibrary", "placement", "removed",
             "onscreen", "springboard", "offscreen", "cloud", "shown")
    flags = sorted(
        {k for data in results.values() for k in data["keys"]
         if any(w in k.lower() for w in WORDS)}
    )

    print("\n" + "=" * 62)
    if flags:
        print("  POSSIBLE VISIBILITY FIELDS FOUND:")
        for flag in flags:
            print(f"     {flag}")
        print("\n  Worth investigating - removal may be achievable after all.")
    elif interesting:
        print("  Versions differ, but no field looks like a visibility flag.")
        print("  Check the dump before concluding.")
    else:
        print("  Every version returns the same shape, and nothing resembles a")
        print("  Home Screen visibility flag.")
        print("\n  Conclusion: removal is not expressible through this API.")
    print("=" * 62)

    out.write_text(json.dumps(
        {v: {"keys": d["keys"], "objectCount": d["objectCount"]} for v, d in results.items()},
        indent=2, default=str), encoding="utf-8")
    print(f"\n  Field dump saved: {out}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
