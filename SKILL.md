---
name: nifi-and-ai
description: Build, deploy, and debug Apache NiFi 2.x, MiNiFi (C++/Java), and EFM data flows — programmatically via the REST API, as custom Python/Java processors, or as edge agents on Kubernetes — including LLM/RAG inference patterns (Kafka, Whisper, embeddings, vector stores). Use when wiring a NiFi flow, deploying a MiNiFi agent, writing a custom processor, exposing NiFi as an HTTP API, or debugging silent data drops, corrupted sensitive properties, or flow-definition uploads.
---

# NiFi + AI flow playbook

A working playbook for building **NiFi 2.x + MiNiFi + EFM** flows programmatically and agentically, on Kubernetes and at the edge. Each rule is the distilled version of a real bug that cost real time. The detail lives in `references/`; load the one the task needs.

**Conventions:** `$NS` is the namespace NiFi runs in, `<nifi-pod>` the NiFi pod, `$NIFI` the API base (`https://<host>:8443`, API under `$NIFI/nifi-api/...`), `<external-nodeport>` Kafka's external NodePort. Self-signed TLS is assumed (`-k` / `verify_ssl=False`); drop it once a real cert is wired.

## The rules — read before touching any live flow

1. **Live UI / `flow.json` is truth; docs and memory lag.** Dump the live flow before touching a running PG. Resolve its path from `nifi.flow.configuration.file` (operator pods: `data/`, not `conf/`) and `kubectl exec -c nifi`. One carve-out: `enc{}` in `flow.json` can't tell a `#{param}` from a literal; the parameter context's `referencingComponents` can. → `flow-api.md` §0, §5.
2. **Never GET-then-PUT a processor entity that has sensitive properties.** GET returns `"********"`; PUT it back and you overwrite the real credential. Bind secrets to a **Parameter Context**, or use a narrow endpoint (`PUT /processors/{id}/run-status`). Check `descriptors[...].sensitive` before any full-entity PUT; `VALID` never proves a secret is real. → `flow-api.md` §5.
3. **Don't hand-patch a live PG while it is posting or queueing.** Change it through the API from a trusted host, or rebuild and redeploy. Never inject hand-made data into a live trigger to shortcut a test.
4. **Keep changes scoped.** A rename is not a rewire is not a retype.
5. **Every flow change gets exported and committed.** → `flow-api.md` §4.
6. **MiNiFi C++ `ListenHTTP` is fire-and-forget; MiNiFi Java is not.** C++ has no `HandleHttpRequest`/`HandleHttpResponse`, so the reply exits via Kafka keyed on a `request_id`. The Java agent has both. → `minifi-efm.md` §0.
7. **`Retry` is not `Failure`.** Never auto-terminate `Retry`. Self-loop it with a bounded `FlowFile Expiration` (10 min) and send `Failure`/`No Retry` to a sink beside the stage.
8. **New logic goes in its own new, finite PG, never inline in a live one.** If it must connect into an existing flow, that is a separate, deliberate step.
9. **Decompose into a chain of small native processors.** No timers, state or branching inside one custom Python processor: you lose queue counts, provenance and re-testability, and a leaked thread can outlive NiFi's lifecycle. Use `Run Schedule` for cadence. → `patterns.md`.
10. **Never read `flow.json.gz` to add a component.** POST the committed export to the parent's `process-groups/upload`, and list children with `GET /process-groups/root/process-groups`. → `flow-registry.md`.
11. **Lay the flow out before it lands, then check it.** Positions are baked into the JSON. Run `layout.md`'s pre-flight, then `scripts/check_layout.py <flow.json>`, before any upload and before accepting a flow a sub-agent built. Sinks: one per stage in dev, pruned in prod. One master label on top of every PG. → `layout.md`.
12. **Get an agent's deployer command from EFM, never hand-build it.** Use the Deploy Agent CLI screen or `POST /efm/api/agent-deployer/generateCommand` with no `agentIdentifier`. A new enrollment or class migration needs a fresh, server-minted identifier. → `minifi-efm.md` §4.

A rebuild or redeploy of a service a live `InvokeHTTP` calls kills its in-flight request: drain first (→ `debugging.md`).

## References — load the one you need

| File | Covers |
|---|---|
| `references/flow-api.md` | Deployment shapes; reading the live flow; REST auth; upload / download (keeping exports current); safe live edits; parameter-bound checks; script restore traps. |
| `references/flow-registry.md` | Add/update a PG without the root flow; GitHub as registry; upsert (stop→drain→delete→reimport); Parameter Context pre-create; k8s Job; Vault / AWS secrets. |
| `references/layout.md` | Pre-flight, NiFi box sizes, in-PG pitches and shapes, direction rules, dev/prod failure sinks, labels, root-canvas hub-and-spoke, NiFi's overlapping-connection warning, delegation. |
| `scripts/check_layout.py` | The layout rules as a checker: overlaps, label-gap pitch, bend-less duplicate connections, upward routes, far-flung ports/groups, unlabelled PGs, `Retry` auto-terminated. Non-zero exit on FAIL. |
| `references/patterns.md` | NiFi as an HTTP API (incl. an SQL-backed door as one database function), the MiNiFi fire-and-forget router, ingest→Kafka→transform→sink (RAG), the edge→host bridge. |
| `references/custom-processors.md` | Custom Python/Java processors, the EL binding traps, rebuild→redeploy, bundle-only version switch. |
| `references/minifi-efm.md` | C++ vs Java agents, staging binaries, EFM persistence, `generateCommand`, Windows + Python, the EFM Designer API, dark-agent recovery, manifest-cache and `SYNC RESOURCE` traps. |
| `references/site-to-site.md` | S2S and secure-cluster rollout on the CFM operator: `userCertAuth`, cert SAN identity, one-CA chain, peer `User` CRs, the traps table. Load before wiring any RPG. |
| `references/debugging.md` | Wire-up gotchas (EL traps, UTC clock, log FIFOs, redeploys) and the 10-step silent-drop checklist: `ListenHTTP` 5/5 buffers, `InvokeHTTP` stuck on `GET`, auto-terminated relationships, Kafka ports. |
