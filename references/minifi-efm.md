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

## 4. Deploying an agent (the deployer curl)

Same shape for every arch — swap `agentType` / `agentVersion` / `osArch`:

```bash
curl -L \
 -d agentClass=MyClass \
 -d agentIdentifier=$(cat /proc/sys/kernel/random/uuid) \
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

- **Windows:** `Invoke-WebRequest ... | Invoke-Expression` from PowerShell **as Administrator**. Do **not** run it from `C:\WINDOWS\system32` — the deployer installs to `$PWD` and system32 is a permission nightmare. `cd` to a clean dir first.

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
- `POST .../process-groups/{pgId}/processors` — create. Body: `{"revision":{"version":0,"clientId":...},"componentConfiguration":{"componentType":"PROCESSOR","type":"<fqcn>","bundle":{...},"name":...,"position":{...},"properties":{...},"autoTerminatedRelationships":[...]},"requestId":"<uuid>"}`. Properties can be set in this one call. **Before you send the `position`:** on an EFM Designer build, row pitch is **300** (not the NiFi 200) and branch/column pitch **~600–900** (not ~300–480), and a linear chain is **vertical** (constant x, `y += 300`) — a `(0,0)→(400,0)` sideways pair is the flagged-bad shape that repeatedly lands cramped. State your intended shape + pitch and match it against [`layout.md`](layout.md) §"Per-shape placement rules" first; a PreToolUse hook can prompt you to do exactly this on the `POST`.
- `POST .../connections` — same envelope, `componentConfiguration:{componentType:"CONNECTION",source:{id,type:"PROCESSOR",groupId},destination:{...},selectedRelationships:[...],bends:[]}`.
- `PUT .../processors/{id}` — update, same shape; `revision.version` must match current.
- `GET .../flows/{id}/validate` → `{"validationErrors":[]}` — confirm empty before publishing.
- `POST .../flows/{id}/publish` — body `{"comments":"..."}`. **This is the real push-to-agent step** — it overwrites even a manually hand-edited agent-local `config.yml` on the agent's next heartbeat. A hand-edited local config is never authoritative once you use the real API.
- `DELETE /efm/api/agents/{id}` — removes a stale/`MISSING` agent record EFM never garbage-collects on its own.

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
