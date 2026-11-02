#!/usr/bin/env python3
"""Print / optionally gist-edit the OTA speak pack."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "store" / "remote" / "speak_rewrites_ota.json"
# Dedicated public gist for the speak-rewrites OTA pack.
GIST_ID = "c5fdb854b75d7ab4a82cddfcc83a3b64"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--gist",
        action="store_true",
        help=f"gh gist edit {GIST_ID} --filename speak_rewrites_ota.json",
    )
    args = ap.parse_args()
    if not PACK.exists():
        sys.exit(f"missing {PACK}")
    print(PACK.read_text())
    if args.gist:
        subprocess.check_call(
            [
                "gh",
                "gist",
                "edit",
                GIST_ID,
                "--filename",
                "speak_rewrites_ota.json",
                str(PACK),
            ]
        )
        print("gist updated", file=sys.stderr)


if __name__ == "__main__":
    main()
