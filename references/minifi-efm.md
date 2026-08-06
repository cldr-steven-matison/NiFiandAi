# MiNiFi C++ / EFM — the edge side

EFM (Edge Flow Manager) manages MiNiFi agents: it stores agent-class flows, deploys agent binaries, and pushes flow updates to agents over their heartbeat.

## 1. Stage agent binaries into EFM

EFM's `agent-deployer/binaries` directory layout is **strict**: its validator rejects hyphens in `osArch` and more than one archive per leaf directory. Layout for the common four:

```
binaries/cpp/linux/<ver>/minifi.tar.gz            # x86_64 Linux
binaries/cpp/linuxaarch64/<ver>/minifi.tar.gz     # ARM64 Linux
binaries/cpp/windows/<ver>/minifi.msi             # Windows
binaries/java/linux/<ver>/minifi.tar.gz           # Java MiNiFi
```

Inject any Linux `.so` extra-extensions and extra-python-components **inside** the tarball's `extensions/` dir before re-tarring, then tar-pipe into the EFM pod:

```bash
EFM_POD=$(kubectl get pod -n $NS -l app=efm -o jsonpath='{.items[0].metadata.name}')
tar -cf - binaries/ | kubectl exec -i $EFM_POD -n $NS -- tar -xf - -C /opt/efm/<efm-dir>/agent-deployer/
kubectl rollout restart deployment/efm -n $NS
```

## 2. EFM persistence — three layers or a restart wipes state

1. **Postgres** — metadata: `agent_class`, `flow`, `flow_content`, `agent`, `agent_manifest`, `asset`, `resource_metadata`.
2. **A binaries PVC** → the agent archives from §1.
3. **A resources PVC** → uploaded Resources (Python scripts, JARs). The DB tracks the metadata; the file bytes live here. **Skip this and every uploaded script vanishes on pod restart even though the DB rows survive** — a confusing failure where the resource "exists" but has no content.

## 3. Agent pod boot race

A MiNiFi agent pod downloads the deployer script from EFM at startup. EFM's Jetty takes ~2 min to bind its port on a cold start. A one-shot `curl` races that and exits silently — the pod stays `Running 1/1` but the MiNiFi install dir is empty, with a single `curl: (7) Failed to connect` at the top of the pod log and nothing after.

**Fix:** health-poll `/efm/actuator/health` (e.g. 120 × 5s = 10 min ceiling) *before* running the deployer. Diagnose with `kubectl exec <agent-pod> -- ls /nifi-minifi-cpp-<ver>/` — empty means the deployer never ran.

## 4. Deploying an agent (the deployer command)

**Get the command from EFM — never hand-build or copy-edit it.** The only sanctioned way to obtain a deployer command is EFM's own **Deploy Agent CLI** screen in the UI, or its backing API `POST /efm/api/agent-deployer/generateCommand`. Both return the full, ready-to-run command with a **server-minted `agentIdentifier`**. Do **not** hand-construct the `curl`/`Invoke-WebRequest`, and do **not** copy a previous deployment's command and tweak the fields — that is exactly how a stale `agentIdentifier` gets reused and two pods collide on one EFM identity (see the incident callout below).

`generateCommand` body — **omit `agentIdentifier` and the server generates a fresh, collision-free one:**

```bash
curl -s -X POST http://<efm-host>:10090/efm/api/agent-deployer/generateCommand \
 -H 'Content-Type: application/json' \
 -d '{
   "agentClass": "MyClass",
   "agentType": "cpp",
   "agentVersion": "<ver>",
   "osArch": "linuxaarch64",
   "baseUrl": "http://127.0.0.1:<port>/efm/api",
   "hbPeriod": 5000,
   "serviceUser": "minifi",
   "serviceName": "minifi",
   "autoConfigureSecurity": false,
   "trustSelfSignedCertificates": false
 }'
```

The returned command has the shape below (shown so you can read the fields — **do not assemble it by hand**; the `agentIdentifier` line is server-supplied, not something you pick or copy):

