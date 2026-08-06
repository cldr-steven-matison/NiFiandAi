# Custom processors (Java NiFi 2.x)

**Before writing one, check that you need it.** Rule 9 in `SKILL.md`: decompose "poll → check → act" logic into a chain of stock processors (see the native-chain pattern in `patterns.md`), and reach for custom Python only for the one thing NiFi genuinely can't do natively — e.g. holding a persistent external socket, or a transform no stock processor covers. A custom processor that re-implements timers/branching/state internally is the wrong shape.

Python processors do **not** exist in NiFi 1.x. If your palette shows no custom processor even though the CR claims a 2.x version, verify the pod's actual image tag (`kubectl describe pod <nifi-pod> | grep Image:`) — the tag can lag the label.

## Two base classes cover 99% of cases

- `FlowFileSource` — generates FlowFiles from nothing (schedulers, generators).
- `FlowFileTransform` — reads one FlowFile in, writes one out.

Minimum viable transform:

```python
from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult

class MyProc(FlowFileTransform):
    class Java:
        implements = ['org.apache.nifi.python.processor.FlowFileTransform']
    class ProcessorDetails:
        version = '0.0.1'
        description = 'One-line summary'
        dependencies = ['requests-oauthlib==1.3.1']   # pip deps NiFi installs on load
    def __init__(self, **kwargs): pass
    def transform(self, context, flowfile):
        # ... do work ...
        return FlowFileTransformResult(relationship='success', attributes={...}, contents=b'...')
```

## Defensive contract for every processor

- Route errors to a `failure` relationship — never crash. Call `getLogger().error(...)` so it surfaces in the app log.
- Sensitive credentials → **Parameter Context**, referenced as `#{param-name}`. Never a literal in the property.
- Add a `Dry Run` boolean property (default `true`) that logs the intended action instead of performing the destructive one.
- Bump `ProcessorDetails.version` on every real change so you can see in the palette that the reload actually landed.

## The mixed-template EL trap

**`PropertyValue.evaluateAttributeExpressions(flowfile).getValue()` does not reliably evaluate a template that mixes literal text with multiple `${attr}` tokens** in the Python processor binding. A property value like `${streamer} is now showing on ${screen}.` evaluated to just the first token's value alone — silently dropping the literal text and the second token.

This is a limitation of the NiFi Python binding, not a config mistake, and it does **not** match Java-side NiFi EL (which handles multi-token templates fine, e.g. `ReplaceText`). Don't assume Python-processor EL behaves like Java NiFi EL.

For any Python-processor property that mixes literal text with more than one attribute reference, **evaluate it yourself**: pull the raw string with `.getValue()` (no `evaluateAttributeExpressions`) and substitute manually:

```python
import re
attrs = dict(flowfile.getAttributes())
template = context.getProperty('My Property').getValue()   # raw, un-evaluated
result = re.sub(r'\$\{(\w+)\}', lambda m: attrs.get(m.group(1), ''), template)
```

A property that's a single bare `${attr}` with no surrounding text may evaluate fine either way — this bug specifically bites *mixed* templates.

## Deploying custom Python processors — three real paths

Pick by host constraints; all three mount code at `/opt/nifi/nifi-current/python/extensions`.

1. **`minikube mount` (hot reload, best for iterative dev):**
   ```bash
   minikube mount ~/nifi-custom-processors:/extensions --uid 10001 --gid 10001   # keep terminal open
   ```
   In the `Nifi` CR, add a `hostPath` volume for `/extensions` and mount it at the extensions dir. Reliable on native-driver minikube; flaky on WSL2/docker-driver — use path 2 there.
2. **PVC + loader pod (where `minikube mount` is flaky):** create an extensions PVC, mount it into a small `ubuntu` pod, `kubectl cp` the wheel/`.py` in, then mount the PVC at the extensions dir. Also set `nifi.python.extensions.directories` in the CR's `configOverride`.
3. **NAR + PVC (Java processors):**
   ```bash
   mvn archetype:generate -DarchetypeGroupId=org.apache.nifi \
     -DarchetypeArtifactId=nifi-processor-bundle-archetype -DarchetypeVersion=2.4.0 ...
   mvn clean install -Denforcer.skip=true
   kubectl cp target/*.nar <loader-pod>:/home/ubuntu/nars/
   ```
   Then reference a `custom-nars` PVC via `narProvider.volumes` in the `Nifi` CR.

## Rebuild → redeploy discipline

A custom Python processor is **not** a hot patch. Every change requires all of:

1. Build the artifact (`hatch build` → `dist/*.whl`, or `kubectl cp` the `.py` directly for a fast dev loop).
2. Copy it onto the mounted extensions volume (paths above).
3. **Bump `ProcessorDetails.version`** — NiFi tracks bundles by this string; a same-version overwrite may not register as a new bundle at all.
4. **Explicitly switch every already-running instance to the new bundle version.** Dropping the new file on disk only makes NiFi *aware* of it (`GET` on the processor shows `multipleVersionsAvailable: true`); a running instance stays pinned to its old bundle until you force it: stop the processor → `PUT /processors/{id}` with `component.bundle.version` set to the new version string → restart. **If the processor has any sensitive property (check `descriptors[...].sensitive` first) and it's a literal value, not a Parameter Context reference, do this switch by hand in the NiFi UI (stop → change the Bundle dropdown → start) instead of scripting the PUT.** `GET` always returns a sensitive property masked as `"********"`; a scripted full-entity PUT round-trips that mask back as the literal value and destroys the real credential — this has happened more than once. The UI's version-switch doesn't round-trip masked fields the same way.
5. **After the switch, the processor shows transient red "'Processor' is invalid because Initializing runtime environment for the Processor."** This is normal — NiFi is spinning up the new bundle's Python environment. Wait, then refresh the canvas; it clears to valid on its own. Don't try to "fix" it by re-editing the processor while it's in this state.

This is **not** how MiNiFi C++'s `ExecuteScript` behaves — that re-reads its script file from disk on every trigger, no restart needed. Don't assume the two behave alike just because both are "a script NiFi runs."

## Apache upstream Python extensions

Clone `apache/nifi-python-extensions` and mount its `src/extensions/` the same way. `ChunkDocument`, `ParseDocument`, `PromptChatGPT`, `QueryOpenSearchVector`, and `QueryQdrant` load reliably. The full palette advertised in the repo README does **not** all load in every version — treat it as "a handful ship reliably, the rest are a gamble."
