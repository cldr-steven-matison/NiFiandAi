# Canvas layout & arrangement

The canonical home for the layout technique — the NiFi REST API build path (`flow-api.md`) and the EFM Designer API build path (`minifi-efm.md`) both point here, because both produce the same problem: a functionally-correct flow that's visually rough. Processors land wherever the call's `position` said, connections cross, and it reads nothing like a hand-laid flow.

### Pre-flight self-check — run this before the first `POST .../processors`

This doc existing wasn't enough on its own: fresh EFM builds skip it and place processors at `(0,0)`/`(400,0)` — the exact flagged-bad shape below. Before you send a single processor-create/update call, answer these out loud (wiring a PreToolUse hook to prompt for them helps):

1. **Which build path?** EFM Designer API → use the roomier EFM pitches. NiFi REST API → use the NiFi pitches. Crossing them is the most common miss.
2. **Which shape?** Linear chain / branch-fanout / join / parallel-independent-lanes. Default a linear chain to **vertical** (constant x, `y +=` pitch), not sideways.
3. **Which pitch numbers?** State them. EFM linear: row **300**. EFM branch/column: **~600–900**. NiFi linear: row **200**, branch **±300**. If your numbers don't match the per-shape rule below, fix them before the call, not after review flags it.

**This check applies to every `POST .../processors` call, including a single diagnostic or error-log processor bolted onto a live flow mid-debug — not just deliberate from-scratch builds.** It's easy to treat a quick "add a `LogAttribute` to see what's failing" add as exempt because it feels temporary/throwaway, and eyeball an offset instead of running the check. It isn't exempt: it still lands on the canvas at whatever `position` you send, and "it's just a diagnostic" is not a reason it gets to skip the pitch. (Incident: an error-log `LogAttribute`, added mid-debug to catch `InvokeHTTP`'s failure relationships, landed at only +400 from center on an EFM build — under the 600 floor — because the check was run for the deliberate rebuild passes that session but not for this one-processor add.)

**Read the rest, because it sets the honesty bar:** the technique below gets a build *close* to hand-laid, but it does **not** eliminate the manual align/tidy pass in the Designer or NiFi UI. Even with role-matched columns and consistent rows, connections still cross and it won't look finished. Don't claim a build is visually done — say what it functionally does, and expect (or explicitly ask about) a cleanup pass. Good coordinates alone are not enough; a role-matched, consistently-spaced build still gets a human processor-sliding pass afterward.

### Coordinate model (same for both build paths)

NiFi canvas and EFM Designer share one position model: each component has `position:{x, y}`, origin **top-left**, +x right, **+y down**. A flow reads **top-to-bottom** — source at the top (lowest, sometimes negative y), sinks at the bottom. So the technique here is identical whether you're building via the NiFi REST API or the EFM Designer API.

### Grounded constants

These are read off real hand-tidied flows, not invented — re-derive from your own tidied flows with `jq '.. | objects | select(has("processors")) | .processors[] | {name, x:.position.x, y:.position.y}'` if in doubt. **They were all read off NiFi-UI-tidied flows**, so they're the right defaults for a NiFi REST API build. The EFM Designer canvas needs more — see the note after this list.

- **Row pitch (one stage to the next): 200 default**, down to **150** for dense flows. A linear chain steps 200 (y = 0, 200, 400, 600); a dense live-check subchain steps 150 (600 → 750 → 900 → 1050 → 1200).
- **Center column: x = 0.** The spine of a linear chain sits at x = 0.
- **Branch column pitch: ~300 (dense) to ~480 (roomy).** A two-way split sits at x = −300 / +300; a three-way fan-out sits at −488 / 0 / 480.

