---
name: nifi-and-ai
description: Build, deploy, and debug Apache NiFi 2.x, MiNiFi (C++/Java), and EFM data flows — programmatically via the REST API, as custom Python/Java processors, or as edge agents on Kubernetes — including LLM/RAG inference patterns (Kafka, Whisper, embeddings, vector stores). Use when wiring a NiFi flow, deploying a MiNiFi agent, writing a custom processor, exposing NiFi as an HTTP API, or debugging silent data drops, corrupted sensitive properties, or flow-definition uploads.
---

# NiFi + AI flow playbook

A working playbook for building **NiFi 2.x + MiNiFi + EFM** flows programmatically and agentically — on Kubernetes and at the edge. Each rule and pattern here is the distilled version of a real bug that cost real time. If you're wiring a flow, deploying an agent, writing a custom processor, or debugging why one silently drops data, the pattern is below.

**Conventions used in every example:**
- `$NS` — the Kubernetes namespace NiFi runs in.
- `<nifi-pod>` — the NiFi pod (e.g. a StatefulSet's `-0` pod).
- `$NIFI` — the NiFi API base, e.g. `https://<host>:8443` (the API lives at `$NIFI/nifi-api/...`).
- `<external-nodeport>` — Kafka's external NodePort (only relevant when a flow runs *outside* the cluster).
- Self-signed TLS is assumed by default, hence `-k` / `verify_ssl=False`. Drop it once you've wired a real cert.

## The 10 rules — read before touching any live flow

1. **Live UI / `flow.json` is truth. Docs and memory lag.** Before touching a running Process Group, dump the live flow and read what's actually there:
   ```bash
   # Ask the pod where its flow lives - do NOT hardcode the directory (see below).
   NIFI_HOME=/opt/nifi/nifi-current
   FLOW=$(kubectl exec <nifi-pod> -n $NS -c nifi -- \
            sed -n 's|^nifi\.flow\.configuration\.file=\./||p' $NIFI_HOME/conf/nifi.properties)
   kubectl exec <nifi-pod> -n $NS -c nifi -- gunzip -c "$NIFI_HOME/$FLOW" | jq '<selector>'
   ```
   Never edit blind from a remembered description of the flow.

   **The flow file is not always under `conf/`.** `nifi.flow.configuration.file` decides, and on the
   CFM-operator pods it is `./data/flow.json.gz`, not `./conf/flow.json.gz` — a hardcoded `conf/` path
   fails with an empty result and a zero-byte dump, which reads like "the flow is empty" rather than
   "you looked in the wrong place". Also pass `-c nifi`: an operator-managed pod runs the NiFi container
   alongside several log sidecars, and without it `kubectl exec` can land in one that has no flow at all.

   **One carve-out: `flow.json` is truth for *structure and state*, not for whether a sensitive property is parameter-bound.** NiFi persists the **resolved** value of a `#{param}`-referenced sensitive property as `enc{...}` — byte-for-byte the same form as a genuine inline literal. `flow.json.gz` cannot tell the two apart, and a `GET /processors/{id}` can't either (it masks both as `********`). The authoritative check is the parameter context:
   ```bash
   GET /nifi-api/parameter-contexts/{id}
   # -> .component.parameters[].parameter.referencingComponents[]   <- this list is the answer
   ```
   Reading `enc{}` as "literal, so the Parameter Context migration never happened" is a real trap: it once cost a third of a session and produced a wrong user-facing claim that credentials had regressed, plus a re-run of a migration that had been done weeks earlier and had held the whole time.
2. **Never GET-then-PUT a processor entity that has sensitive properties.** NiFi returns `"********"` for a sensitive property on GET; PUT the returned entity back and you write that literal string over the real credential, destroying it. Instead:
   - Bind sensitive props to a **Parameter Context** (`#{param-name}`) — write-only via the API, immune to the mask. This is the only safe pattern for credentials inside a flow.
   - Or use a narrow-scope endpoint that sends only the field you're changing, e.g. `PUT /processors/{id}/run-status` (revision + state only).
   - Before any full-entity PUT, check that processor's `descriptors[...].sensitive` — this applies to every processor that has one, not just ones already known to hold credentials. `validationStatus: VALID` never proves a sensitive value is real; NiFi can't tell a genuine secret from the mask string.
   - To check whether a given processor is *already* bound to a Parameter Context, use the context's `referencingComponents` (rule 1's carve-out) — not `flow.json.gz`, and not a `GET` of the processor. Neither of those can distinguish a reference from a literal.
