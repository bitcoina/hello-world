# Barometer

A private, single-user app for a dedicated Raspberry Pi. You write (or say) quick notes about family and friends; Barometer turns them into a structured record and, before an event, gives you a short brief: who wants what, where the fault lines are, what to avoid.

This repo implements the Barometer spec end to end, covering milestones M1–M5. The build follows the spec's principles: capture is effortless, raw notes are immutable, extraction auto-commits with a one-paragraph readback, facts and judgements stay apart, everything is isolated on its own device, and forgetting is a feature.

## Decisions taken while building

| Open question in the spec | Decision |
| --- | --- |
| Is sending note text to a hosted API acceptable? | **Yes, the Claude API** (`claude-opus-5-5`). Names are swapped for refs (`P12`) before text leaves the Pi wherever a name is unambiguous, and mapped back on return. |
| What happens to a note captured while Deep is locked? | **Extract immediately, in memory.** The note is sealed to the Deep public key the moment it arrives, so it survives a reboot. Extraction runs straight away using only the Almanac's context. Shareable facts (people, dates, follow-ups, gifts, events) are filed at once; the political part is sealed and applied on the next unlock. The plaintext stays in RAM only until the session idles out, so you can still correct the readback. |
| Hidden entrance or visible lock? | **A visible "Private" lock** in the header. Long-pressing the logo also opens it. |

## What's here

```
barometer/
  app.py            FastAPI routes, security headers, CSRF header check
  core.py           Wiring and housekeeping: idle lock, weekly push, nightly backup
  config.py         Settings from BAROMETER_* env vars (or data/.env)
  crypto.py         Argon2id-wrapped X25519 key, sealed boxes, password hashing
  almanac.py        The shareable tier (SQLite, optional SQLCipher)
  deep.py           The private tier: encrypted SQLite image, unlocked into memory
  db.py             Schemas for both stores, provenance helpers
  schemas.py        Strict JSON schemas for every LLM call
  llm.py            Claude client: structured outputs, validate, retry once, refusal fallback
  pseudo.py         Names <-> refs for prompts
  extraction.py     Extraction prompt, index, semantic validation
  commit.py         Resolve people, write with provenance, undo, readback
  pipeline.py       Capture -> transcribe -> extract -> commit; locked-mode sealing and drain
  graph.py          Interactions -> alliances/tension (6-month half-life), triads, source grades
  retrieval.py      Context assembly; Brief, Explain, Record check, Reflect; map; person readings
  relationships.py  Key dates, follow-ups, cadence nudges, dormant ties, Sunday digest, ledger
  mirror.py         Mirror caps: weekly cap, 30-day mute after 3 dismissals, rating-tuned threshold
  forget.py         Forget a person: delete rows, redact names everywhere, vacuum
  voice.py          faster-whisper or whisper.cpp; audio shredded after reading
  backup.py         Backups sealed to an off-Pi public key; restore and verify
  golden.py         Golden extraction set runner
  cli.py            `barometer init | serve | golden | backup | restore | wipe | ...`
  static/           The PWA: vanilla JS modules, CSS tokens for both tiers, vendored fonts and Cytoscape
golden/cases.jsonl  10 golden notes
deploy/             systemd unit, env template, Tailscale ACL, firewall, LAN check, installer
scripts/demo_seed.py  A fictional family for trying the UI without an API key
tests/              52 tests, including the acceptance checks
```

## How it maps to the spec

**Two tiers, two stores.**
- The **Almanac** (`almanac.db`) holds people, family links, key dates, follow-ups, gifts, contacts, events and the digest. Partner logins see only this, through forms for dates and gifts. They never get free-text capture.
- **Deep** (`deep.db.enc`) holds raw notes, interactions, trust, claims, topics and frames, motives, self-observations, assessments, briefs and the Mirror log. At rest it is one SecretBox-encrypted SQLite image. Its key derives from an X25519 private key, which is itself wrapped with Argon2id(passphrase). The Pi never stores the passphrase or the unwrapped key. On unlock, the image is decrypted into an in-memory SQLite database, and every write re-encrypts and atomically replaces the file.
- **Writing while locked.** Captures are sealed with the public key into the Almanac's `deep_inbox`, and drained on the next unlock.