**EFM Designer builds are roomier in both axes — the NiFi defaults read as cramped.** The constants above are NiFi-UI-derived and read fine on the NiFi canvas — leave them alone for NiFi REST API builds. But the EFM Designer renders processor boxes such that the NiFi pitches stack components too close to read, **vertically and horizontally**. Grounded observation: a clean linear chain built via the EFM Designer API at the 200 row pitch (y = 0, 200, 400, 600) — functionally correct, following the NiFi default exactly — came back from review as visibly cramped.

Bump both, but **the horizontal needs to grow quite a bit more than the vertical** — EFM's processor boxes are far wider than they are tall, so columns run into each other long before rows do:

- **Row pitch: 300**, not 200 (y = 0, 300, 600, 900); dense flows 225 rather than 150.
- **Branch column pitch: ~600 (dense) to ~900 (roomy)**, not ~300/~480 — a two-way split lands at x = −600 / +600, a three-way fan-out around −900 / 0 / +900. This is ~2× the vertical pitch as the per-column offset (columns ~1200 apart for a two-way), deliberately wider than the ~1.5× the rows get.

This is EFM-Designer-only — it does **not** change the NiFi defaults.

**Field-validated against live EFM flows.** A human-tidied flow of 3 parallel vertical chains measures row pitch ≈300 (296–315) and column pitch ≈715 — confirms the numbers above for a **vertical chain** shape. A flow with 2 processors at `(0,0)` → `(400,0)` isn't even on the vertical axis — see the axis note below. A human-tidied parallel-pipeline flow uses a shape the row/column split above doesn't cover at all — see "Parallel independent pipelines" below.

### Per-shape placement rules

Confirm the axis before tuning the pitch — a bigger row/column number only helps if the chain is already stepping the right one. A two-processor flow at `(0,0)` → `(400,0)` is x-incrementing with constant y: a sideways linear chain, not a cramped vertical one. Default every linear chain to vertical (constant x, `y +=` pitch) unless deliberately building the row-based shape below. Fresh EFM Designer builds repeatedly re-attempt this exact `(0,0)` → `(400,0)` mistake and fail visual review — a two-processor `GenerateFlowFile → LogAttribute` should be vertical (`(0,0)` → `(0,300)` on EFM).