```bash
curl -L \
 -d agentClass=MyClass \
 -d agentIdentifier=<SERVER-MINTED — never hand-picked or reused> \
 -d agentType=cpp \
 -d agentVersion=<ver> \
 -d autoConfigureSecurity=false \
 -d baseUrl=http%3A%2F%2F127.0.0.1%3A<port>%2Fefm%2Fapi \
 -d hbPeriod=5000 \
 -d osArch=linuxaarch64 \
 -d serviceName=minifi -d serviceUser=minifi \
 -d trustSelfSignedCertificates=false \
 http://<efm-host>:10090/efm/api/agent-deployer/script | bash -
```

> **Real-world failure mode:** consolidating two agent classes into one, a Java agent was re-enrolled with a **hand-built** deployer `curl` that **reused the retired agent's `agentIdentifier`**. The EFM C2 `UPDATE` pushing the flow to the re-enrolled agent failed twice (`state: FAILED`), and the Agents update-status column showed errors for the class. Fixed by re-enrolling via `generateCommand` with its server-generated identifier. The one place reusing an identifier is correct is restoring the **exact same** bare pod that was never de-registered (§11) — a *new* enrollment or a *class migration* is not that case; mint a fresh identifier.

- **Windows:** run the *generated* command via `Invoke-WebRequest ... | Invoke-Expression` from PowerShell **as Administrator**. Do **not** run it from `C:\WINDOWS\system32` — the deployer installs to `$PWD` and system32 is a permission nightmare. `cd` to a clean dir first.

## 5. Windows MiNiFi + Python (the real gotcha)

The Windows MSI **bundles** the Python scripting extension but as **Feature Level 2** (`CM_C_python_script_extension`) — optional, not selected by the EFM deployer. Symptom without it: `Could not instantiate: PythonScriptExecutor` every 30s.

**Field-verified on Windows (a parallel C++ agent class):** administrative extract works without elevation and still unpacks the Level-2 DLL:

```powershell
# Download MSI, then extract ALL payload (no service):
msiexec /a C:\minifi\minifi.msi TARGETDIR=C:\minifi\extract /quiet
# minifi_native.pyd is a CustomAction mklink to the python DLL — copy if no elevation:
Copy-Item ...\extensions\minifi-python-script-extension.dll ...\extensions\minifi_native.pyd
# Configure C2 for a *parallel* class (do not reuse a live Java agent's class), run bin\minifi.exe
```

Elevated alternative: `msiexec /i minifi.msi ADDLOCAL=ALL INSTALLPYTHONDIR=C:\Python314 INSTALL_ROOT=C:\minifi`.  
Do **not** install from `C:\WINDOWS\system32`.

## 6. `ExecuteScript` availability across builds

| Build | ExecuteScript | Notes |
|---|---|---|
| Stock C++ image (`apacheminificpp` / vendor `:latest`) | ❌ | Production-minimal processor set (74, field-verified). No scripting. |
| C++ MSI + extract/`ADDLOCAL=ALL` | ✅ | Field-verified on Windows. See §5. |
| CEM MiNiFi Java tarball (EFM-staged 2.24.08.0-19) | ❌ | No scripting NAR — only `ExecuteProcess`. |
| Source-built C++ with `-DENABLE_PYTHON_SCRIPTING=ON -DENABLE_LUA_SCRIPTING=ON` | ✅ | Multi-stage Dockerfile from Apache source at the matching tag. |

## 6a. A Windows named pipe's existence is not reliably detectable from outside its owning process

If an `ExecuteStreamCommand`-invoked script needs to tell "is the long-running process on the other end of this named pipe (e.g. mpv's `--input-ipc-server`) already alive" — **don't gate on `os.path.exists()` or PowerShell's `Test-Path`.** Both gave a false negative on a genuinely live, working pipe (confirmed: the same pipe was visible to a .NET `[System.IO.Directory]::GetFiles('\\.\pipe\')` enumeration at the same moment `Test-Path` reported it absent). Gating a "launch fresh vs. reconnect" decision on either check is a real bug, not a theoretical one — it launched a second process bound to the same pipe name, racing the first.

