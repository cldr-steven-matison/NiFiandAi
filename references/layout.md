# Canvas layout & arrangement

One home for layout on both build paths. The NiFi REST API (`flow-api.md`) and the EFM Designer API (`minifi-efm.md`) produce the same problem: a flow that works but reads badly. Processors land wherever the call's `position` said, connections cross, and nothing looks hand-laid. Positions in an uploaded flow JSON are baked in before the upload, so layout happens **before** the call, never after.

## Pre-flight — answer these before the first create or upload

The rules below were known and still skipped: an injected reminder got read as background, a sub-agent got a one-line "keep it tidy", a rewire left ports 4,000 px from their PG. So this is a step you run, not context you skim.

1. **Build path?** NiFi REST → NiFi pitches. EFM Designer → EFM pitches. Crossing them is the most common miss.
2. **Shape?** Linear chain, branch/fan-out, join, parallel lanes, or a **root/PG canvas** of groups. Default a linear chain to **vertical**: constant x, `y +=` row pitch.
3. **Pitch numbers?** State them. NiFi: row **200**, branch **±600**. EFM: row **300**, branch **±600**. PG canvas: column **~664**, row **~272**. A two-way OK/Error split is a branch and gets the full pitch.
4. **Direction?** Inside a chain, every connection points down or level.
5. **Beside existing work?** State the canvas's current extents, and where the new block goes relative to what it is **wired to**.
6. **Check it.** Run `scripts/check_layout.py <flow.json>` before any upload, and on any flow a sub-agent hands back. It exits non-zero on a FAIL. Fix the layout, don't argue with the checker.

**No exemption for small adds.** A single diagnostic `LogAttribute` bolted on mid-debug still lands at the `position` you send. Run the pitch for it too.

**Honesty bar.** Good coordinates get a build close to hand-laid, not to finished. Say what the flow does, run the checker, and expect a human sliding pass. Never claim a build is visually done.

## The canvas NiFi actually draws

Origin top-left, +x right, **+y down**, the same on NiFi and EFM. A flow reads top to bottom. The NiFi 2.x frontend's own box sizes (width × height), which every pitch below is built from:

| Component | Box |
|---|---|
| Processor | 352 × 128 |
| Input / output port | 240 × 48 (remote port 240 × 80) |
| Funnel | 48 × 48 |
| Process group, remote process group | 384 × 176 |
| Label | its own `width` × `height` (UI default 148 × 148, min 64 × 24) |
| Connection label | 240 wide |

The UI snaps hand moves to an **8 px grid**. A hand-tuned flow reads 192/208/632 where a generator writes 200/600. Either is fine; just don't mix both in one column.

**NiFi's own layout warning (2.9+).** The canvas shows a banner, "N overlapping connection group(s) detected", when **two or more connections join the same two components (either direction) and none of them has a bend**. They draw as one line. The fix is **one connection carrying every relationship** between that pair (e.g. `["failure", "No Retry"]`). Only if they must stay separate, give every connection but one a bend. The UI's own avoidance bend sits at the midpoint, offset ±75 in y (or ±250 in x for a steep line). The check is per PG, client-side, and nothing server-side rejects an import that trips it. It surfaces the first time someone opens the PG.

## Inside a process group

### Constants

- **Row pitch 200** (dense 150) on NiFi; **300** (dense 225) on EFM Designer. A 128-tall processor at 200 leaves a 72 px gap, which is where a section caption sits.
- **Spine at x = 0.**
- **Branch pitch ±600** (roomy ±900) on both canvases. A processor is 352 wide, so 600 leaves 248 between neighbours: room for one 240-wide connection label. Anything under that reads as one box.

### Shapes

- **Linear chain:** same x, `y += row`.
- **Branch / fan-out:** the router stays on the spine. Its N targets share one row (`router.y + row`) at `x = ±pitch` (odd N keeps one on the spine). **Every same-row split gets the full pitch**, a bare OK/Error pair off any processor included.
- **Join / merge:** the merge target goes back on the spine, one row below the lowest branch.
- **Self-loop** (`Retry`, skill rule 7): stays in place, no new column. If the processor also continues (`InvokeHTTP` `Response` → next), it is a fan-out: the continuation stays on the spine, the error sink goes one branch pitch out on the **same row**.
- **Pre-source timers** (`GenerateFlowFile`, roster fetch): negative y above the source, declared at build time.
- **Parallel independent lanes (EFM):** each lane is its own horizontal row, source on the left; lanes ~200 apart, stages ~600 apart, the shared sink ~1000 past the last column.
- **Ports:** next to what they connect, never laid out as a chain root of their own. An input port sits one row above the stage(s) it feeds, centred over them, or the nearest free slot on that row; a port that joins mid-flow (a reply coming back in) sits above that stage, not at the top of the canvas. A collecting output port sits below or beside its legs.

