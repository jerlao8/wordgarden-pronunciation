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

# Lasagna-only Phonon override. Claude's `luh-ZAN-yuh` / `luh zahn yuh`
# normalize to forms Phonon voices as "loozanyo". `lazanya` forces nya but
# stresses LA-; `luhzahnya` keeps nya with mid ZA stress (la-ZA-nya).
_OPERATOR_EXACT: dict[str, str] = {
    "lasagna": "luhzahnya",
}

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="WordGarden pronunciation", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Distinct reporters (or raw report count without reporterId) before OTA.
REPORT_PUBLISH_THRESHOLD = max(
    1, int(os.environ.get("WG_PRON_REPORT_THRESHOLD", "2"))
)
TALLY_PATH = DATA_ROOT / "report_tally.json"


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
    # Stable anonymous install id from the client (preferred for threshold).
    reporterId: str | None = None


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


_PAUSE_MARKS = re.compile(r"[-‐‑‒–—_]")


def _normalize_speak_form_for_phonon(form: str, *, spelling: str | None = None) -> str:
    """Strip Phonon pause marks from a speak form (mirrors Dart helper).

    Hyphens / underscores → removed. Spaces removed when [spelling] is a
    single token (Claude `con frairs` → `confrairs`); multi-word spellings
    keep word spaces (`or derves`). Apostrophes kept. Lowercased.
    """
    s = (
        (form or "")
        .strip()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("ʼ", "'")
        .casefold()
    )
    if not s:
        return s
    s = _PAUSE_MARKS.sub("", s)
    spelling_stripped = (spelling or "").strip()
    single_token = bool(spelling_stripped) and not re.search(r"\s", spelling_stripped)
    if single_token:
        s = re.sub(r"\s+", "", s)
    else:
        s = re.sub(r"\s+", " ", s).strip()
    return s


def _apply_operator_exact(pack: dict) -> bool:
    """Force lasagna-only (etc.) speak forms; returns True if pack changed."""
    exact = pack.setdefault("exact", {})
    vetted = pack.setdefault("vetted", [])
    changed = False
    vetted_keys = {_norm(str(v)) for v in vetted}
    for raw, spoken in _OPERATOR_EXACT.items():
        key = _norm(raw)
        form = _normalize_speak_form_for_phonon(spoken, spelling=raw) or spoken
        if exact.get(key) != form:
            exact[key] = form
            changed = True
        if key not in vetted_keys:
            vetted.append(key)
            vetted_keys.add(key)
            changed = True
    return changed


def _load_pack() -> dict:
    path = PACK_PATH if PACK_PATH.exists() else SEED_PACK
    if not path.exists():
        pack = {
            "version": 1,
            "packVersion": "0",
            "hyphenPolicy": "ota_corrections",
            "exact": {},
            "regex": [],
            "vetted": [],
        }
    else:
        pack = json.loads(path.read_text())
        pack.setdefault("exact", {})
        pack.setdefault("regex", [])
        pack.setdefault("vetted", list(pack.get("exact", {}).keys()))
    if _apply_operator_exact(pack):
        # Persist so OTA/corrections GET see the operator form immediately.
        # Only runs when the on-disk form differed (no bump loop).
        pack["packVersion"] = _bump_pack_version(
            str(pack.get("packVersion") or "0")
        )
        pack["hyphenPolicy"] = "ota_corrections"
        try:
            _save_pack(pack)
        except OSError:
            pass
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
    raw = exact.get(key) or exact.get(spelling)
    if not raw:
        return None
    # Older pack rows may still carry hyphen pause marks.
    return _normalize_speak_form_for_phonon(str(raw), spelling=spelling) or None