**The fix: attempt the real round-trip and catch failure.** Open the pipe and send one lightweight command with a short timeout (e.g. `get_property idle-active` over mpv's JSON IPC, ~4s); a clean reply proves the process is alive, any exception (including `FileNotFoundError` when the pipe genuinely doesn't exist) means it isn't. This generalizes past mpv: any per-invocation script (stateless by construction, since each `ExecuteStreamCommand` trigger is a fresh process with no memory of prior calls) that needs to know "is my long-lived companion process already running" should resolve that from a real interaction with it, not a filesystem existence check on its IPC handle.

## 7. EFM Flow Designer API (no OpenAPI spec)

EFM exposes **no** OpenAPI/Swagger doc for its flow-editing REST API (`/efm/api-docs`, `/v3/api-docs`, `/efm/swagger-ui` all 404). Guessing at body shapes produces generic `500`s or, worse, silent no-ops — Jackson deserializes an unrecognized shape into a default/empty DTO without erroring, so a `200 OK` does not mean the call did anything.

**Recover the exact contract from EFM's own UI bundle.** Its Angular UI ships an OpenAPI-generated TypeScript client, so the compiled JS has every operation name/URL/body shape verbatim, even minified:

```bash
curl -s http://<efm-host>:10090/efm/ui/ | grep -oE 'src="[^"]*main[^"]*\.js"'   # find the hashed bundle
curl -s http://<efm-host>:10090/efm/ui/main.<hash>.js -o /tmp/efm_main.js
grep -oE '"[A-Za-z]+Service\.[a-zA-Z]+"' /tmp/efm_main.js | sort -u            # every real operation
```

Confirmed working contract:
- `GET /efm/api/designer/client-identifier` → `{"clientId": "<uuid>"}` — required in every write's `revision.clientId`.
- `GET /efm/api/designer/flows/summaries` → one entry per agent class with `identifier` / `rootProcessGroupIdentifier`; `GET .../flows/{id}` for the full live flow doc. **Read this before editing — it's ground truth over any doc or memory.**
- **Full path prefix, always** — every create/update/delete below hangs off `/designer/flows/{flowId}/...`, e.g. `POST /efm/api/designer/flows/{flowId}/process-groups/{pgId}/processors`, `POST .../process-groups/{pgId}/controller-services`, `DELETE .../processors/{id}?version=N&clientId=...`. Dropping the `flows/{flowId}` segment (e.g. guessing `/designer/{pgId}/processors`) 404s as "No static resource" — easy to get wrong since the UI bundle's minified source shows the path built from two params (`e`=flowId, `t`=pgId) without a label telling you which is which; confirm by grepping the literal template string, not just the param count.
- `POST .../process-groups/{pgId}/processors` — create. Body: `{"revision":{"version":0,"clientId":...},"componentConfiguration":{"componentType":"PROCESSOR","type":"<fqcn>","bundle":{...},"name":...,"position":{...},"properties":{...},"autoTerminatedRelationships":[...]},"requestId":"<uuid>"}`. **`revision.version` is always `0` on a create, never an incrementing counter** — every component has its own independent revision sequence; a shared client-side counter across multiple creates produces `"A revision of 0 must be specified when creating a new component"` on the second one. Properties can be set in this one call. **Before you send the `position`:** on an EFM Designer build, row pitch is **300** (not the NiFi 200) and branch/column pitch **~600–900** (not ~300–480), and a linear chain is **vertical** (constant x, `y += 300`) — a `(0,0)→(400,0)` sideways pair is the flagged-bad shape that repeatedly lands cramped. State your intended shape + pitch and match it against [`layout.md`](layout.md) §"Per-shape placement rules" first; a PreToolUse hook can prompt you to do exactly this on the `POST`.
- `POST .../controller-services` — same create envelope as a processor. **Get the `bundle` from a known-working example, don't guess it from the processor NAR.** `StandardHttpContextMap` ships in `{"group":"org.apache.nifi","artifact":"nifi-http-context-map-nar",...}` — the standard-processor bundle (`org.apache.nifi.minifi`/`minifi-standard-nar`) creates it fine (`201`) but fails `/validate` with `"...is not available Controller Service type"`, since the type exists but under a different NAR than processors use. A `PUT` cannot fix a wrong `bundle` on an existing component (silently no-ops, confirmed by re-GETting after) — delete and recreate with the right one, then repoint any processor property that referenced the old identifier.
- `POST .../connections` — same envelope, `componentConfiguration:{componentType:"CONNECTION",source:{id,type:"PROCESSOR",groupId},destination:{...},selectedRelationships:[...],bends:[]}`.
- `PUT .../processors/{id}` — update, same shape; `revision.version` must match current.
- `GET .../flows/{id}/validate` → `{"validationErrors":[]}` — confirm empty before publishing.
- `POST .../flows/{id}/publish` — body `{"comments":"..."}`. **This is the real push-to-agent step** — it overwrites even a manually hand-edited agent-local `config.yml` on the agent's next heartbeat. A hand-edited local config is never authoritative once you use the real API.
- `DELETE /efm/api/agents/{id}` — removes a stale/`MISSING` agent record EFM never garbage-collects on its own. **This does not clean up that agent's history in two other tables, and either one left behind keeps a "N agents failed to update" alert showing in the EFM UI indefinitely even though the agent is gone:**
  - `operation` — one row per operation EFM sent that agent. `DELETE FROM operation WHERE target_agent_id = '<deleted-agent-id>' AND state = 'FAILED'` (only terminal states — never touch a `QUEUED`/non-terminal row for an agent that might still be reconnecting).
  - `bulk_operation` — a **separate, class-scoped** rollup row (`agent_class_id`, `current_state`) that the EFM dashboard's "N of M agents received the last update" widget actually reads. A failed push leaves this at `current_state='FAILED'` and it is **not** recomputed when the failing `operation` row is deleted or the failing agent is removed — check it independently: `SELECT * FROM bulk_operation WHERE agent_class_id = '<class>' AND current_state != 'DONE'`, and delete the stale row once the class's real live agent(s) are confirmed healthy. Skipping this table is why deleting only the `operation` row doesn't clear the UI alert.
