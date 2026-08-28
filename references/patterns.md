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

### Consolidated router — ONE HandleHttpRequest + a path-driven InvokeHTTP for every route

**When a single agent fronts several local upstreams (an edge-AI box with N inference services on N ports), do not build one `HandleHttpRequest → InvokeHTTP → HandleHttpResponse` triple per door.** That verbose per-leg shape (N listeners on N ports, ~3N processors) is what a fleet review flags — the simpler, canonical shape is **one listener + one dynamic InvokeHTTP keyed on the request path + one responder**, because the request path already tells you which upstream to call:

```
HandleHttpRequest (:PORT, Allowed Paths = /(reason|embed|rerank|transcribe))   ← one listener, all routes
  → UpdateAttribute   (derive target.url from ${http.request.uri})
  → InvokeHTTP        (HTTP URL = ${target.url})                                ← one dynamic caller
  → HandleHttpResponse                                                          ← one responder
```

One `StandardHttpContextMap` still pairs the single request/response by the per-request `http.context.identifier`, so concurrent callers on different paths never cross — the map, not the port, is what pairs a response to its request (that's why one pair serves every route).

**Two realizations of the path→target map, by how much the upstreams differ:**

- **Same host+port, path passes straight through** — no map needed, just suffix the path: `HTTP URL = http://localhost:<port>${http.request.uri}`. One `HandleHttpRequest` (all paths) can feed several endpoints on one upstream server through a single `InvokeHTTP` this way — the request path *is* the upstream path.
- **Doors differ by port and/or upstream path** — set `target.url` in one `UpdateAttribute` with a nested-`ifElse` case switch on the path, then `InvokeHTTP ${target.url}`:
  ```
  target.url = ${http.request.uri:equals('/reason'):ifElse('http://127.0.0.1:8000/v1/chat/completions',
               ${http.request.uri:equals('/embed'):ifElse('http://127.0.0.1:8001/embed',
               ${http.request.uri:equals('/rerank'):ifElse('http://127.0.0.1:8002/rerank',
               'http://127.0.0.1:8003/inference')})})}
  ```
  A four-door router (e.g. `/reason /embed /rerank /transcribe` → four inference services on different loopback ports) collapses four separate listener legs (~12 processors) to one listener + one caller + one responder this way.

**Per-route request Content-Type is set on the flowfile, not hardcoded on InvokeHTTP.** With one shared `InvokeHTTP`, leave its `Request Content-Type = ${mime.type}` and set `mime.type` on each branch (JSON routes → `application/json`; the multipart route → `multipart/form-data; boundary=…` in its reconstruction leg, below). Do **not** leave it as the literal `${Content-Type}` — no such attribute exists by default, it resolves empty, and a JSON upstream answers `415` (see the gotcha list below).

**A route that needs a different request shape gets a sub-branch before the shared InvokeHTTP, not its own leg.** `/transcribe` takes a multipart upload that whisper needs reassembled — route it (a `RouteOnAttribute` on `${http.request.uri:equals('/transcribe')}`) through the multipart-reconstruction sub-leg below, which rejoins the shared `InvokeHTTP` once `target.url` and `mime.type` are set. One extra branch, still one listener and one responder.

### Multipart reconstruction for a whisper `/inference` upload

`HandleHttpRequest` **splits** an inbound `multipart/form-data` upload into one flowfile per part (headers exposed as `http.multipart.*` attributes); a whisper.cpp `/inference` endpoint wants the original multipart body back. Reassemble it:

```
HandleHttpRequest (multipart in)
  → UpdateAttribute   fragment.identifier=${http.context.identifier},
                      fragment.index=${http.multipart.fragments.sequence.number:minus(1)},
                      fragment.count=${http.multipart.fragments.total.number}
  → RouteOnAttribute  hasType = ${'http.multipart.content.type':isEmpty():not()}
       hasType   → ReplaceText (Prepend a part header WITH Content-Type)
       unmatched → ReplaceText (Prepend a part header, no Content-Type)
  → MergeContent      Merge Strategy=Defragment, Merge Format=Binary Concatenation,
                      Footer = --<BOUNDARY>--
  → UpdateAttribute   mime.type = multipart/form-data; boundary=<BOUNDARY>
  → InvokeHTTP        (the shared dynamic caller)
```

The `<BOUNDARY>` string in the two `ReplaceText` part-headers, the `MergeContent` footer, and the `mime.type` must be byte-identical. This is the sub-branch a `/transcribe`-style route hangs off the shared `InvokeHTTP`.

**Verify the upstream's real route before pointing `InvokeHTTP` at it — don't assume the vendor's documented path.** whisper.cpp's server serves **`/inference` only**; the OpenAI-style `/v1/audio/transcriptions` route 404s on it. Curl the upstream directly (`curl -sS localhost:<port>/<path>`) and read the status before wiring the flow, rather than debugging a 404 as if it were a flow bug.

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
