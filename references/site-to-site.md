# Site-to-Site & secure-cluster rollout (CFM NiFi operator)

Site-to-Site (S2S) moves FlowFiles NiFi-to-NiFi or MiNiFi-to-NiFi over a dedicated protocol (RAW or
HTTP), authenticated by client cert rather than the flow's normal ingress path. On the CFM operator
this only works from a securely-authorized cluster — the rollout below is a package deal with S2S.

## 1. When you need S2S (and when you don't)

Use S2S when the producer is a separate NiFi/MiNiFi instance (edge agent, foreign cluster, a
`SiteToSiteReportingRecordSink` metrics relay) handing FlowFiles to this NiFi without a plain HTTP
endpoint. Don't reach for it inside one flow — a same-cluster PG-to-PG connection or
`InvokeHTTP`/`ListenHTTP` is simpler and needs none of the cert/authorizer machinery below. It earns
its cost only at a real process boundary: MiNiFi C++/Java agent → NiFi, or NiFi → NiFi across clusters.

## 2. Cluster prerequisites: `userCertAuth`, one CA chain, `initialAdminIdentity` = SAN

`singleUserAuthorizer` (username/password) has no access-policy mechanism — `/policies` returns
`409` — so it can't grant a peer "receive via site-to-site." Secure S2S needs a managed authorizer:

```yaml
spec:
  security:
    initialAdminIdentity: "nifi-admin"        # SAN-mapped identity, IMMUTABLE after creation
    userCertAuth:
      verificationCASecret: cert-manager/cfm-operator-ca-tls
    nodeCertGen:
      issuerRef: { name: cfm-operator-ca-issuer-signed, kind: ClusterIssuer }
    s2sCertGen:
      issuerRef: { name: cfm-operator-ca-issuer-signed, kind: ClusterIssuer }
  configOverride:
    nifiProperties:
      upsert:
        nifi.remote.input.host: <nifi-pod>.<nifi-svc>.$NS.svc.cluster.local   # e.g. mynifi-0.mynifi.<ns>.svc.cluster.local
        nifi.remote.input.secure: "true"
        nifi.remote.input.http.enabled: "true"
```

Set `userCertAuth` + `initialAdminIdentity` **at CR creation** — it's immutable after, so getting it
wrong costs a delete+recreate.

`initialAdminIdentity` is a login seed, not a runtime flow-author: it gets an admin who can read
policies and list users, not one who can create process groups or ports (`403 No applicable
policies could be found`) — the operator owns authoring from here on (§4).

**Identity maps by SAN, not subject DN.** NiFi 2.6.0 under the operator uses
`SANX509PrincipalExtractor` — a cert with no SAN gets `HTTP 500 At least one Subject Alternative
name must be provided` on every request. A cert's NiFi identity is its `dnsNames`/SAN entry, not its
`CN`. Give every cert (admin, peer, node) `dnsNames` equal to the identity it should authenticate
as, and set `User.spec.identity`/`initialAdminIdentity` to that same string.

## 3. Issuer chain that works (cluster-issuer first; selfSigned never)

One CA has to sign everything — node certs, the S2S transport cert, every client cert — because
`verificationCASecret` only trusts that one CA:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: cfm-operator-ca-issuer }
spec: { selfSigned: {} }
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: { name: cfm-operator-ca, namespace: cert-manager }
spec:
  isCA: true
  commonName: cfm-operator-ca
  secretName: cfm-operator-ca-tls
  issuerRef: { name: cfm-operator-ca-issuer, kind: ClusterIssuer, group: cert-manager.io }
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: { name: cfm-operator-ca-issuer-signed }
spec:
  ca: { secretName: cfm-operator-ca-tls }
