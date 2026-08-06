# NiFi flow patterns that ship

## NiFi-as-HTTP-API (Java NiFi only)

`HandleHttpRequest` → your logic → `HandleHttpResponse`.

- `HandleHttpRequest` starts an embedded Jetty on the port you choose; use a regex on `Allowed Paths` to scope it.
- Both processors **must share the same `HttpContextMap` controller service** — that's what pairs a response back to its request.

This is the standard way to expose a NiFi flow as a synchronous REST endpoint.

**Early-ack variant — don't make the caller wait on the whole pipeline.** `HandleHttpResponse` transfers the flowfile to `success` after the response is flushed, so it doesn't have to be the last processor. Put it immediately after `HandleHttpRequest` and continue the flow from its `success`:

```
HandleHttpRequest → HandleHttpResponse (200, immediate ack) → …rest of the flow
```

That gives fire-and-forget latency for the caller while the real work continues behind it — the same profile as the MiNiFi C++ router below, but by choice rather than by limitation, and reversible per flow when you *do* want to hold the connection and return a real payload. Confirmed on the MiNiFi Java agent: a POST returned a real 200 in ~84ms and the FlowFile independently reached the next processor afterward — the response genuinely flushes before downstream work has to finish, not just before it starts.

## MiNiFi C++ fire-and-forget router

MiNiFi C++ has no request/response pair, so a flow that "answers" an HTTP call has to answer out-of-band:

```
ListenHTTP (:8080, /contentListener)
  → EvaluateJsonPath (request_id: $.request_id)
  → InvokeHTTP (POST to a local inference server)
  → PublishKafka (Kafka key = ${request_id})
```

The HTTP caller gets an empty 200 immediately; the real reply lands on a response Kafka topic keyed by `request_id`, which the caller consumes separately.

**Real bugs this pattern hides:**
- **`ListenHTTP` `Batch Size`/`Buffer Size` default to `5/5`.** A single request never fires the buffer-full path and is silently dropped (`buffer is NOT full 1/5` in the log). Set both to `1`. **A `1/1 buffer is NOT full ... was dropped` line after that is not proof of a real drop by itself** — a controlled count-at-both-ends test (sender count vs. what actually reached the process behind it) on a `1.26.02` agent found the warning fires on every request while 20/20 were delivered and processed normally. Don't diagnose this leg by grepping the log for `was dropped`; count at both ends.
- **`InvokeHTTP`'s `HTTP Method` persists as `GET`** even when you meant `POST`, unless you explicitly set the field. Verify the persisted value, not your intent.
- **`PublishKafka`'s `Known Brokers` must be the external broker port** when the agent is outside the cluster, not the in-cluster `9092`.
- **`EvaluateJsonPath` for a field on a JSON object is `$.request_id`**, not `$[0]` (that's array-index for a top-level array).
- **Multipart request bodies** (e.g. file upload for transcription): `EvaluateJsonPath` can't reach `request_id` inside them. Instead set `ListenHTTP`'s `HTTP Headers to receive as Attributes (Regex)` to `request_id` and have the caller send it as an HTTP header.

## Ingest → Kafka → Transform → Sink (the RAG shape)

The canonical shape for feeding an LLM/vector pipeline from NiFi:

```
Ingest:        ListenHTTP :9000/contentListener → RouteOnAttribute → PublishKafka(new_documents | new_audio)
Transcribe:    ConsumeKafka new_audio      → InvokeHTTP <whisper>/transcribe → PublishKafka new_documents
Embed + store: ConsumeKafka new_documents  → embed → <vector-store> upsert
```

- Set `concurrentlySchedulableTaskCount=3` on `InvokeHTTP` / `PublishKafka` only when the downstream can genuinely take N in parallel.
- Leave `ConsumeKafka` at 1 concurrent task if the topic is single-partition — more won't help and complicates ordering.

## Native FlowFile chain for a poll → check → act loop (rule 9 made concrete)

When the task is "poll something, check a condition per item, then act," the shape is a chain of stock processors — **not** one custom Python processor running its own timer/state/decision loop internally (rule 9 in `SKILL.md` explains why: a monolith gives up per-stage queues, provenance, mid-flow inspection, and can leak a background thread that outlives NiFi's lifecycle).

The shipped shape (a periodic live-streamer poll → per-item fan-out):

```
GenerateFlowFile (timer)  → InvokeHTTP (fetch the roster/list)
  → SplitJson (one FlowFile per item)
  → RouteOnContent / RouteOnAttribute (branch per type)
  → ExtractText / EvaluateJsonPath (pull fields) → InvokeHTTP (per-item status check)
  → RouteOnAttribute (is_live?) → act (post / add to watchlist / …)
```

- **NiFi's own scheduler drives cadence** via the `GenerateFlowFile`'s `Run Schedule` — never an internal `while`/`sleep`.
- Each `InvokeHTTP` self-loops its `Retry` relationship (rule 7) and routes terminal `No Retry`/`Failure` to a log processor.
- Reach for custom Python only for the one thing NiFi genuinely can't do natively (e.g. holding a persistent external socket) — see `custom-processors.md`.

## GUI-less edge agent → native host process bridge

Some edge targets have no path to a real GUI — a pod inside a Kubernetes cluster on a Windows/WSL2 host has no `DISPLAY`, no X socket, and the container runtime won't expose one. Don't fight that by trying to mount a display in.

Check the *other* direction first: a pod can almost always reach *outbound* to the host (`host.docker.internal`, the host LAN IP, the runtime gateway IP) even when the reverse — host reaching into the pod — is blocked. So don't have the pod perform the GUI action itself. Have its `ExecuteScript` POST a small payload (e.g. `{"url": "..."}`) to a tiny native listener (`http.server`) running directly on the host, and let *that* process own the actual GUI action.

Two things that make this reliable:
- **Verify the native action actually happened — don't trust exit code 0.** A backgrounded launch can hand off to an already-running instance via IPC and exit clean regardless of whether anything visible changed. Poll for the real end state (window title present, window geometry matches expected) instead.
- **Make the native listener durable via the OS scheduler**, not a bare process started by hand. On Windows: a Scheduled Task with an `AtLogOn` trigger plus a second trigger that re-fires every few minutes as a self-heal; run it with `pythonw.exe` (no console window to close by accident); always stop/start through the task, not a raw `Stop-Process` (which leaves the scheduler's state out of sync — `Ready` shown while the process is actually dead).
