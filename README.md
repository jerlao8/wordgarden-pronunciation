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

## Multi-report threshold

Phonon is usually better than a one-tap Claude rewrite. The API **does not**
publish to the OTA gist / mark `exact`+`vetted` until **≥2 distinct
reporters** agree (env `WG_PRON_REPORT_THRESHOLD`, default `2`).

1. App POSTs JSON to `POST /v1/pronunciation-reports` with `spelling`,
   `spokenForm`, and a stable anonymous `reporterId` (install id).
2. First report(s): store + optionally log Claude → response
   `status: "recorded"` (no `speakForm`; client keeps Phonon).
3. When the threshold is met: Claude speakForm → pack update + gist publish
   → `status: "published"` (or `retrained`) with `speakForm` + `vetted: true`.
4. Operator-locked spellings (lasagna) still publish immediately (`locked`).

Without `reporterId`, each report id counts toward the threshold (MVP
limitation: the same device can inflate the count).

## Flow

1. App POSTs JSON to `POST /v1/pronunciation-reports` (spelling, spokenForm,
   reporterId).
2. Below threshold → `recorded` (Claude logged server-side only).
3. At threshold → Claude `{"speakForm":"..."}` → pack `exact` + `vetted`,
   `packVersion` bumped, gist published.
4. If Claude returns nothing usable at threshold, any prior pack entry for
   that spelling is dropped (`queued`).

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
