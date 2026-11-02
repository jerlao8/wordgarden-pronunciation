# Pronunciation report API (scaffold)

Receives “Doesn’t sound right” reports from WordGarden, stores them, asks
Claude for a Phonon-friendly speak form on every report (including re-reports
of a bad prior fix), and publishes an OTA `speak_rewrites_ota.json` pack
devices fetch on launch.

## Flow

1. App POSTs JSON (+ optional WAV base64) to `POST /v1/pronunciation-reports`.
2. Store the report (+ WAV for operators). Anthropic cannot hear audio — the
   prompt states that and trains from spelling + `spokenForm` (+ prior pack
   form when retraining).
3. Call Claude for a short American-English respelling / hyphenation suitable
   for Phonon Zoey 31M (text-only TTS). System prompt teaches loanword
   pitfalls (e.g. lasagna → luh-ZAHN-yuh pattern, not loozanyo); no hardcoded
   per-word overrides in the pack from operators.
4. Write/replace `exact[spelling]`, bump `packVersion`, persist disk pack and
   push the public gist raw file devices already poll. Re-reports retrain.

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
flutter run --dart-define=WG_PRON_API=http://127.0.0.1:8787/v1/pronunciation-reports
```

Live service: https://wordgarden-pronunciation.onrender.com  
Health: `GET /health` · Pack: `GET /v1/speak-rewrites` · Word: `GET /v1/corrections/{spelling}`

Client dart-define: `WG_PRON_API_BASE=https://wordgarden-pronunciation.onrender.com`

Publish pack (after corrections accumulate):

```bash
python3 publish_pack.py --gist
# gist id: c5fdb854b75d7ab4a82cddfcc83a3b64 (speak_rewrites_ota.json)
```

## Privacy

Reports include the **app TTS WAV** and the spelling — not the child’s
microphone stream.
