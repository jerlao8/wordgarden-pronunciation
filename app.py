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

app = FastAPI(title="WordGarden pronunciation", version="0.2.3")
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
    # Optional learner/operator label for what the clip sounded like
    # (e.g. "loozanyo"). Claude cannot hear the WAV; this is the failure signal.
    heardLike: str | None = None
    failureNote: str | None = None


def _norm(s: str) -> str:
    return (
        s.strip()
        .lower()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("ʼ", "'")
    )


def _fold_speak(s: str) -> str:
    """Collapse hyphens/spaces/caps so near-variants compare equal."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").casefold())


def _soft_fold(s: str) -> str:
    """Folds Phonon-equivalent vowel spellings without turning 'lah' into 'la'.

    Maps zahn→zan, oo→u, uh→u. Does NOT map ah→a (that made lah-ZAHN-ya
    collide with lazanya and blocked the better lasagna candidates).
    """
    x = _fold_speak(s)
    x = x.replace("zahn", "zan").replace("oo", "u").replace("uh", "u")
    return x


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


def _collect_failed_forms(spelling: str, *extras: str | None) -> list[str]:
    """Prior speak forms that already failed for this spelling (reports + extras)."""
    key = _norm(spelling)
    out: list[str] = []
    seen: set[str] = set()

    def add(form: str | None) -> None:
        f = (form or "").strip()
        if not f:
            return
        fold = _fold_speak(f)
        if not fold or fold in seen:
            return
        seen.add(fold)
        out.append(f)

    for extra in extras:
        add(extra)

    if REPORTS_DIR.exists():
        for path in sorted(REPORTS_DIR.glob("*.json")):
            try:
                rec = json.loads(path.read_text())
            except Exception:
                continue
            if _norm(str(rec.get("normalized") or rec.get("spelling") or "")) != key:
                continue
            add(str(rec.get("spokenForm") or ""))
            add(str(rec.get("appliedSpeakForm") or ""))
            add(str(rec.get("heardLike") or ""))
    return out


# Phonon misreads dictionary respells of Italian -gn- words as "loo…yo".
# Reject these shapes server-side even when Claude echoes the old prompt example.
_LOOZANYO_SHAPE = re.compile(
    r"(?i)^\s*(loo|luh|lu)[- ]?(zan|zahn)[- ]?(yo|yuh|ya)\s*$"
    r"|loozanyo|loo[- ]?zan[- ]?yo|lazanya"
)


def _looks_loozanyo_like(form: str) -> bool:
    f = (form or "").strip()
    if not f:
        return False
    if _LOOZANYO_SHAPE.search(f):
        return True
    fold = _fold_speak(f)
    soft = _soft_fold(f)
    # Exact known-bad collapses (including old prompt example luh-ZAHN-yuh).
    return fold in {
        "loozanyo",
        "loozahnyo",
        "luhzanyuh",
        "luhzahnyuh",
        "luhzanya",
        "luhzahnya",
        "lazanya",
    } or soft in {"loozanyo", "luzanyo", "luzanyu", "luzanya", "lazanya"}


def _too_close_to_failed(candidate: str, failed: list[str]) -> bool:
    c = _fold_speak(candidate)
    cs = _soft_fold(candidate)
    if not c:
        return False
    for ban in failed:
        b = _fold_speak(ban)
        bs = _soft_fold(ban)
        if not b:
            continue
        if c == b or cs == bs:
            return True
        # Tiny edits of a known failure (hyphen/caps already folded).
        if abs(len(c) - len(b)) <= 1 and _edit_distance(c, b) <= 1:
            return True
    return False


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _reject_reason(candidate: str, failed: list[str]) -> str | None:
    if _looks_loozanyo_like(candidate):
        return "loozanyo_shape"
    if _too_close_to_failed(candidate, failed):
        return "echo_failed_form"
    return None


_SYSTEM_PROMPT = """You write Phonon-TTS speak forms for WordGarden, a US children's
spelling app. Phonon (Zoey 31M) is English text-only neural TTS — no IPA, no
SSML phonemes. Your job is ONE short American-English respelling the model
will say correctly when read as ordinary English text.

IMPORTANT: You cannot hear audio. Train only from spelling + the speak form
that was fed to Phonon + any learner "heard like" / prior-failure labels.

