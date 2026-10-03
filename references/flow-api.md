# Deploying a NiFi flow via the REST API

Covers the Kubernetes / operator-managed case; the same API calls work against a host-native NiFi once you have an auth handle and can reach `$NIFI/nifi-api`.

## Deployment shapes

| Shape | Where it lives | Auth | When |
|---|---|---|---|
| **Operator-managed on Kubernetes** | A `Nifi` CR → StatefulSet pod | Operator-issued mTLS user cert, *or* Single-User Auth via a k8s secret | In-cluster flows |
| **Host-native NiFi** | A tarball install, `bin/nifi.sh start`, single-user auth | Single-user login | A single VM / public-facing host |
| **MiNiFi C++/Java agent (EFM-deployed)** | Windows service, Linux `minifi.service`, or a K8s pod | Unauthenticated agent→EFM heartbeat by default (`autoConfigureSecurity=false`) | Edge / desktop flows driven from EFM |

A full edge-to-core deployment is often all three at once: **EFM + MiNiFi agents on the edge + Kafka in the middle + NiFi doing the heavier lift.**

## 0. Read the live flow before you touch it (skill rule 1)

Dump the live flow and read what is actually there. Never edit blind from a remembered description of the flow.

```bash
# Ask the pod where its flow lives - do NOT hardcode the directory.
NIFI_HOME=/opt/nifi/nifi-current
FLOW=$(kubectl exec <nifi-pod> -n $NS -c nifi -- \
         sed -n 's|^nifi\.flow\.configuration\.file=\./||p' $NIFI_HOME/conf/nifi.properties)
kubectl exec <nifi-pod> -n $NS -c nifi -- gunzip -c "$NIFI_HOME/$FLOW" | jq '<selector>'
```

**The flow file is not always under `conf/`.** `nifi.flow.configuration.file` decides, and on the CFM-operator pods it is `./data/flow.json.gz`. A hardcoded `conf/` path fails with an empty result and a zero-byte dump, which reads like "the flow is empty" rather than "you looked in the wrong place". Pass `-c nifi` too: an operator-managed pod runs the NiFi container alongside several log sidecars, and without it `kubectl exec` can land in one that has no flow at all.

**`flow.json` is truth for structure and state, not for whether a sensitive property is parameter-bound.** It stores the resolved value of a `#{param}` reference as `enc{...}`, the same form as an inline literal. Ask the parameter context's `referencingComponents` instead (§5, "Is this property actually parameter-bound?"). Reading `enc{}` as "the migration never happened" once cost a third of a session, a wrong claim that credentials had regressed, and a re-run of a migration that had held for weeks.

## 1. Get an auth handle

**Preferred — operator mTLS user cert (no login, no token expiry).** If NiFi is managed by an operator that issues a user cert, pull it from the secret and use it as a client cert:

```bash
kubectl get secret <operator-user-cert-secret> -n $NS -o jsonpath='{.data.tls\.crt}' | base64 -d > client.crt
kubectl get secret <operator-user-cert-secret> -n $NS -o jsonpath='{.data.tls\.key}' | base64 -d > client.key
# Then: curl -k --cert client.crt --key client.key https://.../nifi-api/...
```

**Fallback — Single-User bearer token, obtained from inside the pod.** Do this from the NiFi pod itself, where the credential secret is mounted — never inject the password into an unrelated pod's process list:

```bash
kubectl exec -n $NS <nifi-pod> -- bash -c '
  U=$(cat /path/to/creds/username)
  P=$(cat /path/to/creds/password)
  curl -sk -X POST https://localhost:8443/nifi-api/access/token \
    -d "username=$U&password=$P"
'
```

Two traps with the bearer token:
- **Never echo the password to the terminal/transcript.**
- **Don't pair a Bearer token with session cookies.** If a cookie is present, NiFi flips into cookie-auth mode and rejects the token with `403`/CSRF errors. Send the `Authorization: Bearer` header alone.

## 2. Reach the API

- Local dev: `kubectl port-forward -n $NS svc/<nifi-web-svc> 8443:8443` → `https://localhost:8443/nifi-api`.
- From another pod or a Job: address the internal service DNS, `https://<nifi-web-svc>.$NS.svc.cluster.local:8443/nifi-api`.
- Always `-k` for self-signed TLS until you've wired a real cert.

## 3. Upload a Process Group flow-definition JSON

