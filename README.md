# Pronunciation report API (scaffold)

Receives “Doesn’t sound right” reports from WordGarden, stores them, asks
Claude for a Phonon-friendly speak form when the spelling is new, and
publishes an OTA `speak_rewrites_ota.json` pack devices fetch on launch.

## Flow

1. App POSTs JSON (+ optional WAV base64) to `POST /v1/pronunciation-reports`.
2. If `exact[spelling]` already exists in the published pack → mark duplicate;
   devices that missed the pack will pick it up on next OTA fetch.
3. Else call Claude for a short English respelling / hyphenation suitable for
   Phonon 31M (text-only TTS). Prefer dictionary/IPA → speak orthography over
   ad-hoc guesses.
4. Append to pack, bump `packVersion`, write `store/remote/speak_rewrites_ota.json`
   and push the public gist raw file devices already poll.

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

Publish pack (after corrections accumulate):

```bash
python3 publish_pack.py
# then gist-edit speak_rewrites_ota.json like bundled_banks.json
```

## Privacy

Reports include the **app TTS WAV** and the spelling — not the child’s
microphone stream. Update App Store privacy nutrition before enabling
`WG_PRON_API` in release builds.
