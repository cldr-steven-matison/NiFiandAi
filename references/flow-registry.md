# Flow Registry — Add/Update a Process Group without the root flow

Use this pattern any time you need to add or update a PG in a live environment. **Never read `flow.json.gz` to plan an addition.** The committed JSON in `files/` is the source of truth; the NiFi upload API is the deployment mechanism.

---

## The model

| Layer | What it is |
|---|---|
| `{owner}/{repo}/files/*.json` | Versioned flow registry — NiFi 2.x `VersionedFlowSnapshot` exports, committed to git |
| GitHub raw URL | Registry read API — fetch any commit of any flow without `git clone` |
| `POST .../process-groups/{parent_id}/process-groups/upload` | Deployment API — NiFi imports the file as a new child PG |
| k8s Secrets | Credential store — sensitive parameter values, mTLS certs, GitHub PATs |

---

## Step 0 — Source the flow file

**From disk (any session):**
```bash
FLOW_FILE=/path/to/your-repo/files/SomeFlow.json
```

**From GitHub raw URL (k8s Job or any shell without the repo cloned):**
```
https://raw.githubusercontent.com/{owner}/{repo}/<ref>/files/<FlowName>.json
```
- `<ref>` = a commit SHA for reproducible deploys, or `main` for latest.
- Private repo: pass `-H "Authorization: token $GH_PAT"` with a PAT stored as a k8s Secret.

```bash
GH_REPO={owner}/{repo}
GH_REF=main   # or a commit SHA for a pinned deploy
curl -sL -H "Authorization: token $GH_PAT" \
  "https://raw.githubusercontent.com/$GH_REPO/$GH_REF/files/SomeFlow.json" \
  -o /tmp/SomeFlow.json
```

---

## Step 1 — Pre-flight: does the PG already exist?