This is the right tool for flow-definition uploads (including ones with sensitive properties) — raw multipart `curl`, not a client library:

```bash
ROOT_PG_ID=$(curl -sk --cert client.crt --key client.key \
  "$NIFI/nifi-api/flow/process-groups/root" | jq -r '.processGroupFlow.id')

curl -sk --cert client.crt --key client.key -X POST \
  "$NIFI/nifi-api/process-groups/$ROOT_PG_ID/process-groups/upload" \
  -H 'Content-Type: multipart/form-data' \
  -F "positionX=100.0" -F "positionY=100.0" \
  -F "groupName=MyFlow" \
  -F "clientId=$(uuidgen)" \
  -F "disconnectNode=false" \
  -F "file=@./MyFlow.json"
```

Then start it:

```bash
curl -sk --cert client.crt --key client.key -X PUT \
  "$NIFI/nifi-api/flow/process-groups/$NEW_PG_ID" \
  -H 'Content-Type: application/json' \
  -d '{"id":"'$NEW_PG_ID'","state":"RUNNING"}'
```

**Positioning:** the `positionX`/`positionY` above place the PG; the `position` on each processor inside the uploaded JSON places the components. Pick these deliberately — a build with careless positions is functionally correct but unreadable on the canvas. Before you commit the `position` values, state the flow shape + pitch and match them against the per-shape rules in [`layout.md`](layout.md) (NiFi REST builds use row pitch 200 / branch ±300; the EFM Designer numbers are larger — don't cross them up). This isn't optional politeness: skipping it lands EFM builds cramped, and a PreToolUse hook can prompt for this self-check on any processor-create/update carrying a `position`.

## 4. Downloading a flow definition (the reverse direction — keeping exports current)

Checked-in flow-definition JSON (`flows/*.json`, or wherever a repo snapshots its NiFi flows) goes stale the moment someone hand-edits the live PG via the UI or the API — which is the normal way these flows evolve. Treat re-exporting as a habitual close-out step after any live-build session that touches a flow with a checked-in export, not something you only do when asked.

```bash
# Find the PG's real runtime ID first (its instanceIdentifier, not the version-control
# identifier — see the two-IDs gotcha elsewhere in this skill)
curl -sk --cert client.crt --key client.key \
  "$NIFI/nifi-api/flow/process-groups/root" | jq -r '.processGroupFlow.id'

# Same VersionedFlowSnapshot JSON the UI's "Download flow definition" produces
curl -sk --cert client.crt --key client.key \
  "$NIFI/nifi-api/process-groups/$PG_ID/download" -o MyFlow.json
```

**Pretty-print before committing.** The raw response is minified (single line) — committing it that way turns every future diff into a full-file rewrite instead of the real, reviewable additive change:

```python
import json
d = json.load(open("MyFlow.json"))
json.dump(d, open("MyFlow.json", "w"), indent=2)
```

**Confirmed safe to commit (checked empirically, not assumed):** Parameter Context sensitive-property values export as `null`, never the real value or even the `"********"` GET-mask — and processor-level sensitive properties aren't embedded either, since the correct pattern (rule 2 above) keeps them out of literal processor properties entirely. No credential-leak risk in a flow-definition download, unlike a raw processor-entity `GET`.

## 5. Editing a live processor safely

**State change only** (start/stop/enable — e.g. to pulse a processor once):

```
GET  /processors/{id}                 # capture revision.version
PUT  /processors/{id}/run-status      # {"revision":{"version":N},"state":"RUNNING"}
```

This endpoint takes revision + state only. It cannot corrupt sensitive properties. It's the basis of the `run-once` pattern: start → sleep a few seconds → re-fetch revision → stop.

**`RUN_ONCE` needs the processor `STOPPED` first (NiFi 2.6).** `PUT /processors/{id}/run-status` with `"state":"RUN_ONCE"` against a source that is currently `RUNNING` (e.g. a CRON-scheduled `GenerateFlowFile`) returns a non-JSON error, not a single fire — NiFi only accepts `RUN_ONCE` from the `STOPPED` state. Sequence: `run-status` → `STOPPED`, then `run-status` → `RUN_ONCE`. This is the precondition behind the manual start→sleep→stop pattern above.

**`InvokeHTTP` `Response Body Attribute Size` is a bare integer of bytes (NiFi 2.6).** It takes `65536`, not `"64 KB"` — a human-readable data-size string is rejected as INVALID (unlike NiFi's timer/size properties elsewhere that do accept `64 KB`). And note the routing side effect: once `Response Body Attribute Name` is set, `InvokeHTTP` writes the response body into that attribute on the **enriched original** FlowFile and routes it to `Original`, producing **no** separate Response FlowFile — so auto-terminate the `Response` relationship or FlowFiles pile up unrouted there.

**A running processor rejects a property-only `PUT` with `409 Conflict`.** NiFi requires the processor be `STOPPED` before any config-property change lands — a full-entity `PUT` (properties intact, just changing one) against a `RUNNING` processor 409s even though the exact same body would succeed while stopped. The safe sequence for changing a live, in-use processor's property (e.g. repointing an `InvokeHTTP`'s target URL): `run-status` → `STOPPED` (narrow endpoint, safe per above) → `GET` full entity (revision bumped by the stop) → `PUT` full entity with the one property changed → `run-status` → `RUNNING` again (revision bumped again). Each step's revision must come from the immediately-preceding response, not an earlier one.

**Property edit** — send only the properties you're changing; never PUT the full entity. If the property is sensitive, don't send it here at all — bind it to a Parameter Context and manage the value there (see rule 2 in `SKILL.md`).

**Deleting a connection requires BOTH endpoint processors `STOPPED`, not just a version match.** `DELETE /connections/{id}?version=N&clientId=...` 409s with `Upstream component of Connection (...) is running` (then, once the source is stopped, `Destination of Connection (...) is running`) if either the source or destination processor is `RUNNING` — this is a separate requirement from the revision-version check, and the error message is the only thing that tells you which side is still blocking. Sequence: `run-status` the source to `STOPPED`, `run-status` the destination to `STOPPED`, delete the connection(s), then `run-status` both back to `RUNNING` if they were running before. If several connections share the same source processor (a fan-out, e.g. one `RouteOnAttribute` feeding many downstream branches), stopping that one processor pauses *everything* it feeds, not just the branch you're editing — confirm that's an acceptable blast radius (and get a fresh go-ahead if it's a shared, live-traffic PG) before stopping it, per rule 8 below.

**Ports count as endpoints too.** An input or output port on either end of the connection blocks the delete the same way a processor does (`/input-ports/{id}/run-status`, `/output-ports/{id}/run-status`). And a **local port with no incoming connection will not start** — NiFi rejects the `RUNNING` transition — so deleting the last connection into an output port leaves that port stopped for good. Plan for it: either delete the port in the same run or leave it stopped deliberately and say so.

**Changing a connection's relationships needs both ends stopped as well, and a start sent to an invalid processor is held.** A `PUT /connections/{id}` that changes `selectedRelationships` 409s with `Cannot change the destination of connection because the current destination is running` while either end runs, even when the destination itself is not changing. A merge script that deletes the duplicate first and restarts a port end as soon as that delete lands will hit exactly this on the widening `PUT`, leaving the source with an unconnected relationship. Start that source in this state and NiFi accepts the request but holds it until the processor is valid again: `GET /processors/{id}` still reads `STOPPED`, while every connection on it reports `"running": true` for that end. A stop check that reads only `component.state` misses it, and the next relationship change or connection delete is refused again. Read each end's `running` flag from the connection itself, send an explicit `STOPPED` to anything it reports running, and only then make the change. (2026-10-03, #426: a DOBridge merge left `RouteDoAction` INVALID for a few minutes; nothing queued was lost.)

**Auto-terminate and connections, in which order.** NiFi refuses to add a relationship to `autoTerminatedRelationships` while a connection uses it (`409 Cannot automatically terminate '<rel>' relationship because a Connection already exists`), and a relationship that is neither connected nor auto-terminated makes the processor INVALID, so it will not start. Creating a connection on a relationship that is still auto-terminated is allowed. So:
- **Routing a relationship that was auto-terminated:** create the connection first, then drop the relationship from `autoTerminatedRelationships`. The processor is valid at every step.
- **Retiring a routed relationship:** delete the connection, then either auto-terminate the relationship or remove the dynamic property that defines it (a `RouteOnAttribute` rule). The processor is invalid between those two writes, so keep it stopped for exactly those two and have a fallback if the second one fails.

**A script that does these writes needs a way back.** Record every component the script stops and restart them from an `EXIT` trap, so a failed write (a 409, a stale revision) still ends with everything that was running running again — under `set -e` the first failure otherwise exits with processors stopped and queues filling. Skip the restart for anything the script deleted. Before running it against a live flow, run the dry run (a helper ending in `[ $APPLY = 1 ] && …` returns 1 and kills a `set -e` dry run) and run the apply against a stub seeded from a read-only snapshot of the flow, with a failure injected at each write type and the refusals above modelled.

**Is this property actually parameter-bound? Ask the parameter context, not the processor and not `flow.json`.** Both of the obvious checks lie: `flow.json.gz` stores the **resolved** value of a `#{param}` reference as `enc{...}`, identical in form to a real inline literal, and `GET /processors/{id}` masks a sensitive value as `********` whether it is bound or not. Only the context knows:

```bash
# 1. find the context
curl -sk --cert c.crt --key c.key "$NIFI/nifi-api/flow/parameter-contexts" \
  | jq -r '.parameterContexts[] | "\(.id)  \(.component.name)"'

# 2. ask it which components reference each parameter — this list is authoritative
curl -sk --cert c.crt --key c.key "$NIFI/nifi-api/parameter-contexts/<ctx-id>" \
  | jq -r '.component.parameters[]
           | "\(.parameter.name): \([.parameter.referencingComponents[]?.component.name] | join(", "))"'
```

A parameter with an empty `referencingComponents` list is genuinely unused; a processor that appears there is genuinely bound, no matter what `flow.json.gz` shows. Verified from both ends: a processor created via `POST /process-groups/{id}/processors` with an explicit `"Client Secret": "#{twitch-chat-client-secret}"` — which the create response echoed back as `#{...}` and which validates `VALID` — still persisted to `flow.json.gz` as `enc{...}`.

**Verify a processor's capabilities against the live API before designing around them.** Recollection of NiFi processor behavior is not a source. Two probes: for a capability question (does this type take dynamic properties?), `GET /nifi-api/flow/processor-types` and read the type's descriptors — `supportsDynamicProperties` was absent for `GenerateFlowFile` on a 2.x build, which would have silently broken a design built on the assumption; for a behavior question (what attribute name does `ListenHTTP` give a received header?), run one real-but-harmless probe — a value that matches no real downstream route — and read the queued FlowFile back (`POST /flowfile-queues/{connId}/listing-requests`, then `GET /flowfile-queues/{connId}/flowfiles/{uuid}`; the connection id is the `instanceIdentifier`, not `identifier`). Also: creating an explicit connection for a relationship clears its auto-terminate flag as a side effect — check both afterwards and confirm `validationStatus: VALID`.

**Hitting the API via a pod's own IP instead of its expected hostname can fail TLS entirely.** `curl -sk https://<pod-ip>:8443/nifi-api/...` from inside the pod itself returned `400 Invalid SNI` on a cluster where `https://localhost:8443` also failed (connection refused — the port is bound to the pod's IP, not loopback). Fix: `curl -sk --connect-to <expected-hostname>:8443:<pod-ip>:8443 https://<expected-hostname>:8443/nifi-api/...` — connects to the real reachable address while sending the hostname the server's Jetty SNI check actually wants (its own service DNS name, `<nifi-svc>.<ns>.svc.cluster.local` in an operator-managed deployment). Confirm the pod's actual bound address first (`ss -tlnp` inside the pod) rather than assuming `localhost` or a bare IP will work.

## 6. Client libraries (nipyapi)

`nipyapi` is fine for Registry-backed flow versioning, Parameter Context CRUD, and flat CRUD on components — reach for it when the alternative is scripting five separate `curl` calls. It is **not** the tool for flow-definition uploads that carry sensitive properties; use the raw multipart `curl` in §3 for those.

## 7. A note on public TLS certs

If you terminate a real (e.g. Let's Encrypt) cert in front of NiFi, do it at an ingress/proxy layer and re-encrypt to NiFi's backend cert — **don't replace an operator's node-identity cert chain with the public cert.** With Single-User Auth the node's server-identity DN is often also the `Initial Admin Identity`, so swapping it means editing `authorizers.xml` and restarting on every renewal. For host-native NiFi, a `certbot` deploy hook that rebuilds the keystore and restarts NiFi is the clean path.
