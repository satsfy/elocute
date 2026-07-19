# Loudreader — build report

What this is: the standalone, completely free spin-off of inquisitor's
/browsertts reader. Server stores text only; the visitor's browser does all the
speaking via the Web Speech API, so hosting costs are just a small container
and a Postgres database.

## Decisions

- **Anonymous-first identity.** Every visitor gets a random id in the signed
  session cookie; books belong to it. Register/login *claims* those rows
  (`UPDATE ... SET user_id WHERE anon_id = ...`), so trying the app before
  signing up never loses anything. No email verification — this is a personal
  free service, not a business.
- **Text pipeline reused verbatim** from inquisitor's `narrator-gpu/bookprep.py`
  (PDF header/footer cleanup, hyphenation, TOC chapters, EPUB spine walk,
  covers). It is CPU-only, so it moved in with zero changes beyond the
  docstring.
- **Speech engine ported with all its scars**: sentence-chained utterances
  (Chrome long-utterance cutoff), chainSeq token gating every callback (engines
  double-fire end/error, which forked interleaved readings), one retry per
  sentence (espeak hiccups), heartbeat position saves every 3s (engines without
  boundary events), visibilitychange save (mobile tabs skip beforeunload).
- **Postgres on serverB** (`192.168.0.5:5432`, database `loudreader`), the
  shared app instance, reached from serverD over the LAN. Tables auto-create;
  `db.init()` never raises so the app serves even with the DB down.
- **Deploy**: plain compose stack at `serverD:/home/renato/loudreader`, host
  port `10125`, public as `loudreader.satsfy.xyz` on serverD's fleet-d tunnel.
  Bookkeeping in `~/infra/placement/manifest.json` + regenerated ingress.

## Verified

- Local smoke against the real DB: import (anon) -> list -> chunk text ->
  position -> register claims books -> fresh anon isolated -> demo book ->
  delete.
- See the repo history for the build order.