3. **Don't hand-patch a live Process Group while it's actively posting/queueing.** Route the change through the API from a trusted host, or rebuild → redeploy. Never inject hand-crafted data into a live trigger to shortcut a test — let the real pipeline fire it.
4. **Keep changes scoped.** Make the change asked for, not the adjacent "obvious improvement." A rename is not a rewire is not a retype — bundling them turns a one-line review into a hunt.
5. **Every flow change gets exported + committed.** A running canvas that isn't in version control is one restart from gone. Export the Process Group JSON after every real change.
6. **`ListenHTTP` on MiNiFi C++ is fire-and-forget; MiNiFi Java is not.** MiNiFi C++ has no `HandleHttpRequest`/`HandleHttpResponse` pair — the caller gets an empty 200 ack, and the real reply must exit via Kafka keyed on a caller-supplied `request_id`. This is a **C++-agent-only** limitation. The MiNiFi **Java** agent ships both processors and the `StandardHttpContextMap` controller service — verified in the agent's NAR bundle (both `1.23.04-b15` and `2.24.08.0-19`) and by running the early-ack flow (`HandleHttpRequest` → `HandleHttpResponse(200)` → rest of flow), which returns a real 200 in ~84ms while the FlowFile continues downstream independently. EFM's Flow Designer exposes both with no side-loading. Cost: a Java agent runs ~500MB RSS / ~500MB installed vs the C++ agent's ~100MB RSS / ~247MB installed — real but not prohibitive, and Java and C++ agents run side by side under separate agent classes with no conflict.
7. **`Retry` is not `Failure`.** Auto-terminating `InvokeHTTP`'s `Retry` relationship silently drops every transient 5xx/429. Self-loop `Retry` with a bounded `FlowFile Expiration` (10 min is a good default) and route `Failure`/`No Retry` to a log processor.
8. **Build new logic in its own new, finite Process Group — never inline inside a live one.** Sharing a PG with running logic is how a connection lands on the wrong relationship or a rewire silently reroutes existing traffic, and it makes "what changed" unreviewable. Give the new capability its own PG with no shared connections, sitting alongside the existing flows rather than woven into them. If it genuinely must connect into an existing flow, make that a separate, deliberate step — don't let the wiring fall out as a side effect of the build.
9. **Decompose into a FlowFile chain of small native processors — don't put timers, state, and branching inside one custom Python processor.** A single `FlowFileSource` running its own background thread looks simpler but is the wrong shape: you lose per-stage queue counts, provenance, mid-flow inspection, and the ability to re-test one step by re-queuing a FlowFile — and the thread can outlive NiFi's own start/stop lifecycle (*why:* a leaked internal-timer thread once kept re-logging stale state after a restart, which a stock-processor chain can't do since NiFi owns all scheduling). Prefer `GenerateFlowFile`(timer) → `InvokeHTTP`/`SplitJson`/`EvaluateJsonPath`/`RouteOnAttribute`, and let each processor's `Run Schedule` drive cadence — never an internal `while`/`sleep`. Reach for custom Python only for the one thing NiFi can't do natively (e.g. one persistent external socket). Shape detail in `patterns.md`.
10. **Never read `flow.json.gz` to add a component.** Adding a PG to a live environment does not require reading the root flow. The committed JSON export in your git repo is the source of truth — download it from disk or its GitHub raw URL, then POST it to the parent PG's `process-groups/upload` endpoint. This is surgical: only the new PG is touched; the rest of the canvas is never read or modified. If you need to know what's already deployed, use `GET /nifi-api/process-groups/root/process-groups` to list child PGs by name — not the config file. Full playbook, upsert pattern, Parameter Context pre-create, k8s Job template, and secret manager options: `references/flow-registry.md`.

## Deployment shapes

| Shape | Where it lives | Auth | When |
|---|---|---|---|
| **Operator-managed on Kubernetes** | A `Nifi` CR → StatefulSet pod | Operator-issued mTLS user cert, *or* Single-User Auth via a k8s secret | In-cluster flows |
| **Host-native NiFi** | A tarball install, `bin/nifi.sh start`, single-user auth | Single-user login | A single VM / public-facing host |
| **MiNiFi C++/Java agent (EFM-deployed)** | Windows service, Linux `minifi.service`, or a K8s pod | Unauthenticated agent→EFM heartbeat by default (`autoConfigureSecurity=false`) | Edge / desktop flows driven from EFM |

A full edge-to-core deployment is often all three at once: **EFM + MiNiFi agents on the edge + Kafka in the middle + NiFi doing the heavier lift.**

**Deploying/re-enrolling a MiNiFi agent: get the command from EFM, never hand-build it.** Obtain the deployer command *only* from EFM's Deploy Agent CLI screen or its backing API `POST /efm/api/agent-deployer/generateCommand` (omit `agentIdentifier`; the server mints a fresh one). Never hand-construct the `curl`/`Invoke-WebRequest`, and never copy a prior deployment's command and edit the fields — that path reuses a stale `agentIdentifier` and two pods collide on one EFM identity, which makes the C2 `UPDATE` flow-push fail (`state: FAILED`). This applies to any "recreate this pod under a new class" / class-migration task: a new enrollment always needs a fresh, server-minted identifier — reusing one is only correct when restoring the *exact same* bare pod that was never de-registered. Full incident + `generateCommand` body: `references/minifi-efm.md` §4.

## A redeploy can break a live flow

A rebuild/redeploy — or a single-replica pod restart — of any service a running `InvokeHTTP` targets kills the in-flight request mid-response (`unexpected end of stream`), the same silent drop as an auto-terminated `Retry` (rule 7). It's not a NiFi edit, so it's easy to treat as unrelated to the flow — but it breaks the flow. Before any such redeploy: dump the live flow (rule 1), confirm no processor is running/mid-fetch and let in-flight ones drain (don't fire and assume they stopped), and get a fresh go-ahead each time. The full restart/redeploy policy — one-pod-`Running` sanity, per-instance asks — is deploy discipline; keep it in your own operational runbook, separate from the flow-edit rules here.

## References — load the one you need

| File | Covers |
|---|---|
| `references/flow-api.md` | Deploying and editing flows via the NiFi REST API — auth handles, uploading a Process Group JSON, downloading/re-exporting one to keep a checked-in copy current, safe live edits. |
| `references/patterns.md` | Flow patterns that ship: NiFi-as-HTTP-API, MiNiFi fire-and-forget router, the ingest→Kafka→transform→sink (RAG) shape, and the GUI-less edge→host bridge. |
| `references/custom-processors.md` | Writing custom Python/Java processors, the mixed-template EL trap, and rebuild→redeploy discipline. |
| `references/minifi-efm.md` | The edge side: staging agent binaries, EFM persistence, deploying an agent via EFM's `generateCommand` (never a hand-built deployer curl), Windows+Python, the (undocumented) EFM Flow Designer API, recovering an EFM-managed agent whose heartbeat has gone silently dark (bare-pod restart, asset-sync race, IP instability), a manifest-cache staleness gotcha when a processor's properties change but its name doesn't, and orphaned Resource assets causing a permanent `SYNC RESOURCE` failure loop that unassigning alone won't clear (needs an EFM pod restart) — plus why the "Updated Agents" dashboard badge can stay red or green independent of actual health. |
| `references/debugging.md` | Cross-cutting wire-up gotchas and a 10-step debugging checklist. |
| `references/layout.md` | Canvas layout & arrangement: the coordinate model, grounded spacing constants, direction & sprawl rules (route/add down never up, new work goes right of existing canvas, one test funnel, per-branch terminal logs), per-shape placement rules, and a worked example — plus the running list of other things a Claude-built flow still needs a human pass on. |
| `references/site-to-site.md` | Site-to-Site and secure-cluster rollout on the CFM operator: `userCertAuth` + `initialAdminIdentity` at CR creation, identity = cert SAN not DN, the one-CA issuer chain (cluster-issuer first, never the operator's selfSigned), peers as `User` CRs in fixed order (flow-author → port → peer policy; never hand-POST policies), HTTP transport keys, the foreign-peer transit proof, `SiteToSiteReportingRecordSink`, and a symptom→cause→fix traps table. Load this before standing up any secure NiFi or wiring any RPG. |
| `references/flow-registry.md` | Add/Update a PG without root flow; GitHub as registry; PG upsert (stop→drain→delete→reimport); Parameter Context pre-create from k8s Secrets; complete k8s Job YAML; HashiCorp Vault and AWS Secrets Manager options; NiFi Registry as optional enhancement. |

## The most common ways a NiFi flow silently fails

Reach for `references/debugging.md` for the full list, but the top offenders:
- **`ListenHTTP` `Batch Size`/`Buffer Size` default to `5/5`** — a single request never fills the buffer and is dropped. Set both to `1`.
- **`InvokeHTTP`'s `HTTP Method` silently stays `GET`** even when you meant `POST`, unless you explicitly set it.
- **Auto-terminated `Retry`/`Failure`/unmatched relationships** — where FlowFiles vanish. Dump the live flow's `autoTerminatedRelationships`.
- **Wrong Kafka bootstrap port** — external `<external-nodeport>` from outside the cluster vs `9092`/`9093` inside it.
- **NiFi pod clock is UTC** — cron-scheduled processors fire on UTC, not your local time zone.