```

Apply before the operator/`Nifi` CR. `nodeCertGen`, `s2sCertGen`, and every peer `Certificate`
reference `cfm-operator-ca-issuer-signed`; the CR's `verificationCASecret` is the same
`cfm-operator-ca-tls`. **Never use the operator's own `selfSigned` issuer for a peer/admin cert** —
a cert off any other issuer isn't in NiFi's truststore, and the S2S handshake fails
`certificate unknown`.

## 4. Peers as `User` CRs — never hand-POST policies

The operator reconciles users/groups/policies from `User`/`UserGroup`/`AccessPolicyProfile` CRs
(`apiVersion: cfm.cloudera.com/v1alpha1`) and owns `authorizations.xml`. Trap: `500 Unable to save
Authorizations` on a hand-POSTed policy → the operator owns the policy file → declare the policy as
a `User` CR instead. A hand-POST can also tear the file and crash-loop the pod; recovery is moving
`authorizations.xml`/`users.xml` aside and letting NiFi rebuild from the `authorizers.xml` seed.

Fixed sequencing — the port UUID is a chicken-and-egg:

1. **Flow-author `User`** — write on `/flow`, `/controller`, `/process-groups/root`, so the input
   port can be created at all.
2. **Create the input port** (+ a downstream connection — a port with nothing downstream is invalid
   and won't start), enable S2S input, read the port's UUID.
3. **Peer `User`** — write on `/data-transfer/input-ports/<uuid>`, read on `/site-to-site`.

```yaml
apiVersion: cfm.cloudera.com/v1alpha1
kind: User
metadata: { name: s2s-peer, namespace: $NS }
spec:
  identity: s2s-peer
  instanceTarget: { kind: Nifi, name: <nifi-cr-name>, namespace: $NS }
  accessPolicies:
    - actions: [read]
      resources: [/site-to-site]
    - actions: [write]
      resources: [/data-transfer/input-ports/<port-uuid>]
```

`certificate.generate: true` on the `User` CR is a **no-op in CFM operator 3.0.0 (b126)** — no secret, no
`Certificate` CR, nothing in the operator log. Mint the peer cert yourself, off the same issuer as
§3, `dnsNames` equal to `spec.identity`:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: { name: s2s-peer-cert, namespace: $NS }
spec:
  secretName: s2s-peer-cert
  commonName: s2s-peer
  dnsNames: [s2s-peer]
  usages: [digital signature, key encipherment, client auth]
  issuerRef: { name: cfm-operator-ca-issuer-signed, kind: ClusterIssuer, group: cert-manager.io }
```

An admin `User` follows the same shape with a broader grant (`read`+`write` on `/flow`,
`/controller`, `/process-groups/root`, `/tenants`, `/policies`, `/parameter-contexts`,
`/provenance`, `/counters`).

## 5. Transport: HTTP not RAW; `nifi.remote.input.*` keys; RPG targets a port by instance id

Two transports: **RAW** (its own socket, `nifi.remote.input.socket.port`) and **HTTP** (tunnelled
over the existing HTTPS port). Use **HTTP** — it rides the port already exposed for the UI/API, no
new socket past the pod-IP binding. Required properties are the three in §2's `configOverride`.