- `DELETE .../connections/{id}?version=N&clientId=...` / `DELETE .../processors/{id}?version=N&clientId=...` — same version+clientId query params as any other write. Delete connections before their endpoint processors (standard graph-integrity order). **One call per component — no bulk/batch delete exists**, matching the no-bulk-create rule below.
- **Bootstrapping a brand-new, genuinely empty flow for a class with none yet** (no prior agent ever enrolled, `flows/summaries` has no row for it): there's no `createFlow` operation. Use `POST .../designer/{className}/flows/import` (see the export/import bullet below) with a hand-built minimal `ExportableFlow` — take a real export as a template, zero out `processors`/`connections`/`controllerServices`/etc., and give the `flowContent` a fresh `identifier`/`instanceIdentifier`. **`parameterContexts` must be a JSON array, even when empty — `{}` fails Jackson deserialization** (`Cannot deserialize value of type HashSet ... from Object value`, a `500` with the real cause only visible in the EFM pod's own logs, not the HTTP response body).
- **Cloning a flow from one class into another** (e.g. migrating an agent class): `GET .../designer/{sourceClassName}/flows/export` → `{exportableFlowFormat, flowContent, parameterContexts, agentManifest}` (a self-contained, re-importable document — different shape from `GET .../flows/{id}`, which carries extra version-control metadata not meant for reuse). `POST .../designer/{destClassName}/flows/import` with that same body creates a **real new flow** on the destination class (new `identifier`, same `rootProcessGroupIdentifier` as the source — confirmed a true structural clone, not a reference). The destination class needs a manifest compatible with the source content first — see the static-mapping bullet in §12 if you hit `"input agent manifest id ... is not equal to configured static mapping"`. This is the sanctioned way to port a flow between classes; it's what keeps a class recreation (§14's fallback) from reproducing the old class's problems, since you're never pointing the new class at the old `designerFlowId`.