- **Linear chain** — same x, `y += 200` each step (e.g. `ListenHTTP` 0,0 → `RouteOnAttribute` 0,200 → …). **On an EFM Designer build use `y += 300`** per the pitch note in "Grounded constants" above.
- **Branch / fan-out** (a `RouteOnAttribute`/`RouteOnContent` feeding N targets) — router stays at center; the N targets **share one row** (`y = router.y + 200`) and spread symmetric `x = center ± pitch`. Odd count keeps one target on center (−pitch, 0, +pitch); even count straddles (−pitch/2, +pitch/2 or ±pitch).
- **Join / merge** (funnel, or several branches into one processor) — the merge target sits at center **x = 0**, one row below the lowest branch row.
- **Self-loop** (e.g. `Retry` self-loop on `InvokeHTTP`, rule 7) — leave the processor where it is; the loop renders as a small bend, no new column needed. Route the terminal `Failure`/`No Retry` to a log processor one row down — e.g. a `LogFailure` sink at (0, 600). **If that same processor also has a real success continuation** (e.g. `InvokeHTTP`'s `Response` feeding `HandleHttpResponse`, not just terminating), that's a two-way fan-out, not a pure self-loop: apply the branch rule above, not this one. Keep the success continuation on the center column (it's the spine a reader's eye follows), and push the error/log sink out by a full branch-column pitch (±300/480 NiFi, ±600/900 EFM) on the **same row** as the continuation — see the pre-flight check note above for the incident this came from.
- **Pre-source timers** (a `GenerateFlowFile` timer or roster fetch ahead of the real source) — negative y, above the source (e.g. a poll-timer `GenerateFlowFile` at y = −264, a roster-fetch `InvokeHTTP` at −120).
- **Parallel independent pipelines (EFM Designer only)** — N short, mostly-unrelated pipelines (e.g. `ListenHTTP → EvaluateJsonPath → InvokeHTTP` repeated per pipeline) that only converge at one shared downstream sink. Don't stack them into one tall vertical chain — lay each pipeline out as its own **horizontal row** (source on the left, reading left-to-right, each role sharing one column per "Deriving from a live flow" below), then stack the rows top-to-bottom. Grounded off a real parallel-pipeline flow: lane-to-lane pitch ≈190–200 (tighter than the 300 stage pitch above — there's no connecting line to clear between lanes), stage-to-stage column pitch ≈600 (the same branch-column number, but here it's the main reading-direction pitch, not a spread), and the shared merge sink pushed a further ≈950–1000 out past the last per-lane column to leave room for the multi-way fan-in.

### Inserting a new node into an existing connection

Splitting an existing `A → B` connection to add a new hop (`A → C → B` — e.g. adding a formatting step ahead of a processor that already existed) is a different problem from placing a fresh node, and the rules above don't cover it on their own. **Don't put `C` at the midpoint of `A` and `B`'s existing y-values.** That compresses the row pitch for exactly one hop and desyncs it from every parallel column that still uses the original pitch.

Instead:
1. Give `C` a full row pitch below `A` (`C.y = A.y + row_pitch`, same `row_pitch` already established in this column/flow — see "Deriving from a live flow" below).
2. Push `B` (and everything already below it in the same column) down by one more `row_pitch` to make room, rather than shrinking the gap.
3. If parallel columns share rows (a common pattern — a "success" and "cleanup" branch sitting side by side), keep them aligned: `C` and `B` should land on the same rows as whatever already occupies those rows in the neighboring column, not just "however far apart is convenient" for this one column in isolation.

Example — inserting `BuildJoinedEvent` between `JoinAndGreet` (y=824) and a pre-existing `PublishKafka_2_6` (y=1016), in a column whose parallel branch steps 824 → 1016 → 1208 (~192px pitch). **Wrong:** the midpoint y=920 — it compresses that one hop to under half the column's own pitch and desyncs it from the parallel branch. **Right:** `BuildJoinedEvent` → 1016 (takes `PublishKafka_2_6`'s old row, aligned with the neighbor's `BuildRemoveBody`), and `PublishKafka_2_6` pushed down to 1208 (aligned with `RemoveFromWatchlist`).

### Deriving from a live flow (the precise "match the existing column")

When you're adding to a flow that already exists, don't pick fresh numbers — inherit them. This is rule 1 (live state is truth) applied to layout:

1. Dump the live flow and read `position.x` grouped by processor role/type — the flow already has a de-facto column layout (all `ListenHTTP`s at one x, all `InvokeHTTP`s at another).
2. Reuse that role's x for your new processor of the same role.
3. Set y to the next free row below the existing chain.

### Worked example

A `ListenHTTP → EvaluateJsonPath → RouteOnAttribute → {InvokeA, InvokeB} → LogFailure`, using the constants above:

```
ListenHTTP         (0,   0)
EvaluateJsonPath   (0,   200)
RouteOnAttribute   (0,   400)
InvokeA  (-300, 600)      InvokeB  (300, 600)     ← branch row, symmetric ±300
LogFailure         (0,   800)                     ← merge, back on center
```

These are the NiFi pitches (row 200, branch ±300). For the same shape on an **EFM Designer** build, row 300 / branch ±600 (horizontal pushed out further than vertical): `ListenHTTP` (0,0), `EvaluateJsonPath` (0,300), `RouteOnAttribute` (0,600), `InvokeA` (−600,900) / `InvokeB` (600,900), `LogFailure` (0,1200).

## Other human-pass gaps

Layout is the biggest thing a programmatic build gets functionally-right-but-not-done, but it isn't the only one. This is the running list of everything else a human has had to clean up by hand after an API build — read it before claiming a build is "finished," and add to it the next time something new turns up.

- _(nothing else logged yet — add the next one here)_