**Capture and extraction.**
- One text box, a hold-to-talk button and an optional event tag. Saving never blocks.
- Extraction returns strict JSON covering people, interactions, edges, trust, claims, topics/frames, motives, key dates, gestures, contacts, self-observations, assessments, profiles, an event and the Mirror.
- Output is validated twice: against the schema (structured outputs) and semantically (every ref exists, dates are real, character judgements go only to assessments). Failures are retried once. Nothing partial is ever written.
- Every derived row carries `source_note_ids`. One note means provisional; two or more means corroborated.
- **Undo** removes the note's support everywhere and deletes rows left with none. **Correct** re-extracts with your words and stores the result as a golden case.
- At most one question per note, in priority order: who is this (shared or near-identical names), a contradiction of a corroborated relationship, or whether a relayed claim is now confirmed.

**Political graph.**
- Alliance and tension are never stored. They are computed on read from the interaction log, with each interaction decayed by a six-month half-life and weighted by status. Each pair also gets a 90-day trend (warming, steady or cooling) and a weather word (Fair, Settled, Changeable or Stormy).
- Trust is directional and per domain.
- Sources get Admiralty-style grades A–F, and claims get credibility 1–6, updated when you answer the confirm/contradict question.
- Unbalanced triads appear in briefs as "Low pressure".
- Topics hold competing frames, and each person–topic link records their frame and how much they care.

**Retrieval.**
- A drop zone for people and event chips (drag on desktop, tap on a phone), four modes, and a Deep-brief toggle that draws on assessments for that one query.
- Context assembly includes "brokers": people who weren't selected but are connected to two or more of those who were. Notes are ranked by recency and how charged they are, and fit to a token budget. Older notes are summarised rather than dropped, and the brief says how many.
- Every brief claim must cite note IDs taken from the context. Uncited or invented citations are dropped and counted. Tapping a citation opens the note.
- A saved brief logs its prediction, which Today asks you to score afterwards.

**Relationships.**
- Follow-ups are created automatically from one-off dates; a hospital visit, for example, gets "ask how it went" two days later.
- Cadence follows the layer (5/15/50/150) set with one tap.
- The Sunday digest has at most three nudges. Once a month the app picks one dormant tie for you.
- There is a gift log and an initiation ledger.
- Lapses cost nothing: follow-ups older than two weeks fall away quietly, and nudges are regenerated weekly rather than piling up.

