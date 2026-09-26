# Debugging + cross-cutting wire-up gotchas

## Cross-cutting wire-up gotchas

- **Kafka external NodePort vs. internal port.** `PublishKafka`/`ConsumeKafka` bootstrap must be the external broker port when the flow runs *outside* the cluster (edge MiNiFi). It's `9092`/`9093` from *inside* the cluster.
- **Broker `advertisedHost` for cross-network access.** For consumers on a different network (VPN/overlay/LAN), each broker's advertised host must be a DNS name those consumers can resolve — not the raw pod IP. With Strimzi, patch `spec.kafka.listeners[].configuration.brokers[N].advertisedHost`; a rolling restart follows automatically.
- **The NiFi pod clock is UTC.** Any cron-driven processor (`GenerateFlowFile`, scheduled `InvokeHTTP`) fires on UTC. A "3pm–9pm EST" window is a different set of hours in UTC. The pod does not honor `TZ` unless the StatefulSet is patched.
- **Internal service DNS from another namespace.** Address services cross-namespace as `<svc>.<ns>.svc.cluster.local`. A NodePort address is external-only and will time out from an in-cluster `InvokeHTTP` — use the ClusterIP service DNS there.
- **A `#{parameter}` inside a quoted EL string literal is never substituted.** `${name:equals('A'):ifElse('#{Target A}', '')}` yields the literal text `#{Target A}`, and in a URL the `#` starts a fragment, so the request silently becomes `GET /nifi-api/process-groups/` → 404 with no bulletin. Keep parameters outside the EL (`#{Clips Dir}/${clip_id}.mp4`), or bind each one to an attribute with a plain reference in one `UpdateAttribute` and pick among the attributes with EL in the next (`UpdateAttribute` evaluates all its properties against the incoming attributes, so the pick can't happen in the same processor).
- **`EvaluateJsonPath` does not evaluate EL in its JsonPath properties.** `$.processors[?(@.component.name=='${gen.name}')].id` filters on the literal string `${gen.name}`, matches nothing, and still routes `matched` with an empty result. Use a static path per case and choose downstream with EL (`${gen.name:equals('X'):ifElse(${id.X}, …):jsonPath('$[0]')}`).
- **An `InvokeHTTP` 4xx leaves no trace but the request log.** `No Retry` raises no bulletin, and provenance may be forbidden to your user. The pod's `request-log` sidecar (`kubectl logs <nifi-pod> -c request-log --since=10m`) has every call NiFi made to its own API, with the status code: that is how a 404 on a malformed URL gets found.
- **Port-forward layout files don't retroactively apply.** Editing a terminal-multiplexer layout (zellij/tmux) does not add a pane to an already-running session. Reload or start a new session, or the "new" port-forward simply isn't listening.

## 10-step debugging checklist

Silent data loss in NiFi is almost always one of these. Work top to bottom:

1. **Is the pod actually the version you think it is?** `kubectl describe pod <nifi-pod> | grep Image:` — the CR's `nifiVersion` label can differ from the real image tag.
2. **Is the extension loaded?** `kubectl exec <nifi-pod> -- ls /opt/nifi/nifi-current/python/extensions/` — is your `.py` / built wheel actually present?
3. **Did the Python subprocess reload?** `kubectl logs <nifi-pod> -c nifi | grep -i "python\|extension"` — look for a fresh startup line *after* your last change.
4. **What relationships are auto-terminated?** Dump the live flow:
   ```bash
   # `data/`, not `conf/`, on the CFM-operator pods - resolve it from nifi.properties (SKILL.md rule 1)
   kubectl exec <nifi-pod> -c nifi -- gunzip -c /opt/nifi/nifi-current/data/flow.json.gz \
     | jq '.rootGroup.processGroups[] | select(.name=="MyPG") | .processors[] | {name, autoTerminatedRelationships}'
   ```
   Silent drops are almost always a `Retry` / `Failure` / `unmatched` relationship auto-terminated here.
5. **Are attributes actually populated?** Add a `LogAttribute` on the failing edge and watch the app log — the fastest way to catch an `EvaluateJsonPath` typo.
6. **Is `ListenHTTP`'s `Batch Size`/`Buffer Size` set to `1/1`?** (See `patterns.md` — the MiNiFi fire-and-forget router.)
7. **Is `InvokeHTTP`'s method really `POST`?** Check the persisted value, not what you intended.
8. **Do the bootstrap ports match the actual Kafka listener?** External NodePort ≠ in-cluster `9092`.
9. **Is the pod's clock UTC or your local TZ?** For any cron-scheduled processor.
10. **Did credentials suddenly stop working after a UI edit?** You probably GET-then-PUT a sensitive property and wrote `********` back over it. Re-hydrate from the source-of-truth values via a Parameter Context (rule 2 in `SKILL.md`).