Output rules:
- Return ONLY the speak form. No quotes, no explanation, no IPA.
- Prefer ordinary English letters; light hyphenation between syllables is OK.
- Optional CAPS on the stressed syllable (dictionary-respell style) is OK.
- Avoid letter-name traps (forms that make the model say letter names).
- If the form fed to TTS already failed, propose a DIFFERENT form — never
  echo the failed spokenForm, a prior pack correction, or a near-variant
  (same letters with only hyphen/CAPS/AH↔A/UH↔U changes).

Phonon failure modes (observed — do NOT propose these shapes):
- Italian -gn- food words: dictionary respells like "luh-ZAHN-yuh",
  "luh-ZAN-yuh", "la-zan-ya", "loo-ZAHN-yo" are heard as "loozanyo".
  Avoid "loo"/"luh"/"lu" onsets paired with "zan"/"zahn" and "yo"/"yuh"
  endings. Prefer an open "lah"/"la" first syllable and a clear final "ya",
  or leave ordinary English orthography when a respell keeps failing.
- Avoid Spanish-ish / Italian orthography endings English TTS mangles
  (-o / -anyo onsets with "loo-").

Other loanword patterns (patterns only; not per-word hardcodes):
- bruschetta → roughly "broo-SKET-uh" (US hard-k; not British "SHET")
- croissant → roughly "kwah-SAHN"
- hors d'oeuvres → roughly "or-DURVZ"
- chauffeur → roughly "shoh-FUR"

When unsure between raw orthography and a respell, pick the form most likely
to yield the US dictionary reading on Phonon English TTS — and never reuse a
form the learner already marked wrong."""


def _claude_respell(
    spelling: str,
    spoken_form: str,
    *,
    locale: str = "en-US",
    engine: str | None = None,
    voice_id: str | None = None,
    prior_pack_form: str | None = None,
    failed_forms: list[str] | None = None,
    heard_like: str | None = None,
    failure_note: str | None = None,
    has_audio: bool = False,
    reject_hint: str | None = None,
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
    # Claude must infer the failure from spelling + spokenForm (+ labels/history).
    fed = (spoken_form or spelling).strip() or spelling
    prior = (prior_pack_form or "").strip()
    failed = [f for f in (failed_forms or []) if (f or "").strip()]
    failed_line = ""
    if failed:
        joined = ", ".join(failed[:12])
        failed_line = (
            f"Previous speak forms that ALREADY failed for this spelling "
            f"(do not reuse or near-variants): {joined}\n"
        )
    heard = (heard_like or "").strip()
    heard_line = (
        f'Learner said the clip sounded like: "{heard}" — avoid any form '
        f"Phonon would voice that way.\n"
        if heard
        else ""
    )
    note = (failure_note or "").strip()
    note_line = f"Failure note: {note}\n" if note else ""
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
            "form that was fed to TTS, plus any heard-like / prior-failure "
            "labels.\n"
        )
    else:
        audio_line = "No WAV attached; rely on spelling + spokenForm + labels.\n"

    reject_line = (
        f"Your previous proposal was rejected ({reject_hint}). "
        f"Propose a clearly different form.\n"
        if reject_hint
        else ""
    )

    user = f"""Spelling (orthography the child is learning): {spelling}