List all child PGs of the root (or any parent PG you're targeting):

```bash
curl -sk --cert $CERT --key $KEY \
  "$NIFI/nifi-api/process-groups/root/process-groups" \
  | jq -r '.processGroups[] | "\(.component.id) \(.component.name)"'
```

- **Name absent** → fresh import (Step 2A).
- **Name present** → upsert (Step 2B). Capture the existing `$PG_ID`.

To find the root PG ID if needed:
```bash
ROOT_PG_ID=$(curl -sk --cert $CERT --key $KEY \
  "$NIFI/nifi-api/process-groups/root" \
  | jq -r '.component.id')
```

---

## Step 2A — Fresh import

Identical to `flow-api.md §3`. The short form:

```bash
CLIENT_ID=$(uuidgen)
curl -sk --cert $CERT --key $KEY -X POST \
  "$NIFI/nifi-api/process-groups/$ROOT_PG_ID/process-groups/upload" \
  -F "positionX=100.0" \
  -F "positionY=100.0" \
  -F "groupName=SomeFlow" \
  -F "clientId=$CLIENT_ID" \
  -F "disconnectNode=false" \
  -F "file=@/tmp/SomeFlow.json"
```

Capture `NEW_PG_ID` from the response:
```bash
NEW_PG_ID=$(... | jq -r '.component.id')
```

Then start it (after binding a Parameter Context if needed — see Step 3):
```bash
curl -sk --cert $CERT --key $KEY -X PUT \
  -H "Content-Type: application/json" \
  "$NIFI/nifi-api/flow/process-groups/$NEW_PG_ID" \
  -d "{\"id\":\"$NEW_PG_ID\",\"state\":\"RUNNING\"}"
```

---

## Step 2B — Upsert (update an existing PG)

1. **Stop the PG:**
   ```bash
   curl -sk --cert $CERT --key $KEY -X PUT \
     -H "Content-Type: application/json" \
     "$NIFI/nifi-api/flow/process-groups/$PG_ID" \
     -d "{\"id\":\"$PG_ID\",\"state\":\"STOPPED\"}"
   ```

2. **Wait for queues to drain** — poll until `runningCount == 0`:
   ```bash
   until [ "$(curl -sk --cert $CERT --key $KEY "$NIFI/nifi-api/process-groups/$PG_ID" \
     | jq '.component.runningCount')" = "0" ]; do sleep 2; done
   ```

3. **Capture version, then delete:**
   ```bash
   VER=$(curl -sk --cert $CERT --key $KEY \
     "$NIFI/nifi-api/process-groups/$PG_ID" | jq -r '.revision.version')
   curl -sk --cert $CERT --key $KEY -X DELETE \
     "$NIFI/nifi-api/process-groups/$PG_ID?version=$VER"
   ```

4. **Re-import** as Step 2A.

---

## Step 3 — Parameter Context pre-create (before starting)

If the flow JSON uses `#{param_name}` references, create the Parameter Context *before* starting the PG. Pull sensitive values from a k8s Secret — never hardcode them.

```bash
# Pull values
CLIENT_ID=$(kubectl get secret nifi-params -n $NS \
  -o jsonpath='{.data.client_id}' | base64 -d)
CLIENT_SECRET=$(kubectl get secret nifi-params -n $NS \
  -o jsonpath='{.data.client_secret}' | base64 -d)

# Create Parameter Context
PC_ID=$(curl -sk --cert $CERT --key $KEY -X POST \
  -H "Content-Type: application/json" \
  "$NIFI/nifi-api/parameter-contexts" \
  -d "{
    \"revision\": {\"version\": 0},
    \"component\": {
      \"name\": \"SomeFlowParams\",
      \"parameters\": [
        {\"parameter\": {\"name\": \"client_id\",   \"sensitive\": false, \"value\": \"$CLIENT_ID\"}},
        {\"parameter\": {\"name\": \"client_secret\",\"sensitive\": true,  \"value\": \"$CLIENT_SECRET\"}}
      ]
    }
  }" | jq -r '.component.id')
```

Bind the PC to the PG (the PG must already exist, and be STOPPED):
```bash
PC_VER=$(curl -sk --cert $CERT --key $KEY \
  "$NIFI/nifi-api/process-groups/$NEW_PG_ID" | jq -r '.revision.version')

curl -sk --cert $CERT --key $KEY -X PUT \
  -H "Content-Type: application/json" \
  "$NIFI/nifi-api/process-groups/$NEW_PG_ID" \
  -d "{
    \"revision\": {\"version\": $PC_VER},
    \"component\": {
      \"id\": \"$NEW_PG_ID\",
      \"parameterContext\": {\"id\": \"$PC_ID\"}
    }
  }"
```

Then start as Step 2A.

> **Update an existing Parameter Context** — use `PUT /nifi-api/parameter-contexts/{id}` with the current `revision.version`. Never GET-then-PUT sensitive parameters (Rule 2 of the main skill). Write values directly; they export as `null` and are never echoed back.

---

## Step 4 — k8s Job deployment pattern

A self-contained Job that fetches a flow from GitHub and deploys it to NiFi without requiring any local tooling:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: nifi-deploy-someflow
  namespace: $NS  # set to your namespace
spec:
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: Never
      volumes:
        - name: nifi-cert
          secret:
            secretName: $NIFI_CERT_SECRET  # e.g. mynifi-cfm-operator-user-cert for CFM Operator
        - name: nifi-params
          secret:
            secretName: nifi-params
        - name: github-pat
          secret:
            secretName: github-pat
      containers:
        - name: deployer
          image: curlimages/curl:8
          env:
            - name: NIFI
              value: "https://<nifi-svc>.$NS.svc.cluster.local:8443"
            - name: CERT
              value: "/certs/tls.crt"
            - name: KEY
              value: "/certs/tls.key"
            - name: NS
              value: ""  # set to your namespace
            - name: GH_REPO
              value: "{owner}/{repo}"
            - name: GH_REF
              value: "main"
            - name: FLOW_NAME
              value: "SomeFlow"
          volumeMounts:
            - name: nifi-cert
              mountPath: /certs
              readOnly: true
            - name: nifi-params
              mountPath: /secrets/params
              readOnly: true
            - name: github-pat
              mountPath: /secrets/github
              readOnly: true
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              GH_PAT=$(cat /secrets/github/token)
              # 1. Fetch flow from GitHub
              curl -sL -H "Authorization: token $GH_PAT" \
                "https://raw.githubusercontent.com/$GH_REPO/$GH_REF/files/${FLOW_NAME}.json" \
                -o /tmp/flow.json

              # 2. Get root PG ID
              ROOT_PG_ID=$(curl -sk --cert $CERT --key $KEY \
                "$NIFI/nifi-api/process-groups/root" | jq -r '.component.id')

              # 3. Check if PG already exists
              EXISTING_ID=$(curl -sk --cert $CERT --key $KEY \
                "$NIFI/nifi-api/process-groups/$ROOT_PG_ID/process-groups" \
                | jq -r --arg name "$FLOW_NAME" \
                  '.processGroups[] | select(.component.name==$name) | .component.id')

              # 4. Upsert: delete existing if present
              if [ -n "$EXISTING_ID" ]; then
                curl -sk --cert $CERT --key $KEY -X PUT \
                  -H "Content-Type: application/json" \
                  "$NIFI/nifi-api/flow/process-groups/$EXISTING_ID" \
                  -d "{\"id\":\"$EXISTING_ID\",\"state\":\"STOPPED\"}"
                sleep 5
                VER=$(curl -sk --cert $CERT --key $KEY \
                  "$NIFI/nifi-api/process-groups/$EXISTING_ID" | jq -r '.revision.version')
                curl -sk --cert $CERT --key $KEY -X DELETE \
                  "$NIFI/nifi-api/process-groups/$EXISTING_ID?version=$VER"
              fi

              # 5. Import flow
              CLIENT_ID=$(cat /proc/sys/kernel/random/uuid)
              NEW_PG_ID=$(curl -sk --cert $CERT --key $KEY -X POST \
                "$NIFI/nifi-api/process-groups/$ROOT_PG_ID/process-groups/upload" \
                -F "positionX=100.0" -F "positionY=100.0" \
                -F "groupName=$FLOW_NAME" \
                -F "clientId=$CLIENT_ID" \
                -F "disconnectNode=false" \
                -F "file=@/tmp/flow.json" | jq -r '.component.id')

              # 6. Start
              curl -sk --cert $CERT --key $KEY -X PUT \
                -H "Content-Type: application/json" \
                "$NIFI/nifi-api/flow/process-groups/$NEW_PG_ID" \
                -d "{\"id\":\"$NEW_PG_ID\",\"state\":\"RUNNING\"}"
              echo "Deployed $FLOW_NAME as $NEW_PG_ID"
```

To parameterize with a PC, add the Step 3 curl calls between steps 5 and 6 in the script.

---

## Secret manager options

All three reduce to "k8s Secret exists → Job mounts it." The Job YAML above is unchanged regardless of which backend provides the Secret.

| Backend | Mechanism |
|---|---|
| **k8s Secrets** (default) | Create manually or via `kubectl create secret generic` |
| **HashiCorp Vault** | Vault Agent Injector sidecar writes secrets as files at `/vault/secrets/`; mount the annotations, not an explicit volume |
| **AWS Secrets Manager** | External Secrets Operator (ESO) syncs ASM secrets as k8s Secrets; same volume mount pattern once the `ExternalSecret` CR is applied |

---

## NiFi Registry (optional next level)

If a `NifiRegistry` CR is deployed (see the [NiFi Registry docs](https://nifi.apache.org/docs/nifi-registry-docs/)), use `nipyapi.versioning` to:
- Push a committed `files/*.json` into a Registry bucket as a named snapshot
- Version flows with live-environment semantic versions (not just git SHAs)
- Roll back a PG to a prior Registry version via the NiFi UI without a re-import

Git `files/` remains the source of truth; Registry is the live runtime versioning layer. Not required for the GitHub-as-registry pattern above — add it when you need live rollback without the UI.
