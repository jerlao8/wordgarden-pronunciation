#!/usr/bin/env python3
"""Hosted pronunciation-report API for WordGarden OTA speak packs."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
# On Render, persist under /var/data when a disk is mounted; else local data/.
DATA_ROOT = Path(os.environ.get("WG_PRON_DATA", HERE / "data"))
PACK_PATH = DATA_ROOT / "speak_rewrites_ota.json"
REPORTS_DIR = DATA_ROOT / "reports"
# Seed shipped next to the image; repo copy used when developing locally.
SEED_PACK = HERE / "speak_rewrites_ota.json"
if not SEED_PACK.exists():
    SEED_PACK = HERE.parents[1] / "store" / "remote" / "speak_rewrites_ota.json"
GIST_ID = os.environ.get("WG_SPEAK_PACK_GIST", "c5fdb854b75d7ab4a82cddfcc83a3b64")

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="WordGarden pronunciation", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReportIn(BaseModel):
    id: str = ""
    spelling: str
    spokenForm: str = ""
    kind: str = "word"
    reportedAt: str | None = None
    locale: str = "en-US"
    voiceId: str | None = None
    engine: str | None = None
    clipCacheGen: str | None = None
    appVersion: str | None = None
    wavBase64: str | None = None
    wavFileName: str | None = None


def _norm(s: str) -> str:
    return (
        s.strip()
        .lower()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("ʼ", "'")
    )


def _load_pack() -> dict:
    path = PACK_PATH if PACK_PATH.exists() else SEED_PACK
    if not path.exists():
        return {
            "version": 1,
            "packVersion": "0",
            "hyphenPolicy": "ota_corrections",
            "exact": {},
            "regex": [],
            "vetted": [],
        }
    pack = json.loads(path.read_text())
    pack.setdefault("exact", {})
    pack.setdefault("regex", [])
    pack.setdefault("vetted", list(pack.get("exact", {}).keys()))
    return pack


def _bump_pack_version(current: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if current.startswith(today + "."):
        try:
            n = int(current.rsplit(".", 1)[-1])
            return f"{today}.{n + 1}"
        except ValueError:
            pass
    return f"{today}.1"


def _save_pack(pack: dict) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    PACK_PATH.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n")
    # Keep repo copy in sync when running locally.
    try:
        if SEED_PACK.parent.exists():
            SEED_PACK.write_text(PACK_PATH.read_text())
    except OSError:
        pass
    _publish_gist(pack)


def _publish_gist(pack: dict) -> None:
    """Push pack to the public gist devices fetch (best-effort)."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token or not GIST_ID:
        return
    try:
        import urllib.request

        body = json.dumps(
            {
                "files": {
                    "speak_rewrites_ota.json": {
                        "content": json.dumps(pack, indent=2, ensure_ascii=False) + "\n"
                    }
                }
            }
        ).encode()
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            data=body,
            method="PATCH",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "wordgarden-pronunciation",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception:
        # Local disk pack still serves GET /v1/speak-rewrites and corrections.
        return


def _claude_respell(spelling: str, spoken_form: str) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    client = anthropic.Anthropic(api_key=key)
    prompt = f"""You write Phonon-TTS speak forms for a children's spelling app.
Phonon is English text-only (no IPA). Return ONE short American-English
respelling or lightly hyphenated form that a neural TTS will say correctly.
Do not explain. No quotes. Prefer ordinary English letters.

Spelling: {spelling}
Current speak form: {spoken_form or spelling}
"""
    msg = client.messages.create(
        model=os.environ.get("WG_CLAUDE_MODEL", "claude-sonnet-4-5-20250929"),
        max_tokens=40,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()
    text = text.strip("\"'` ").splitlines()[0].strip()
    if not text or len(text) > 80:
        return None
    if not re.fullmatch(r"[A-Za-z0-9'’\- ]+", text):
        return None
    return text


def _lookup(pack: dict, spelling: str) -> str | None:
    key = _norm(spelling)
    exact = pack.get("exact") or {}
    return exact.get(key) or exact.get(spelling)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "packVersion": _load_pack().get("packVersion")}


@app.get("/v1/speak-rewrites")
def get_pack() -> dict:
    return _load_pack()


@app.get("/v1/corrections/{spelling}")
def get_correction(spelling: str) -> dict:
    pack = _load_pack()
    form = _lookup(pack, spelling)
    if not form:
        raise HTTPException(status_code=404, detail="no_correction")
    return {
        "spelling": spelling,
        "speakForm": form,
        "vetted": True,
        "packVersion": pack.get("packVersion"),
    }


@app.post("/v1/pronunciation-reports")
def post_report(body: ReportIn) -> dict:
    spelling = body.spelling.strip()
    if not spelling:
        raise HTTPException(status_code=400, detail="empty spelling")

    key = _norm(spelling)
    pack = _load_pack()
    existing = _lookup(pack, spelling)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_id = body.id or f"{stamp}-{key}"
    record = body.model_dump()
    record["normalized"] = key
    record["receivedAt"] = datetime.now(timezone.utc).isoformat()
    (REPORTS_DIR / f"{report_id}.json").write_text(
        json.dumps({k: v for k, v in record.items() if k != "wavBase64"}, indent=2)
        + "\n"
    )
    if body.wavBase64:
        wav_name = body.wavFileName or f"{report_id}.wav"
        (REPORTS_DIR / wav_name).write_bytes(base64.b64decode(body.wavBase64))

    if existing:
        return {
            "status": "already_corrected",
            "spelling": spelling,
            "speakForm": existing,
            "vetted": True,
            "packVersion": pack.get("packVersion"),
            "detail": "Correction exists; applied immediately on device.",
        }

    suggestion = _claude_respell(spelling, body.spokenForm)
    if not suggestion:
        return {
            "status": "queued",
            "spelling": spelling,
            "vetted": False,
            "detail": "Stored for review; set ANTHROPIC_API_KEY to auto-correct.",
        }

    exact = pack.setdefault("exact", {})
    exact[key] = suggestion
    vetted = pack.setdefault("vetted", [])
    if key not in vetted:
        vetted.append(key)
    pack["packVersion"] = _bump_pack_version(str(pack.get("packVersion") or "0"))
    pack["hyphenPolicy"] = "ota_corrections"
    _save_pack(pack)
    return {
        "status": "corrected",
        "spelling": spelling,
        "speakForm": suggestion,
        "vetted": True,
        "packVersion": pack["packVersion"],
    }