Speak form that was fed to Phonon and sounded wrong: {fed}
{prior_line}{failed_line}{heard_line}{note_line}{reject_line}Locale: {locale}
Engine: {engine or "phonon"}
Voice: {voice_id or "Zoey"}
{audio_line}
Return one improved speak form for Phonon."""

    msg = client.messages.create(
        model=os.environ.get("WG_CLAUDE_MODEL", "claude-sonnet-4-5-20250929"),
        max_tokens=40,
        system=_SYSTEM_PROMPT,
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
    reason = _reject_reason(text, failed + [fed, prior, heard])
    if reason:
        return None
    return text


def _claude_respell_with_retry(**kwargs: object) -> str | None:
    """Ask Claude; if the proposal is banned, retry once with a reject hint."""
    first = _claude_respell(**kwargs)  # type: ignore[arg-type]
    if first:
        return first
    # Second attempt only when we have an API key path that returned None due
    # to rejection — still call again with an explicit reject hint so Claude
    # leaves the loozanyo basin.
    failed = list(kwargs.get("failed_forms") or [])  # type: ignore[arg-type]
    fed = str(kwargs.get("spoken_form") or "")
    prior = str(kwargs.get("prior_pack_form") or "")
    hint = "near-variant of a failed form or loozanyo-shaped respell"
    kwargs = dict(kwargs)
    kwargs["reject_hint"] = hint
    # Seed failed list with common loozanyo basin so retry avoids it.
    basin = [
        fed,
        prior,
        "luh-ZAHN-yuh",
        "luh-ZAN-yuh",
        "la-zan-ya",
        "loo-ZAHN-yo",
        "loozanyo",
        "lazanya",
    ]
    merged: list[str] = []
    seen: set[str] = set()
    for f in list(failed) + basin:
        fold = _fold_speak(str(f))
        if not fold or fold in seen:
            continue
        seen.add(fold)
        merged.append(str(f))
    kwargs["failed_forms"] = merged
    return _claude_respell(**kwargs)  # type: ignore[arg-type]


def _lookup(pack: dict, spelling: str) -> str | None:
    key = _norm(spelling)
    exact = pack.get("exact") or {}
    return exact.get(key) or exact.get(spelling)


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

    Claude does not receive audio (Anthropic Messages API has no audio
    modality). Training uses spelling + spokenForm + optional heardLike /
    failure history. WAV is stored for operators only.
    """
    spelling = body.spelling.strip()
    if not spelling:
        raise HTTPException(status_code=400, detail="empty spelling")

    key = _norm(spelling)
    pack = _load_pack()
    existing = _lookup(pack, spelling)

    # Infer a heard-like label when the failed feed itself is loozanyo-shaped.
    heard_like = (body.heardLike or "").strip() or None
    if not heard_like and (
        _looks_loozanyo_like(body.spokenForm) or _looks_loozanyo_like(existing or "")
    ):
        heard_like = "loozanyo"

    basin: list[str] = []
    # Only inject the known Phonon basin when this report is already in it
    # (or the learner labeled "loozanyo") — do not ban those strings globally.
    if heard_like and _soft_fold(heard_like) in {
        "loozanyo",
        "luzanyo",
        "luzanyu",
    }:
        basin = [
            "luh-ZAHN-yuh",  # old system-prompt example Phonon misreads
            "luh-ZAN-yuh",
            "la-zan-ya",
            "loo-ZAHN-yo",
            "loozanyo",
            "lazanya",
        ]

    failed = _collect_failed_forms(
        spelling,
        body.spokenForm,
        existing,
        heard_like,
        *basin,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_id = body.id or f"{stamp}-{key}"
    record = body.model_dump()
    record["normalized"] = key
    record["receivedAt"] = datetime.now(timezone.utc).isoformat()
    if heard_like and not body.heardLike:
        record["heardLikeInferred"] = heard_like
    (REPORTS_DIR / f"{report_id}.json").write_text(
        json.dumps({k: v for k, v in record.items() if k != "wavBase64"}, indent=2)
        + "\n"
    )
    if body.wavBase64:
        wav_name = body.wavFileName or f"{report_id}.wav"
        (REPORTS_DIR / wav_name).write_bytes(base64.b64decode(body.wavBase64))

    suggestion = _claude_respell_with_retry(
        spelling=spelling,
        spoken_form=body.spokenForm,
        locale=body.locale or "en-US",
        engine=body.engine,
        voice_id=body.voiceId,
        prior_pack_form=existing,
        failed_forms=failed,
        heard_like=heard_like,
        failure_note=body.failureNote,
        has_audio=bool(body.wavBase64),
    )
    if not suggestion:
        # Do NOT keep a prior form the learner already rejected — that is the
        # loozanyo loop. Drop it so devices fall back to raw orthography and
        # the report button returns.
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
            "detail": (
                "Stored for review; Claude returned no usable form "
                "(rejected echo/loozanyo-shape or no API key). "
                "Prior pack entry cleared."
                if dropped
                else "Stored for review; Claude returned no usable form."
            ),
        }

    # Final gate — never write a loozanyo-shaped form into the pack.
    if _reject_reason(suggestion, failed):
        if existing and _drop_spelling(pack, key):
            pack["packVersion"] = _bump_pack_version(
                str(pack.get("packVersion") or "0")
            )
            pack["hyphenPolicy"] = "ota_corrections"
            _save_pack(pack)
        return {
            "status": "queued",
            "spelling": spelling,
            "speakForm": None,
            "vetted": False,
            "packVersion": pack.get("packVersion"),
            "priorSpeakForm": existing,
            "detail": "Rejected loozanyo-shaped / failed-form echo; prior cleared.",
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
