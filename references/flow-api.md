# Deploying a NiFi flow via the REST API

Covers the Kubernetes / operator-managed case; the same API calls work against a host-native NiFi once you have an auth handle and can reach `$NIFI/nifi-api`.

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

**A running processor rejects a property-only `PUT` with `409 Conflict`.** NiFi requires the processor be `STOPPED` before any config-property change lands — a full-entity `PUT` (properties intact, just changing one) against a `RUNNING` processor 409s even though the exact same body would succeed while stopped. The safe sequence for changing a live, in-use processor's property (e.g. repointing an `InvokeHTTP`'s target URL): `run-status` → `STOPPED` (narrow endpoint, safe per above) → `GET` full entity (revision bumped by the stop) → `PUT` full entity with the one property changed → `run-status` → `RUNNING` again (revision bumped again). Each step's revision must come from the immediately-preceding response, not an earlier one.

**Property edit** — send only the properties you're changing; never PUT the full entity. If the property is sensitive, don't send it here at all — bind it to a Parameter Context and manage the value there (see rule 2 in `SKILL.md`).

**Hitting the API via a pod's own IP instead of its expected hostname can fail TLS entirely.** `curl -sk https://<pod-ip>:8443/nifi-api/...` from inside the pod itself returned `400 Invalid SNI` on a cluster where `https://localhost:8443` also failed (connection refused — the port is bound to the pod's IP, not loopback). Fix: `curl -sk --connect-to <expected-hostname>:8443:<pod-ip>:8443 https://<expected-hostname>:8443/nifi-api/...` — connects to the real reachable address while sending the hostname the server's Jetty SNI check actually wants (its own service DNS name, `<nifi-svc>.<ns>.svc.cluster.local` in an operator-managed deployment). Confirm the pod's actual bound address first (`ss -tlnp` inside the pod) rather than assuming `localhost` or a bare IP will work.

## 6. Client libraries (nipyapi)

`nipyapi` is fine for Registry-backed flow versioning, Parameter Context CRUD, and flat CRUD on components — reach for it when the alternative is scripting five separate `curl` calls. It is **not** the tool for flow-definition uploads that carry sensitive properties; use the raw multipart `curl` in §3 for those.

## 7. A note on public TLS certs

If you terminate a real (e.g. Let's Encrypt) cert in front of NiFi, do it at an ingress/proxy layer and re-encrypt to NiFi's backend cert — **don't replace an operator's node-identity cert chain with the public cert.** With Single-User Auth the node's server-identity DN is often also the `Initial Admin Identity`, so swapping it means editing `authorizers.xml` and restarting on every renewal. For host-native NiFi, a `certbot` deploy hook that rebuilds the keystore and restarts NiFi is the clean path.
