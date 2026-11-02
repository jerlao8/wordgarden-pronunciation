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

app = FastAPI(title="WordGarden pronunciation", version="0.2.2")
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


def _drop_spelling(pack: dict, spelling: str) -> bool:
    """Remove [spelling] from exact + vetted. Returns True if anything changed."""
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


def _claude_respell(
    spelling: str,
    spoken_form: str,
    *,
    locale: str = "en-US",
    engine: str | None = None,
    voice_id: str | None = None,
    prior_pack_form: str | None = None,
    has_audio: bool = False,
) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    client = anthropic.Anthropic(api_key=key)
    # Anthropic Messages API has no audio modality — WAV is stored for operators.
    # Claude must infer the failure from spelling + spokenForm (+ prior form).
    fed = (spoken_form or spelling).strip() or spelling
    prior = (prior_pack_form or "").strip()
    prior_line = (
        f"Prior pack correction that STILL sounded wrong (do not reuse): {prior}\n"
        if prior
        else ""
    )
    if has_audio:
        audio_line = (
            "A Phonon TTS WAV was uploaded with this report and stored for "
            "operators. You CANNOT hear it — the Messages API has no audio "
            "input. Infer what went wrong from the orthography vs the speak "
            "form that was fed to TTS (and any prior failed correction).\n"
        )
    else:
        audio_line = "No WAV attached; rely on spelling + spokenForm only.\n"

    system = """You write Phonon-TTS speak forms for WordGarden, a US children's
spelling app. Phonon (Zoey 31M) is English text-only neural TTS — no IPA, no
SSML phonemes. Your job is ONE short American-English respelling the model
will say correctly when read as ordinary English text.

Output rules:
- Return ONLY the speak form. No quotes, no explanation, no IPA.
- Prefer ordinary English letters; light hyphenation between syllables is OK.
- Optional CAPS on the stressed syllable (dictionary-respell style) is OK.
- Avoid letter-name traps (forms that make the model say letter names).
- Avoid Spanish-ish or Italian-orthography endings that English TTS mangles
  (e.g. -o / -anyo / "loo-" onsets on Italian food words).
- Match General American dictionary pronunciation for the headword.
- If the form fed to TTS already failed, propose a DIFFERENT form — never
  echo the failed spokenForm or a prior pack correction.

Loanword guidance (examples of target US sound — patterns only; do not
hardcode unless the reported spelling matches that word):
- lasagna → roughly "luh-ZAHN-yuh" / la-zan-ya (NOT "loo-zan-yo" / loozanyo)
- bruschetta → roughly "broo-SKET-uh" (NOT "broo-SHET-uh" British drift if US)
- croissant → roughly "kwah-SAHN"
- hors d'oeuvres → roughly "or-DURVZ"
- chauffeur → roughly "shoh-FUR"

When unsure between raw orthography and a respell, pick the form most likely
to yield the US dictionary reading on Phonon English TTS."""

    user = f"""Spelling (orthography the child is learning): {spelling}
Speak form that was fed to Phonon and sounded wrong: {fed}
{prior_line}Locale: {locale}
Engine: {engine or "phonon"}
Voice: {voice_id or "Zoey"}
{audio_line}
Return one improved speak form for Phonon."""

    msg = client.messages.create(
        model=os.environ.get("WG_CLAUDE_MODEL", "claude-sonnet-4-5-20250929"),
        max_tokens=40,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()
    text = text.strip("\"'` ").splitlines()[0].strip()
    if not text or len(text) > 80:
        return None
    if not re.fullmatch(r"[A-Za-z0-9'’\- ]+", text):
        return None
    # Reject echo of the failed form — force a real retrain attempt.
    if text.casefold() == fed.casefold():
        return None
    if prior and text.casefold() == prior.casefold():
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


@app.delete("/v1/corrections/{spelling}")
def delete_correction(spelling: str) -> dict:
    """Un-vet a spelling so devices can show Doesn’t sound right again."""
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

    Always retrain on a new report. A prior pack entry is treated as a failed
    attempt when the learner heard it and tapped Doesn't sound right again —
    never short-circuit with already_corrected.
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
        json.dumps({k: v for k, v in record.items() if k != "wavBase64"}, indent=2)
        + "\n"
    )
    if body.wavBase64:
        wav_name = body.wavFileName or f"{report_id}.wav"
        (REPORTS_DIR / wav_name).write_bytes(base64.b64decode(body.wavBase64))

    suggestion = _claude_respell(
        spelling,
        body.spokenForm,
        locale=body.locale or "en-US",
        engine=body.engine,
        voice_id=body.voiceId,
        prior_pack_form=existing,
        has_audio=bool(body.wavBase64),
    )
    if not suggestion:
        # Keep any prior form so the device is not left with nothing; mark
        # queued so operators can fix manually.
        return {
            "status": "queued",
            "spelling": spelling,
            "speakForm": existing,
            "vetted": bool(existing),
            "packVersion": pack.get("packVersion"),
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
