# Pronunciation report API

Receives “Doesn’t sound right” reports from WordGarden, stores them, asks
Claude for a Phonon-friendly speak form on every report (including re-reports
of a bad prior fix), and publishes an OTA `speak_rewrites_ota.json` pack
devices fetch on launch.

## How “training” actually works (honest)

**Claude does not hear the WAV.** Anthropic’s Messages API has no audio
modality. The uploaded clip is stored under `reports/` for operators only.
The model is prompted with:

- spelling (orthography)
- `spokenForm` (what Phonon was fed)
- optional `heardLike` / `failureNote` (learner complaint text)
- prior pack form + earlier failed speak forms for that spelling

So this is **text-label retraining**, not audio training. If the prompt
steers Claude into a Phonon failure basin (e.g. the old lasagna example
`luh-ZAHN-yuh` → heard as “loozanyo”), reports keep “correcting” into the
same bad sound until the prompt + server-side reject gates change.

Server gates after Claude proposes:

- reject exact / near-variant echoes of failed forms (hyphen/CAPS/AH↔A folds)
- reject loozanyo-shaped respells (`luh-ZAN-yuh`, `loo-ZAHN-yo`, …)
- on failed retrain, **drop** the prior pack entry (do not keep the bad form)

## Flow

1. App POSTs JSON (+ optional WAV base64) to `POST /v1/pronunciation-reports`.
2. Store the report (+ WAV for operators). Prompt Claude from text labels only.
3. Call Claude for a short American-English respelling suitable for Phonon
   Zoey 31M. System prompt teaches Phonon pitfalls (including: do **not**
   propose `luh-ZAHN-yuh`-style lasagna respells). No hardcoded
   `exact["lasagna"]=…` product override.
4. Write/replace `exact[spelling]`, bump `packVersion`, persist disk pack and
   push the public gist. Re-reports retrain; rejected proposals clear the
   prior entry so devices fall back to raw orthography.

## Run locally

```bash
cd server/pronunciation
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=…
uvicorn app:app --reload --port 8787
```

Point a debug build at it:

```bash
flutter run --dart-define=WG_PRON_API_BASE=http://127.0.0.1:8787
```

Live service: https://wordgarden-pronunciation.onrender.com  
Health: `GET /health` · Pack: `GET /v1/speak-rewrites` · Word: `GET /v1/corrections/{spelling}`  
Un-vet: `DELETE /v1/corrections/{spelling}`

Client dart-define: `WG_PRON_API_BASE=https://wordgarden-pronunciation.onrender.com`

Publish pack (after corrections accumulate):

```bash
python3 publish_pack.py --gist
# gist id: c5fdb854b75d7ab4a82cddfcc83a3b64 (speak_rewrites_ota.json)
```

## Privacy

Reports include the **app TTS WAV** and the spelling — not the child’s
microphone stream. WAV is operator forensic data; Claude never receives it.