**Mirror.**
- The Mirror can speak at capture (when you're planning a moral-flex argument), in briefs (when someone present has low tolerance for moral relativism, or a high-sensitivity topic is live), and in Explain/Reflect.
- It gives at most one line per brief, with a weekly cap across the app.
- Three dismissals of the same pattern mute it for 30 days. "Useful / not useful" ratings move its threshold.
- It never comments on other people's morals.

**Security.**
- The app binds to `127.0.0.1`. `tailscale serve` provides HTTPS on your tailnet only, and the firewall drops everything that doesn't arrive over `tailscale0`.
- Login passphrases are hashed with Argon2id, with back-off on failures. Sessions are server-side and idle out after 10 minutes, which also locks Deep. There is no "remember me".
- Cookies are `SameSite=Strict` and `HttpOnly`, and every write needs a custom header. The CSP is strict: no inline script or style, and no third-party origins (fonts and Cytoscape are vendored).
- Double-tap the logo to close Private and land on a blank note.
- One neutral push a week via your own ntfy server.
- Forget person deletes the person and redacts their name in every text column of both stores, then vacuums. The tests check the raw bytes.
- `barometer wipe` deletes everything.

## Where it differs from the letter of the spec, and why

- **Deep uses an encrypted SQLite image plus libsodium instead of SQLCipher.** The spec asks for both "encrypted with a passphrase-derived key never stored on the Pi" and "writing while locked via public-key encryption". SQLCipher can't accept writes without its key, so the sealed inbox plus an encrypted image is the simplest design that meets both. It needs only PyNaCl, which has Pi wheels. The Almanac can still use SQLCipher (`BAROMETER_ALMANAC_KEY_FILE`); LUKS covers it otherwise.
- **The nightly triad scan runs on demand instead.** Deep is locked at rest, so a nightly job couldn't read it. Triads, decay and trends are computed whenever a brief, map or person page asks, so they're never stale.
- **The Reflect chart is a small hand-written SVG, not a chart library.** That's one less dependency for one line chart.

## Run it on the Pi

1. Raspberry Pi OS Bookworm (64-bit), SSD, ideally LUKS-encrypted, with SSH key-only login. Install Tailscale and run `tailscale up --advertise-tags=tag:barometer`. Apply `deploy/tailscale-acl.hujson` in the admin console.
2. `sudo deploy/install.sh` creates a `barometer` user and a venv, installs the app with voice support, runs `barometer init` (owner login + Private passphrase), installs the systemd unit and starts `tailscale serve`.
3. Edit `/srv/barometer/data/.env` (template: `deploy/env.example`) to add `ANTHROPIC_API_KEY`, and optionally ntfy and backups. Then `sudo systemctl restart barometer`.
4. `sudo deploy/firewall.sh`, then run `deploy/check-lan-isolation.sh` on the Pi and `deploy/check-lan-isolation.sh <pi-lan-ip>` from another machine on the LAN.
5. On your phone, open `https://<pi>.<tailnet>.ts.net/` and choose "Add to Home Screen".

Add a partner's Almanac login with `barometer add-user <name>`. Change passphrases with `barometer passwd <name>` and `barometer deep-passphrase`.

**Backups.** On your laptop (not the Pi), run `barometer backup-keygen` and keep the private key there. Put the public key in `.env`, with `BAROMETER_BACKUP_DIR` pointing at a second drive or a mounted share. Backups run at 03:00 and the last 14 are kept. Check them regularly, off the Pi: `barometer restore-test <file>`.

## Develop

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                      # 52 tests, including the acceptance checks
barometer golden            # golden set against the real model (needs ANTHROPIC_API_KEY); exits 1 below 90%
barometer golden --include-corrections   # also replays corrections you've made in the app

python scripts/demo_seed.py ./demo-data # a fictional family, no API key needed
BAROMETER_DATA_DIR=./demo-data BAROMETER_LLM=fake BAROMETER_SECURE_COOKIES=0 barometer serve
# open http://127.0.0.1:8000  ->  demo / demo-login-pass, Private passphrase "demo private passphrase"
```

`BAROMETER_LLM=fake` uses a crude stand-in model, so you can click through without an API key. Real readbacks and briefs need Claude.

## Acceptance checks

| Check | Where |
| --- | --- |
| Any note's extraction can be corrected or undone from its readback in one action | `tests/test_pipeline.py::test_undo_is_one_action_and_note_stays`, `test_correction_replaces_and_becomes_golden_case`, `tests/test_api.py::test_owner_flow_capture_readback_correct_undo` |
| Every Brief claim links to a source note | `tests/test_api.py::test_brief_cites_every_claim_and_hides_names` |
| "Forget person" leaves no trace in People, Edges or note text | `tests/test_forget_and_backup.py::test_forget_person_leaves_no_trace` (checks the raw database bytes) |
| App is unreachable from the home LAN without Tailscale | `deploy/check-lan-isolation.sh` (run on the Pi and from the LAN) |
| Golden test set runs with one command | `barometer golden` |
| M1: a note survives a reboot | `tests/test_storage.py::test_note_survives_restart` |

Still to measure on the real hardware: M2's "≥ 90% of golden notes need no correction" (run `barometer golden` with your API key), M3's "5-person brief in under 20 seconds" (briefs default to `effort=low`), and M4's "60-second note transcribed in under 60 seconds" (faster-whisper `base`, int8, on a Pi 5).

Per the spec, prune any field you haven't used for a decision in three months.
