#!/usr/bin/env python3
"""Print / optionally gist-edit the OTA speak pack."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "store" / "remote" / "speak_rewrites_ota.json"
# Same gist as bundled_banks.json — add speak_rewrites_ota.json as a second file.
GIST_ID = "9383f04223241543400b0a2e43199ffd"


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