**There is no whole-flow-document `PUT` endpoint. Don't guess one.** `PUT /efm/api/designer/flows/{flowId}` with the full modified `flowContent` fails at the routing layer (`HttpRequestMethodNotSupportedException: Request method 'PUT' is not supported`, a `500` before any business logic — nothing is written). The only write path is one `POST` per new processor and one `POST` per new connection, each returning the server-assigned `identifier` you use to wire the next connection. There is no batch/bulk create.

**Query Postgres, not the REST heuristics, for reliable online/offline status.** EFM's `operation` table has no automatic retention; a crash-looping agent can flood it (thousands of rows in hours), which hangs `/efm/api/operations` entirely and breaks anything reconstructing "which agents are online" from it — including EFM's own UI. A read-only query against `agent`(`agent_class`,`agent_state`,`last_seen`) joined to `device`(`ip_address`,`hostname`) is the durable source of truth.

**An agent-class name is not guaranteed to map to one physical machine.** A single class can have multiple separately-registered deployments (e.g. one GPU host, one CPU host running a stub with the same output schema). Don't assume a hardware/script mismatch in an exported flow is a bug without checking which agent identifier — which physical machine — you're actually looking at.

## 8. Canvas layout when building flows programmatically

Canvas layout is not an EFM-specific concern — it's the same discipline for every programmatic build, whether through the EFM Designer API or the NiFi REST API, because both use the same `position:{x,y}` model. The full technique (coordinate model, grounded constants, per-shape placement rules, worked example) and the honest caveat that it still needs a manual tidy pass live in **[`layout.md`](layout.md)**.

**Read `layout.md` before the first `POST .../processors`, not after the build reads cramped.** This pointer existing as a section title wasn't enough — fresh EFM builds skip it and land at the NiFi pitch anyway. The fix hardens the call site itself: inline the pitch numbers on the processor-create bullet in §7 above, and wire a PreToolUse hook to prompt on any processor-create/update carrying a `position` to state shape + pitch and match `layout.md` before the call lands. Treat that prompt as the reminder to actually open `layout.md`, not a box to click through.

## 9. EFM Resource Manager API

The correct way to get a script/asset onto an agent (vs `kubectl cp`-ing it directly):

- `POST /efm/api/resource-manager/resources/file` — multipart; query params `name` / `resourceType` (`ASSET`|`EXTENSION`) / `relativePathOnAgent` / `notes`, field `file`. Returns a SHA-512 `digest` — diff it against local `sha512sum` to confirm no drift.
- `PUT /efm/api/agent-class-resource-manager/{agentClass}/save` — body **must** be exactly `{"resourceIdsToBeAssigned":[...],"resourceIdsToBeUnassigned":[...]}`. A bare array or `{"resourceIds":[...]}` is silently swallowed (`200 OK`, nothing assigned).
- **No in-place asset update exists** (API or UI). Changing an assigned script's content is: unassign → delete the old resource → upload as new → reassign. A same-named re-upload does not overwrite the old bytes.
- A running MiNiFi C++ agent's `ExecuteScript` **re-reads its Script File from disk on every trigger** — a raw `kubectl cp` onto the asset path takes effect on the next call, no republish. Fast for iterating on content, but it bypasses EFM's asset tracking and won't survive a pod restart unless also pushed through the resource-manager flow above.

## 10. A note on agent networking

When an agent's `ListenHTTP` works locally but hangs from another machine, check two things before anything else:
- The listener is bound to `0.0.0.0`, not `127.0.0.1` — `netstat -ano | findstr :<port>` (Windows) / `ss -ltn` (Linux).
- The host firewall allows the port on the interface the remote machine arrives on. A firewall rule scoped to one profile (e.g. Windows `Private`) won't cover a VPN/overlay adapter that lands on `Public`; widen the rule's profile or add an interface-specific one.

## 11. A K8s MiNiFi agent can go silently dark — and if it's a bare pod, restarting it isn't a one-liner

