# Pronunciation report API

Receives “Doesn’t sound right” reports from WordGarden, asks Claude for a
Phonon-friendly speak form, and publishes an OTA `speak_rewrites_ota.json`
pack devices fetch on launch.

## Training (honest)

Claude does **not** receive audio. The prompt is spelling + failed
`spokenForm` (+ locale/engine). Claude already knows US pronunciations and
proposes a short text respell optimized for grapheme-to-phoneme-naive TTS
(plain lowercase; avoid dictionary CAPS / AH-UH codes). No WAV upload; no
per-word phonology rules in the system prompt. Reports log `claudeRaw` +
`spokenForm` under `/var/data/reports/` and stdout (`WG-PRON report …`).

## Flow

1. App POSTs JSON to `POST /v1/pronunciation-reports` (spelling, spokenForm).
2. Claude returns `{"speakForm":"..."}`.
3. Pack `exact[spelling]` is updated, `packVersion` bumped, gist published.
4. Re-reports always retrain. If Claude returns nothing usable, any prior
   pack entry for that spelling is dropped.

## Run locally

```bash
cd server/pronunciation
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=…
uvicorn app:app --reload --port 8787
```

Live: https://wordgarden-pronunciation.onrender.com  
Deploy repo: https://github.com/jerlao8/wordgarden-pronunciation