The MiNiFi RPG (or a NiFi-side RPG) references the target port by its **instance id** — the port's
UUID, not its name (`Input Port Name` is name-keyed only on the reporting-sink CS in §7). Trap:
MiNiFi C++'s strict YAML spells the RPG key differently across its own docs — `Remote Process
Groups` (RAW example) vs `Remote Processing Groups` (HTTP example); pin the exact key against the
agent version at build time. `Remote Processing Groups: []` must be present even when empty, and
every component needs an explicit UUID `id`.

## 6. Proving transit (a FOREIGN peer committed a transaction — queue count before/after)

A same-cluster "self-peer" RPG proves the wiring but not real peer authorization. The real proof is
a separate identity, from a separate client, completing the S2S transaction protocol and moving the
connection's queued count:

```
GET  /site-to-site                                             -> 200, lists the port (e.g. "s2s-in")
POST /data-transfer/input-ports/<port-uuid>/transactions        -> 201
POST /data-transfer/input-ports/<port-uuid>/transactions/<id>/flow-files  -> 202 (crc checked)
DELETE .../transactions/<id>?responseCode=12&checksum=<crc>      -> 200 {"flowFileSent":1}
```

Confirm the downstream connection's `queuedSize` incremented (e.g. `67 → 68`); cross-check
server-side with `GET /policies/write/data-transfer/input-ports/<id>` (peer identity in `users`) and
the operator log (`Created access policy … /data-transfer/input-ports/<id>` and `… /site-to-site`
for that user). Verify FlowFile **content**, not just that one arrived.

## 7. S2S as a metrics sink (`SiteToSiteReportingRecordSink`)

A different entry into the same transport: `PutRecord` writing through the controller service
`org.apache.nifi.reporting.sink.SiteToSiteReportingRecordSink` — not a ReportingTask. Same S2S
session mechanics as an RPG (mTLS, `User` CR authorization), just initiated by a controller service.

| Property | Required | Notes |
|---|---|---|
| `record-sink-record-writer` | Yes | A `RecordSetWriterFactory` CS, e.g. `JsonRecordSetWriter` |
| `Destination URL` | Yes | Target NiFi's URL |
| `Input Port Name` | Yes | Matches the target port **by name**, not id |
| `SSL Context Service` | No default, **not inherited** | Unlike `nifi.minifi.flow.use.parent.ssl`, this CS has its own explicit SSL property — unset means unsecured. Wire a `RestrictedSSLContextService` with the client cert/truststore or an operator NiFi rejects the session |
| `s2s-transport-protocol` | Yes | `RAW` or `HTTP` — match §5, use `HTTP` |
| `Instance URL` | Yes | Cosmetic (event Content-URI); default is fine |
| `Compress Events` | Yes | Default `true` |
| `Communications Timeout` | Yes | Default `30 secs` |
| `Batch Size` | Yes | Default `1000` |
| `proxy-configuration-service` | No | Only for `HTTP` transport behind a proxy |

Shape: `GenerateFlowFile`/`ExecuteStreamCommand` → `PutRecord` (Record Sink =
`SiteToSiteReportingRecordSink`). Controller services are one of the few things EFM Designer can
manage (`controllerServices` is a real key in `flowContent`), so this is buildable from an agent
flow too.

## 8. Traps table

| Symptom | Cause | Fix |
|---|---|---|
| `/policies` → `409` | `singleUserAuthorizer` has no access-policy mechanism | Switch to `userCertAuth`, set at CR creation |
| Grant lands on a string nobody authenticates as | Identity set to subject DN instead of SAN | `User.spec.identity`/`initialAdminIdentity` = the cert's `dnsNames` entry |
| `HTTP 500 At least one Subject Alternative name must be provided` | Client cert has no SAN | Every cert needs `dnsNames: [<identity>]` |
| `403 No applicable policies could be found` creating a PG as the seeded admin | `initialAdminIdentity` seeds a login, not a flow-author grant | Declare a flow-author `User` CR (write on `/flow`, `/controller`, `/process-groups/root`) |
| `500 Unable to save Authorizations`, sometimes followed by a crash-loop | Operator owns `authorizations.xml`; a runtime POST can't persist and can tear the file | Declare via `User`/`UserGroup`/`AccessPolicyProfile` CR; never `POST /policies` |
| `certificate unknown`/PKIX failure in the S2S handshake | Peer cert minted off an issuer other than `verificationCASecret`'s | Mint every cert off the same `ClusterIssuer` as the node certs — never the operator's `selfSigned` issuer |
| `User` CR applied, no cert/secret appears | `certificate.generate: true` is a no-op in CFM operator 3.0.0 (b126) | Mint the peer cert yourself with cert-manager off the same CA |
| Peer policy references a port that doesn't exist | Port UUID needed in the peer's `accessPolicies` resource path | Fixed order: flow-author `User` → create port → peer `User` with the real UUID |
| Input port created but won't start | A port with no downstream connection is invalid | Give it a downstream connection (a funnel is enough) before starting |
| `SiteToSiteReportingRecordSink` session unauthenticated/rejected | Assuming `use.parent.ssl`-style inheritance applies | Set its `SSL Context Service` explicitly — no parent-SSL fallback |