Real incident: a pod's MiNiFi agent had **not heartbeated to EFM in 6 days** — `last_seen` in Postgres' `agent` table was stale, but the pod itself showed `Running 1/1`, 0 restarts, and its already-deployed flow kept working the whole time (MiNiFi C++ doesn't need EFM once a flow is deployed — only *new* pushes need a live heartbeat channel). Query `agent.last_seen`/`agent.agent_state` directly (per §"Query Postgres, not the REST heuristics" elsewhere in this doc) before assuming an EFM-side push will actually reach a live pod — a `200` from the resource-manager/flow-publish API only means EFM accepted the write, not that any agent received it.

**Fixing it isn't a simple restart if the pod is bare** (no `Deployment`/`StatefulSet`/`ReplicaSet` owner — check with `kubectl get pod ... -o jsonpath='{.metadata.ownerReferences}'`, empty means bare). `kubectl delete` on a bare pod does not get it rescheduled. Before deleting: save the exact original manifest from the `kubectl.kubernetes.io/last-applied-configuration` annotation (`kubectl get pod ... -o json`, pull that annotation, it's the full `kubectl apply`-able JSON) — this is what lets `kubectl apply` bring it back identically, deployer-curl args (including the agent's `agentIdentifier`) included, so it re-registers as the *same* EFM agent record rather than a new one.

**A fresh boot doesn't mean the flow's resources actually land on disk in time.** Even with a correctly-pulled `config.yml` (the new processor definitions were there immediately), the assigned asset *files* can still be missing (`/nifi-minifi-cpp-<ver>/asset/` empty, `.state` shows `{"digest": "", "assets": {}}`) — every `ExecuteScript` referencing them (including ones unrelated to whatever you just changed) fails to start, retries a **fixed 3 times, 30s apart, then gives up** (not an infinite retry loop). Fix: `kubectl cp` the asset file(s) directly onto the pod, then restart just the `minifi` process inside the container (find its PID, `kill` it, relaunch `./bin/minifi &` from the install dir) — much cheaper than another full pod delete/reapply, and it re-reads the already-correct `config.yml` cleanly now that the files actually exist.

**A bare pod's IP changes on every restart** (no stable `Service` in front of it). Any NiFi processor with that IP hardcoded in its `HTTP URL` (or any other property) breaks silently until updated — grep for the old IP across the flow, or budget for a real `Service` if the pod is going to need restarting more than once.

## 12. A property-shape change to an already-known processor name doesn't refresh EFM's cached manifest

EFM's manifest registration appears to key off the *set of processor type names* an agent class has already seen, not the manifest content/hash. If a device re-registers with a genuinely new `agentManifestHash` (confirmed via `GET /efm/api/agents/{id}`) but every processor *name* in it was already known to the class, EFM keeps serving the old `agentManifestId` — `GET /efm/api/agent-manifests/{id}` still returns stale `propertyDescriptors`/`typeDescription`, and `GET /efm/api/agent-classes/{class}/manifest-diff` reports `newManifestAvailable: false`. Not an HTTP cache (cache-busting and no-cache headers don't help), and deleting and re-registering the agent record doesn't mint a new manifest ID either.

This blocks configuring the processor's properties through the Designer: `.../validate` rejects any property not in the stale cached descriptor list, which blocks `/publish`.

**Workaround:** rename the processor (e.g. `UpdateAttribute` → `UpdateAttributeVerify`) so the class sees a new *name*, which reliably mints a fresh `agentManifestId` with correct content — then rename back if desired. This is an EFM server-side bug (Java/Postgres-backed manifest store), not fixable from the agent/flow side.

## 13. `InvokeHTTP` in a `HandleHttpRequest → InvokeHTTP → HandleHttpResponse` pair: a 5xx from the target can hang the client for 30+ seconds, or forever

Two compounding gotchas, found building a Jetson Java agent's HTTP-proxy pairs by copying an existing working pair (a `/classify` proxy) as a template:

1. **The `Retry` relationship (status 500-599) is not auto-terminated and easy to leave wired back to the processor itself** (a self-loop looks harmless when copying an existing template, since the working template only ever exercised the 2xx path). With no other destination, a flowfile routed to `Retry` loops forever — the client waiting on `HandleHttpResponse` never gets an answer, no matter how generous `InvokeHTTP`'s own socket timeouts are set. **Route `Retry` to the same terminal `HandleHttpResponse` as `No Retry`/`Failure`** (one connection can carry all three relationships) unless a real automatic-retry loop is actually wanted.

