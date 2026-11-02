#!/usr/bin/env python3
"""Hosted pronunciation-report API for WordGarden OTA speak packs."""

from __future__ import annotations

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

app = FastAPI(title="WordGarden pronunciation", version="0.3.0")
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


def _norm(s: str) -> str:
    return (
        s.strip()
        .lower()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("ʼ", "'")
    )


def _fold_speak(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").casefold())


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
    try:
        if SEED_PACK.parent.exists():
            SEED_PACK.write_text(PACK_PATH.read_text())
    except OSError:
        pass
    _publish_gist(pack)


def _publish_gist(pack: dict) -> None:
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
        return


def _drop_spelling(pack: dict, spelling: str) -> bool:
    key = _norm(spelling)
    if not key:
        return False
    exact = pack.setdefault("exact", {})
    vetted = pack.setdefault("vetted", [])
    changed = False
    if key in exact:
        exact.pop(key, None)
        changed = True
    new_vetted = [v for v in vetted if _norm(str(v)) != key]
    if len(new_vetted) != len(vetted):
        pack["vetted"] = new_vetted
        changed = True
    return changed


def _lookup(pack: dict, spelling: str) -> str | None:
    key = _norm(spelling)
    exact = pack.get("exact") or {}
    return exact.get(key) or exact.get(spelling)


# Minimal, general — no word lists, loanword tables, or digraph cookbooks.
_SYSTEM_PROMPT = """You write speak forms for Phonon, an English neural TTS that
reads ordinary text literally (no IPA, no SSML).

Given a spelling and (optionally) a speak form that already sounded wrong,
return one short American-English respelling that Phonon will voice as the
correct US pronunciation of that word. You already know the pronunciation;
invent a suitable text form.

Reply with ONLY a JSON object: {"speakForm":"..."}.
No other keys, no markdown, no explanation.
Constraints: speakForm uses letters, spaces, hyphens, apostrophes only;
length 1–80; do not echo a known-failed spokenForm."""


def _parse_speak_form(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    # Prefer JSON object; fall back to a bare string line.
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            cand = str(obj.get("speakForm") or "").strip()
        elif isinstance(obj, str):
            cand = obj.strip()
        else:
            cand = ""
    except json.JSONDecodeError:
        # Model sometimes wraps JSON in fences or adds a line of prose.
        m = re.search(r"\{[^{}]*\"speakForm\"[^{}]*\}", raw, re.S)
        if m:
            try:
                cand = str(json.loads(m.group(0)).get("speakForm") or "").strip()
            except Exception:
                cand = raw.strip("\"'` ").splitlines()[0].strip()
        else:
            cand = raw.strip("\"'` ").splitlines()[0].strip()
    if not cand or len(cand) > 80:
        return None
    if not re.fullmatch(r"[A-Za-z0-9'’\- ]+", cand):
        return None
    return cand


def _claude_respell(
    spelling: str,
    spoken_form: str,
    *,
    locale: str = "en-US",
    engine: str | None = None,
    prior_pack_form: str | None = None,
) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    fed = (spoken_form or spelling).strip() or spelling
    prior = (prior_pack_form or "").strip()
    banned = []
    for form in (fed, prior):
        fold = _fold_speak(form)
        if fold and fold not in {_fold_speak(b) for b in banned}:
            banned.append(form)

    failed_line = ""
    if banned:
        failed_line = (
            "Failed spokenForm(s) — do not reuse or near-variants: "
            + ", ".join(banned)
            + "\n"
        )

    user = (
        f"spelling: {spelling}\n"
        f"spokenForm (failed): {fed}\n"
        f"{failed_line}"
        f"locale: {locale}\n"
        f"engine: {engine or 'phonon'}\n"
        'Return JSON: {"speakForm":"..."}\n'
    )

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=os.environ.get("WG_CLAUDE_MODEL", "claude-sonnet-4-5-20250929"),
        max_tokens=60,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()
    cand = _parse_speak_form(text)
    if not cand:
        return None
    # Reject exact / folded echo of a known failure.
    cf = _fold_speak(cand)
    for ban in banned:
        if cf == _fold_speak(ban):
            return None
    return cand


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "version": app.version,
        "packVersion": _load_pack().get("packVersion"),
    }


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


@app.delete("/v1/corrections/{spelling}")
def delete_correction(spelling: str) -> dict:
    """Un-vet a spelling so devices can show Doesn't sound right again."""
    key = _norm(spelling)
    if not key:
        raise HTTPException(status_code=400, detail="empty spelling")
    pack = _load_pack()
    if not _drop_spelling(pack, key):
        return {
            "status": "absent",
            "spelling": spelling,
            "packVersion": pack.get("packVersion"),
        }
    pack["packVersion"] = _bump_pack_version(str(pack.get("packVersion") or "0"))
    pack["hyphenPolicy"] = "ota_corrections"
    _save_pack(pack)
    return {
        "status": "removed",
        "spelling": spelling,
        "vetted": False,
        "packVersion": pack["packVersion"],
    }


@app.post("/v1/pronunciation-reports")
def post_report(body: ReportIn) -> dict:
    """Accept a report and (re)train a speak form via Claude.

    Claude is prompted with spelling + failed spokenForm only (no audio).
    Always retrain on a new report; never short-circuit with already_corrected.
    """
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
        json.dumps(record, indent=2) + "\n"
    )

    suggestion = _claude_respell(
        spelling,
        body.spokenForm,
        locale=body.locale or "en-US",
        engine=body.engine,
        prior_pack_form=existing,
    )
    if not suggestion:
        # Drop a prior the learner already rejected rather than keeping it.
        dropped = False
        if existing and _drop_spelling(pack, key):
            pack["packVersion"] = _bump_pack_version(
                str(pack.get("packVersion") or "0")
            )
            pack["hyphenPolicy"] = "ota_corrections"
            _save_pack(pack)
            dropped = True
        return {
            "status": "queued",
            "spelling": spelling,
            "speakForm": None,
            "vetted": False,
            "packVersion": pack.get("packVersion"),
            "priorSpeakForm": existing,
            "droppedPrior": dropped,
            "detail": "Stored for review; Claude returned no usable form.",
        }

    exact = pack.setdefault("exact", {})
    retrained = bool(existing) and existing != suggestion
    exact[key] = suggestion
    vetted = pack.setdefault("vetted", [])
    if key not in {_norm(str(v)) for v in vetted}:
        vetted.append(key)
    pack["packVersion"] = _bump_pack_version(str(pack.get("packVersion") or "0"))
    pack["hyphenPolicy"] = "ota_corrections"
    _save_pack(pack)
    return {
        "status": "retrained" if retrained else "corrected",
        "spelling": spelling,
        "speakForm": suggestion,
        "vetted": True,
        "packVersion": pack["packVersion"],
        "priorSpeakForm": existing,
    }
