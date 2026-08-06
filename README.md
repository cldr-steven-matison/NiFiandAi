# NiFiandAi — the `nifi-and-ai` Claude skill

A self-contained [Claude skill](https://docs.claude.com/en/docs/claude-code/skills) for building,
deploying, and debugging **Apache NiFi 2.x + MiNiFi (C++/Java) + EFM** data flows — programmatically
via the REST API, as custom Python/Java processors, or as edge agents on Kubernetes — including the
LLM/RAG inference patterns (Kafka fan-out, Whisper, embeddings, vector stores).

It's the sanitized, external-friendly distillation of hard-won lessons: the silent-drop failures
that cost a day each, the sensitive-property mask that overwrites credentials, laying out a flow so
it doesn't look like an API dumped it on the canvas, and the edge-side EFM machinery (staging agent
binaries, the deployer curl, the undocumented Flow Designer API).

## What's here

- **`SKILL.md`** — the playbook: the always-on rules, deployment shapes, and a map into the references.
- **`references/`** — load-on-demand deep dives:
  - `flow-api.md` — deploying/editing flows via the NiFi REST API.
  - `patterns.md` — flow patterns that ship (NiFi-as-HTTP-API, fire-and-forget router, the RAG shape).
  - `custom-processors.md` — writing custom Python/Java processors and rebuild→redeploy discipline.
  - `minifi-efm.md` — the edge side: agent binaries, EFM persistence, the deployer, the Designer API.
  - `debugging.md` — cross-cutting wire-up gotchas and a debugging checklist.
  - `layout.md` — canvas layout: the coordinate model, spacing constants, and per-shape placement.

## Install

Drop it into your Claude skills directory:

```bash
git clone https://github.com/cldr-steven-matison/NiFiandAi.git ~/.claude/skills/nifi-and-ai
```

Claude loads `SKILL.md` at session start and pulls the deeper `references/` material only when the
task needs it.

## Companion

See **[The Complete Guide to Edge Flow Management](https://github.com/cldr-steven-matison/EdgeFlowManager)** —
the full field guide this skill's edge/EFM machinery comes from.