2. **Even after fixing that, every processor has a `penaltyDuration` scheduling field defaulting to 30 seconds — invisible in the Designer's property list**, since it's not a `properties` entry but a top-level field on the processor's `componentConfiguration` (`GET /efm/api/designer/flows/{flowId}/processors/{id}` shows it; confirm on the agent's own `flow.json.gz`, e.g. `penaltyDuration: 30000 ms`). `InvokeHTTP` penalizes the flowfile before routing it to `Retry`, so any 5xx adds a flat ~30s hang before the downstream `HandleHttpResponse` ever sees it — easy to misdiagnose as a hung connection or a bad `Socket Read Timeout` when the HTTP call itself actually completed in milliseconds. For a synchronous request/response pair that terminates on error rather than genuinely retrying, **set `penaltyDuration` to `0 sec`** via the same processor PUT (top-level field, alongside `properties`) — penalizing serves no purpose when nothing loops back.

Check both on *every* `HandleHttpRequest`/`InvokeHTTP` pair copied from a working template, not just new ones — a template that's only ever seen 2xx responses in testing (e.g. `/classify`) can carry this same latent bug unnoticed indefinitely.

## 14. Orphaned Resource assets cause a permanent `SYNC RESOURCE` failure loop — unassigning them isn't enough, and the dashboard won't tell you it's fixed

A second, distinct root cause for the same "Updated Agents" dashboard symptom as §4's failure mode — a hand-built deployer command reusing an `agentIdentifier`; this one is a resource-sync cache issue on an otherwise correctly-enrolled agent. Same visible red badge, two unrelated mechanisms — check which one you actually have before applying either fix.

Real incident: an agent class migrated from a C++ agent to a Java one, but 3 Python assets (`ExecuteScript` files the old C++ agent used) stayed **assigned** to the class in the resource manager. Java `ExecuteScript` can't run Python at all, so these were dead weight the moment the migration happened — and every ~10 minutes the live agent's heartbeat triggered a fresh `SYNC RESOURCE` operation that failed:

```
org.apache.nifi.c2.client.http.C2ServerException: Resource content retrieval failed with HTTP return code 500
	at org.apache.nifi.c2.client.http.C2HttpClient.retrieveResourceItem(...)
	at org.apache.nifi.minifi.c2.command.syncresource.DefaultSyncResourceStrategy...
```
(from the agent's own `minifi-app.log` — EFM's `operation.details` just says `"No resource items were retrieved, please check the log for errors"`, same "check the agent's log, not EFM's own claim" rule as everywhere else in this doc.)

**Diagnose which resources are the problem:**
```bash
curl -s http://<efm-host>:10090/efm/api/agent-class-resource-manager/<class>/assigned
```
Cross-check the file names against what the live agent's actual type can run — an asset left over from a since-migrated agent type (C++ script assigned to a now-Java-only class) is the classic orphan.

**Confirm the failing operation, straight from Postgres** (`operation` + `operation_arg`, same tables as the agent/device registry query elsewhere in this doc):
```sql
SELECT id, operand, state, details FROM operation
WHERE target_agent_id = '<agent-id>' ORDER BY created DESC LIMIT 5;

SELECT arg_key, arg_value FROM operation_arg WHERE operation_id = '<op-id>';
```
`operation_arg`'s `resourceList` value spells out exactly which resource IDs/URLs (`/c2-protocol/resource/{id}`) the agent was told to fetch, and `globalHash` is the digest EFM computed to decide a sync was needed.