### Direction and sprawl

1. **Route down, never up.** Every processor/funnel → processor/funnel connection points down or level. If a destination "must" be above, move it. A deliberate loop back (pagination, a retry leg) is the exception: give it a bend, and pass `--allow-upward "Src->Dst"` to the checker so it is declared, not accidental.
2. **Add down, never up.** New processors extend a chain below the existing ones, never squeezed above.
3. **New work goes right, clear of existing work.** Read the extents first (`max(position.x)`), leave at least one branch pitch of clear space, never interleave columns, never start at `(0,0)` because the call defaulted there.
4. **Inserting into `A → B`:** give `C` a full row below `A` and push `B` and everything under it down one row. Never the midpoint. Keep rows aligned with parallel columns.
5. **Match the existing column.** Adding to a live flow: dump it, reuse the x already used for that processor role, take the next free row.

### Failure sinks — dev, then prod

`LogAttribute` gets overdone. Sinks are a **dev tool with a prod pass**, never one shared sink across the canvas.

- **Dev build:** one log sink **per stage**, on its source's **row**, one branch pitch outboard (spine 0 → sink 600), on the **side its source sits**: a branch left of the spine gets its sink further left, never across the spine. A sink may serve a contiguous run of stages, never rows far apart (the checker warns past 600 px). A shared sink sits on the row of its **lowest** source, so every line into it runs down or level. Set it to log at warn with the payload, `success` auto-terminated. Every failure is visible next to where it happened.
- **Prod pass:** auto-terminate most of them. NiFi's bulletin, `nifi-app.log` and the DROP provenance event are the record. **Keep** the sinks that capture a key failure. **Route** a failure that someone must act on to a notification/email PG instead of a log. `check_layout.py --profile prod` lists the sinks to decide on.
- **Never auto-terminate `Retry`** (rule 7). A failed leg that must still answer an HTTP client goes to a responder, not a sink.
- A **test funnel** is for a debugging session only: one funnel, many connections in, removed after.

### Labels

- **One master label at the top of every PG** (14 px), above every component. It summarises the whole flow: what triggers it, what it does stage by stage, what it emits, where it fails to. It is the first thing a reader sees on opening the PG.
- **Section captions where they help** (12 px, ~560 × 60, about three lines). Put each one in the gap above the section's first processor (`y − 66` at row pitch 200).
- A label may sit behind a whole lane of components, but never **partly** over a box. No colours needed.

## The root canvas and other PG-level canvases

Groups, ports and remote process groups follow different rules from a processor chain. Hand-tuned group canvases are **hub-and-spoke**, not a grid.

- **The hub** (the remote process group, or the bridge PG everything talks to) sits at one side. Its spokes form columns **~664–680** apart (a 384-wide box plus a ~280 gap) and rows **~200–288** apart (a ~96 gap under a 176-tall box). Related groups share a column.
- **Place a new group next to what it is wired to.** Not at `max(x) + 900`: the checker fails any group or RPG connection longer than 1,250 px, and that default always is.
- **Rewire rule: connecting an existing port or group to a new or moved group means re-placing it next to that group.** For example, root S2S ports go centred under the PG they feed, one branch pitch apart.
- **Upward edges are fine between groups.** The "route down" rule is for processor chains.
- **Two connections between the same two groups** still need a bend each but one (NiFi's warning above).
- Unwired neighbours (a column of independent PGs) may sit closer than the label gap. Wired neighbours may not.

## Delegating a flow build

A sub-agent building a flow gets the pre-flight numbers in its prompt (path, shape, row and branch pitch, sink profile, where the block goes), not "keep the canvas tidy". It runs `check_layout.py` before handing back, and the parent **re-runs it** before accepting, the same as it would re-run a validator for wiring or secrets.

## Worked example

`ListenHTTP → EvaluateJsonPath → RouteOnAttribute → {InvokeA, InvokeB} → Respond`, a dev build with stage sinks:

```
[master label: "Webhook router — ListenHTTP takes ..., routes on ..., calls A or B, answers ..." ]   (0, -400)
ListenHTTP         (0,    0)
EvaluateJsonPath   (0,  200)      LogParseFailure   (600, 200)
RouteOnAttribute   (0,  400)
InvokeA  (-600, 600)              InvokeB  (600, 600)       ← branch row, ±600
Respond            (0,  800)                                ← merge back on the spine
```

`InvokeA`/`InvokeB` route `["failure","No Retry"]` as **one** connection each to a sink on their row, one pitch further out (−1200 / 1200), and self-loop `Retry`. On EFM only the row pitch changes: y = 0, 300, 600, 900, 1200.