def _load_tally() -> dict:
    if not TALLY_PATH.exists():
        return {}
    try:
        raw = json.loads(TALLY_PATH.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_tally(tally: dict) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    TALLY_PATH.write_text(json.dumps(tally, indent=2, ensure_ascii=False) + "\n")


def _note_report(
    key: str,
    *,
    reporter_id: str | None,
    report_id: str,
) -> dict:
    """Track distinct reporters per spelling for the publish threshold.

    Prefer [reporter_id]. Without it, each report_id counts (MVP limitation:
    the same device can inflate the count by reinstalling / clearing local id).
    """
    tally = _load_tally()
    entry = tally.setdefault(key, {"reporters": [], "reportIds": []})
    reporters: list[str] = list(entry.get("reporters") or [])
    report_ids: list[str] = list(entry.get("reportIds") or [])
    newly = False
    rid = (reporter_id or "").strip()
    token = rid if rid else (report_id or "").strip()
    if token and token not in reporters:
        reporters.append(token)
        newly = True
    if report_id and report_id not in report_ids:
        report_ids.append(report_id)
    entry["reporters"] = reporters
    entry["reportIds"] = report_ids
    tally[key] = entry
    _save_tally(tally)
    return {
        "distinct": len(reporters),
        "total": len(report_ids),
        "newlyCounted": newly,
    }


# Minimal, general — no word lists, loanword tables, or digraph cookbooks.
_SYSTEM_PROMPT = """You write speak forms for Phonon, an English neural TTS that
reads ordinary Latin text literally (grapheme-to-phoneme naive; no IPA, no
SSML, no dictionary pronunciation keys).

Given a spelling and (optionally) a speak form that already sounded wrong,
return one short American-English respelling that Phonon will voice as the
correct US pronunciation of that word. You already know the pronunciation;
invent a suitable text form.

Prefer one continuous lowercase letter string a careful English reader would
say aloud correctly — no hyphens or syllable spaces (the client also strips
those). Avoid dictionary respell conventions that literal TTS engines
misread: ALL-CAPS stress marks, AH/UH/OO vowel codes, and IPA-ish digraphs.
Write as if feeding a child-oriented English TTS that sounds out spelling,
not a linguist's respelling key.

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
) -> tuple[str | None, str]:
    """Returns (speakForm or None, raw Claude text)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, ""
    try:
        import anthropic
    except ImportError:
        return None, ""

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
        return None, text
    # Reject exact / folded echo of a known failure (pre-smooth).
    cf = _fold_speak(cand)
    for ban in banned:
        if cf == _fold_speak(ban):
            return None, text
    smoothed = _normalize_speak_form_for_phonon(cand, spelling=spelling)
    return (smoothed or None), text


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "version": app.version,
        "packVersion": _load_pack().get("packVersion"),
        "reportPublishThreshold": REPORT_PUBLISH_THRESHOLD,
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
    """Accept a report; publish OTA only after multiple distinct reporters.

    Claude is prompted with spelling + failed spokenForm only (no audio).
    First report(s) are stored (Claude logged) with status `recorded` and are
    NOT pushed to the OTA pack — Phonon is usually better than a one-tap
    Claude rewrite. Once [REPORT_PUBLISH_THRESHOLD] distinct reporters agree,
    Claude's speakForm is published + vetted for everyone.
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
    record["priorSpeakForm"] = existing

    tally = _note_report(
        key,
        reporter_id=body.reporterId,
        report_id=report_id,
    )
    record["reportTally"] = tally

    # Operator-locked spellings (lasagna): keep the Phonon-vetted form;
    # never let Claude overwrite with a worse respell. Publish immediately.
    if key in _OPERATOR_EXACT:
        locked = _normalize_speak_form_for_phonon(
            _OPERATOR_EXACT[key], spelling=spelling
        )
        record["claudeRaw"] = ""
        record["claudeSpeakForm"] = locked
        record["operatorLocked"] = True
        (REPORTS_DIR / f"{report_id}.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        exact = pack.setdefault("exact", {})
        vetted = pack.setdefault("vetted", [])
        changed = exact.get(key) != locked
        exact[key] = locked
        if key not in {_norm(str(v)) for v in vetted}:
            vetted.append(key)
            changed = True
        if changed:
            pack["packVersion"] = _bump_pack_version(
                str(pack.get("packVersion") or "0")
            )
            pack["hyphenPolicy"] = "ota_corrections"
            _save_pack(pack)
        print(
            f"WG-PRON report spelling={spelling!r} operatorLocked={locked!r} "
            f"prior={existing!r} tally={tally}",
            flush=True,
        )
        return {
            "status": "locked",
            "spelling": spelling,
            "speakForm": locked,
            "vetted": True,
            "packVersion": pack.get("packVersion"),
            "priorSpeakForm": existing,
            "reportCount": tally["distinct"],
            "detail": "Operator-locked speak form; Claude skipped.",
        }

    suggestion, claude_raw = _claude_respell(
        spelling,
        body.spokenForm,
        locale=body.locale or "en-US",
        engine=body.engine,
        prior_pack_form=existing,
    )
    record["claudeRaw"] = claude_raw
    record["claudeSpeakForm"] = suggestion
    (REPORTS_DIR / f"{report_id}.json").write_text(
        json.dumps(record, indent=2) + "\n"
    )
    print(
        f"WG-PRON report spelling={spelling!r} spokenForm={body.spokenForm!r} "
        f"prior={existing!r} claudeSpeakForm={suggestion!r} "
        f"tally={tally} raw={claude_raw!r}",
        flush=True,
    )

    # Below threshold: store + log Claude, do not OTA-publish or auto-apply.
    if tally["distinct"] < REPORT_PUBLISH_THRESHOLD:
        return {
            "status": "recorded",
            "spelling": spelling,
            "speakForm": None,
            "vetted": False,
            "packVersion": pack.get("packVersion"),
            "priorSpeakForm": existing,
            "reportCount": tally["distinct"],
            "threshold": REPORT_PUBLISH_THRESHOLD,
            "detail": (
                "Stored; waiting for more reports before OTA publish. "
                "Claude suggestion logged server-side only."
            ),
        }

    if not suggestion:
        # Threshold met but Claude failed — drop a prior the learner already
        # rejected rather than keeping a bad pack entry.
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
            "reportCount": tally["distinct"],
            "detail": "Stored for review; Claude returned no usable form.",
        }

    exact = pack.setdefault("exact", {})
    retrained = bool(existing) and existing != suggestion
    # suggestion is already Phonon-smoothed in _claude_respell.
    exact[key] = suggestion
    vetted = pack.setdefault("vetted", [])
    if key not in {_norm(str(v)) for v in vetted}:
        vetted.append(key)
    pack["packVersion"] = _bump_pack_version(str(pack.get("packVersion") or "0"))
    pack["hyphenPolicy"] = "ota_corrections"
    _save_pack(pack)
    return {
        "status": "retrained" if retrained else "published",
        # Alias kept for older clients that check `corrected`.
        "corrected": True,
        "spelling": spelling,
        "speakForm": suggestion,
        "vetted": True,
        "packVersion": pack["packVersion"],
        "priorSpeakForm": existing,
        "reportCount": tally["distinct"],
    }