**Unassigning via the documented API (§9) is necessary but was not sufficient.** `PUT /agent-class-resource-manager/<class>/save` with `resourceIdsToBeUnassigned` updates Postgres correctly — confirmed via `/assigned`, `/unassigned`, and `/resource-manager/resources/{id}/agent-classes` all reflecting the change immediately. But **the very next `SYNC RESOURCE` operation still failed with the byte-identical `resourceList` and `globalHash` digest as every failure before the unassign** — diffed directly via `operation_arg.arg_value`. EFM caches the per-class resource digest it uses to build this operation somewhere the assign/unassign REST call doesn't invalidate; the read endpoints are honest, the operation-generation path isn't.

**The actual fix:** restart the EFM pod.
```bash
kubectl rollout restart deployment/efm -n <ns>
kubectl rollout status deployment/efm -n <ns> --timeout=90s
```
This only restarts EFM's own JVM — Postgres and both PVCs (§2) are untouched, so nothing is lost; it exists purely to force EFM to drop its in-memory cache and reload from Postgres. Confirmed: the first `SYNC RESOURCE` operation after the restart came back `DONE` with an empty `resourceList`.

**Before restarting: check nothing is mid-flight, across *every* agent, not just the one you're fixing** — an EFM restart drops every connected agent's heartbeat channel for the ~10-20s it takes to come back:
```sql
SELECT target_agent_id, operation_type, operand, state, created
FROM operation WHERE state IN ('QUEUED', 'DEPLOYED');
```
Empty result = nothing mid-flight, safe to restart. Treat this exactly like any other live-service restart (`agent/incident-rules.md`) — confirm before doing it, fresh, every time.

**The "Updated Agents" dashboard health badge does not reflect any of this, in either direction.** It's bound to the class's most recent row in the `bulk_operation` table, not to live per-operation health:
```sql
SELECT * FROM bulk_operation WHERE agent_class_id = '<class>' ORDER BY updated DESC LIMIT 1;
```
A `bulk_operation` row is only created by a class-wide action (e.g. publishing a flow to the whole class) — routine per-agent `SYNC RESOURCE`/heartbeat retries never touch it. So a class can have every individual operation succeeding (confirmed via the `operation` table above) and still show red on the dashboard indefinitely, because nothing has run a fresh bulk action since the last one failed. Don't trust the dashboard tile as proof of health *or* of breakage — query `operation` and `bulk_operation` directly, same "REST/UI view is unreliable, Postgres is truth" rule as the rest of this doc.

**Clearing the badge for real: a plain republish is enough, no flow content change needed.** `GET .../flows/{flowId}/version-info` will likely show `dirty: false` (nothing to publish) — EFM's `POST .../flows/{flowId}/publish` still accepts it anyway, bumps `flowVersion`, and pushes a fresh `UPDATE CONFIGURATION` to every agent in the class. Confirmed end-to-end: this created a brand-new `bulk_operation` row that went `DONE`, and `agentClassHealthStatus` flipped to `GOOD` within seconds — no delete/recreate needed, and no observed disruption to the running agent (same `agentId`, same processors, heartbeat never dropped). **This is still a live config push to every agent in the class** — export the flow first (below) and confirm before doing it, same as any other redeploy.

**If a plain republish doesn't clear it** (the class is more deeply broken than a stale badge — e.g. the agent itself won't apply the config, or `UPDATE CONFIGURATION` keeps coming back `FAILED`), the fallback is delete-agent → delete-class → recreate. **The trap in that fallback: don't deploy a flow carried over from the old (deleted) class's designer-flow ID.** The old `designerFlowId` belongs to a retired class; pushing it at a freshly-recreated class either fails outright or drags forward whatever made the old class unhealthy in the first place. Build the new class's flow fresh in the Designer (or recreate it processor-by-processor via §7's API, same technique as any programmatic build) rather than pointing the new class at the old flow ID — and per §4, get the deployer command from `generateCommand`, never hand-built, with a fresh `agentIdentifier`. This combination (fresh identifier + fresh flow, never reused) is what keeps a class recreation from reproducing the exact failure it was meant to fix.

**Cheap insurance before either path:** export the class's live flow definition first (`GET /efm/api/designer/flows/{flowId}`, same pattern as `flow-api.md` §4's NiFi-side export) so a restart or republish that surfaces some other latent issue doesn't cost you the ability to see what the flow looked like going in.
