#!/usr/bin/env python3
"""Layout checker for NiFi 2.x flows — run it on a flow JSON before you upload it, and on a
sub-agent's flow before you accept it. The rules are references/layout.md; this is them as code.

Reads any of:
  * a flow-definition export / `GET /process-groups/{id}/download`   (top-level `flowContents`)
  * a `flow.json` / `flow.json.gz`                                      (top-level `rootGroup`)
  * a `GET /flow/process-groups/{id}` response                          (top-level `processGroupFlow`, one level)
  * a bare VersionedProcessGroup                                        (top-level `processors` + `connections`)

Usage:
  check_layout.py FLOW.json [FLOW2.json ...] [--group NAME] [--profile dev|prod]
                  [--allow-upward "Src->Dst" ...] [--metrics] [--stamp DIR]

Exit status 1 when any FAIL is found, else 0. `--stamp DIR` writes DIR/<sha256 of the file> for every
file that passed, so a pre-upload hook can tell a checked file from an unchecked or edited one.

Box sizes are the NiFi 2.x frontend's own (rel/nifi-2.12.0, the *-manager.service.ts dimensions).
"""
import argparse
import gzip
import hashlib
import json
import math
import os
import sys
import time

SIZE = {                      # width, height as the 2.x canvas renders them
    "PROCESSOR": (352, 128),
    "INPUT_PORT": (240, 48), "OUTPUT_PORT": (240, 48),
    "FUNNEL": (48, 48),
    "PROCESS_GROUP": (384, 176), "REMOTE_PROCESS_GROUP": (384, 176),
}
MIN_GAP = 240                 # a connection label is 240 wide; the 600 branch pitch leaves 248 beside a processor
FAR = 1250                    # a port / PG / RPG further than this from what it connects to is misplaced
                              # (a "max-x + 900" placement is >= 1284 centre-to-centre, so it always trips)
SINK_SPAN = 600               # a sink fed from more than 3 rows (600 px) away is the shared-sink smell
MASTER_LABEL_MIN_PROCS = 6
FAILURE_RELS = {"failure", "no retry", "unmatched", "nonzero status", "invalid", "timeout"}
SINK_TYPES = ("LogAttribute", "LogMessage")


# ── loading ──────────────────────────────────────────────────────────────────
def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    if "flowContents" in doc:
        return list(walk_versioned(doc["flowContents"]))
    if "rootGroup" in doc:
        return list(walk_versioned(doc["rootGroup"]))
    if "processGroupFlow" in doc:
        return [from_entity(doc["processGroupFlow"])]
    if "processors" in doc and "connections" in doc:
        return list(walk_versioned(doc))
    raise SystemExit(f"{path}: not a flow definition, flow.json or process-group flow response")


def _comp(kind, cid, name, pos, **kw):
    w, h = kw.pop("w", None), kw.pop("h", None)          # only labels carry their own size
    if kind in SIZE:
        w, h = SIZE[kind]
    return dict(kind=kind, id=cid, name=name or cid[:8], x=float(pos["x"]), y=float(pos["y"]),
                w=float(w), h=float(h), **kw)


def walk_versioned(pg, path=""):
    me = pg.get("identifier") or pg.get("instanceIdentifier") or "root"
    name = f"{path}/{pg.get('name', 'root')}" if path else pg.get("name", "root")
    comps = {}
    for p in pg.get("processors", []):
        comps[p["identifier"]] = _comp("PROCESSOR", p["identifier"], p.get("name"), p["position"],
                                       type=p.get("type", "").rsplit(".", 1)[-1],
                                       auto=set(p.get("autoTerminatedRelationships") or []))
    for k, kind in (("inputPorts", "INPUT_PORT"), ("outputPorts", "OUTPUT_PORT"), ("funnels", "FUNNEL"),
                    ("processGroups", "PROCESS_GROUP"), ("remoteProcessGroups", "REMOTE_PROCESS_GROUP")):
        for c in pg.get(k, []):
            comps[c["identifier"]] = _comp(kind, c["identifier"], c.get("name"), c["position"])
    for lb in pg.get("labels", []):
        comps[lb["identifier"]] = _comp("LABEL", lb["identifier"], (lb.get("label") or "")[:40], lb["position"],
                                        w=lb.get("width") or 148, h=lb.get("height") or 148,
                                        text=lb.get("label") or "")
    conns = []
    for c in pg.get("connections", []):
        conns.append(dict(src=_end(c["source"], me), dst=_end(c["destination"], me),
                          rels=list(c.get("selectedRelationships") or []), bends=c.get("bends") or [],
                          sname=c["source"].get("name", ""), dname=c["destination"].get("name", "")))
    yield dict(name=name, comps=comps, conns=conns)
    for child in pg.get("processGroups", []):
        yield from walk_versioned(child, name)


def _end(e, me):
    """NiFi's own rule: a connection to a component inside a child PG / RPG ends at that group."""
    gid = e.get("groupId")
    return gid if gid and gid != me else e["id"]


def from_entity(pgf):
    me, flow = pgf.get("id", "root"), pgf["flow"]
    comps = {}
    for p in flow.get("processors", []):
        c = p["component"]
        comps[p["id"]] = _comp("PROCESSOR", p["id"], c.get("name"), p["position"],
                               type=c.get("type", "").rsplit(".", 1)[-1],
                               auto=set((c.get("config") or {}).get("autoTerminatedRelationships") or []))
    for k, kind in (("inputPorts", "INPUT_PORT"), ("outputPorts", "OUTPUT_PORT"), ("funnels", "FUNNEL"),
                    ("processGroups", "PROCESS_GROUP"), ("remoteProcessGroups", "REMOTE_PROCESS_GROUP")):
        for e in flow.get(k, []):
            comps[e["id"]] = _comp(kind, e["id"], e.get("component", {}).get("name"), e["position"])
    for e in flow.get("labels", []):
        c = e.get("component", {})
        comps[e["id"]] = _comp("LABEL", e["id"], (c.get("label") or "")[:40], e["position"],
                               w=c.get("width") or 148, h=c.get("height") or 148, text=c.get("label") or "")
    conns = []
    for e in flow.get("connections", []):
        c = e["component"]
        conns.append(dict(src=_end(c["source"], me), dst=_end(c["destination"], me),
                          rels=list(c.get("selectedRelationships") or []), bends=c.get("bends") or [],
                          sname=c["source"].get("name", ""), dname=c["destination"].get("name", "")))
    return dict(name=pgf.get("breadcrumb", {}).get("breadcrumb", {}).get("name", me), comps=comps, conns=conns)


# ── geometry ─────────────────────────────────────────────────────────────────
def centre(c):
    return c["x"] + c["w"] / 2, c["y"] + c["h"] / 2


def overlaps(a, b):
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def contains(outer, inner):
    return (outer["x"] <= inner["x"] and outer["y"] <= inner["y"] and
            inner["x"] + inner["w"] <= outer["x"] + outer["w"] and inner["y"] + inner["h"] <= outer["y"] + outer["h"])


def seg_hits_box(p, q, b, pad=4):
    """Liang-Barsky: does segment p->q pass through box b (shrunk by pad)?"""
    x0, y0, x1, y1 = b["x"] + pad, b["y"] + pad, b["x"] + b["w"] - pad, b["y"] + b["h"] - pad
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pp == 0:
            if qq < 0:
                return False
        else:
            t = qq / pp
            if pp < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def segs_cross(a, b, c, d):
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return (v > 0) - (v < 0)
    if a in (c, d) or b in (c, d):
        return False
    return orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0


def path(conn, comps):
    pts = [centre(comps[conn["src"]])] + [(b["x"], b["y"]) for b in conn["bends"]] + [centre(comps[conn["dst"]])]
    return pts


# ── checks ───────────────────────────────────────────────────────────────────
def check_pg(pg, args):
    comps, conns, out = pg["comps"], [c for c in pg["conns"]], []
    conns = [c for c in conns if c["src"] in comps and c["dst"] in comps]
    boxes = [c for c in comps.values() if c["kind"] != "LABEL"]
    labels = [c for c in comps.values() if c["kind"] == "LABEL"]
    procs = [c for c in boxes if c["kind"] == "PROCESSOR"]
    say = lambda lvl, msg: out.append((lvl, msg))
    fmt = lambda c: f"{c['name']} ({c['x']:.0f},{c['y']:.0f})"

    # 1. boxes overlap / stack; wired neighbours on one row too close for a connection label between them.
    # "Wired" = connected to each other, or siblings fed by one source (a fan-out row). Unwired neighbours
    # (a column of independent PGs on a root canvas) may sit closer - hand-tuned root canvases do.
    wired = set()
    fanout = {}
    for c in conns:
        if c["src"] != c["dst"]:
            wired.add(frozenset((c["src"], c["dst"])))
            fanout.setdefault(c["src"], set()).add(c["dst"])
    for dsts in fanout.values():
        wired.update(frozenset((a, b)) for a in dsts for b in dsts if a != b)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if (a["x"], a["y"]) == (b["x"], b["y"]):
                say("FAIL", f"stacked at one position: {fmt(a)} and {b['name']} - read the canvas extents and place it")
            elif overlaps(a, b):
                say("FAIL", f"boxes overlap: {fmt(a)} and {fmt(b)}")
            elif frozenset((a["id"], b["id"])) in wired and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]:
                left, right = (a, b) if a["x"] <= b["x"] else (b, a)
                gap = right["x"] - (left["x"] + left["w"])
                if 0 <= gap < MIN_GAP:
                    say("FAIL", f"wired same-row neighbours {gap:.0f}px apart (< {MIN_GAP}, no room for a connection label; processors want the 600 branch "
                                f"pitch, PGs ~664): {fmt(left)} and {fmt(right)}")

    # 2. labels: never partly on a box (a lane label may contain whole boxes); a master label on top
    for lb in labels:
        for b in boxes:
            if overlaps(lb, b) and not contains(lb, b):
                say("FAIL", f"label {lb['name']!r} at ({lb['x']:.0f},{lb['y']:.0f}) partly covers {fmt(b)}")
    if len(procs) >= MASTER_LABEL_MIN_PROCS and boxes:
        top = min(b["y"] for b in boxes)
        if not any(lb["y"] + lb["h"] <= top + 1 for lb in labels):
            say("WARN", f"no master label above the flow ({len(procs)} processors): add one label at the top "
                        f"that summarises the whole PG")

    # 3. NiFi's overlapping-connections banner (2.9+): >=2 bend-less connections between one pair
    pairs = {}
    for c in conns:
        if not c["bends"]:
            pairs.setdefault(tuple(sorted((c["src"], c["dst"]))), []).append(c)
    for key, group in pairs.items():
        if len(group) > 1:
            a, b = comps[key[0]], comps[key[1]]
            rels = " | ".join(",".join(c["rels"]) or "-" for c in group)
            say("FAIL", f"{len(group)} bend-less connections between {a['name']} and {b['name']} ({rels}) draw as one "
                        f"line and raise NiFi's overlap warning: merge them into one connection carrying every "
                        f"relationship, or give all but one a bend")

    # 4. direction inside a chain: processor/funnel -> processor/funnel never points up
    allowed = set(args.allow_upward or [])
    for c in conns:
        s, d = comps[c["src"]], comps[c["dst"]]
        if c["src"] == c["dst"] or c["bends"] or s["kind"] not in ("PROCESSOR", "FUNNEL") \
                or d["kind"] not in ("PROCESSOR", "FUNNEL"):
            continue
        if d["y"] < s["y"] and f"{s['name']}->{d['name']}" not in allowed:
            say("FAIL", f"routes upward: {fmt(s)} -> {fmt(d)}  (move the destination down, or --allow-upward "
                        f"\"{s['name']}->{d['name']}\" for a deliberate loop)")

    # 5. distance: ports, PGs and RPGs sit next to what they connect to
    for c in conns:
        s, d = comps[c["src"]], comps[c["dst"]]
        if c["src"] == c["dst"]:
            continue
        (sx, sy), (dx, dy) = centre(s), centre(d)
        dist = math.hypot(dx - sx, dy - sy)
        groups = {"PROCESS_GROUP", "REMOTE_PROCESS_GROUP"}
        ports = {"INPUT_PORT", "OUTPUT_PORT"}
        if (s["kind"] in groups or d["kind"] in groups) and dist > FAR:
            say("FAIL", f"{fmt(s)} -> {fmt(d)} is {dist:.0f}px long (> {FAR}): place the port/group next to what it "
                        f"connects to (a rewire moves it too)")
        elif (s["kind"] in ports or d["kind"] in ports) and dist > FAR:
            # inside a PG one port may collect from several legs, so it cannot sit beside all of them
            say("WARN", f"{fmt(s)} -> {fmt(d)} is {dist:.0f}px long (> {FAR}): move the port toward the legs it serves")
        elif d["kind"] == "PROCESSOR" and d.get("type") in SINK_TYPES and abs(dy - sy) > SINK_SPAN:
            say("WARN", f"sink {d['name']} is fed from {abs(dy - sy):.0f}px away ({s['name']}): give that stage its "
                        f"own sink on its row")

    # 6. straight lines through a third box
    for c in conns:
        if c["src"] == c["dst"]:
            continue
        pts = path(c, comps)
        for b in boxes:
            if b["id"] in (c["src"], c["dst"]):
                continue
            if any(seg_hits_box(p, q, b) for p, q in zip(pts, pts[1:])):
                say("WARN", f"{c['sname'] or comps[c['src']]['name']} -> {c['dname'] or comps[c['dst']]['name']} "
                            f"passes through {fmt(b)}")
                break

    # 7. relationships: Retry never terminated; dev routes failures to a stage sink; prod lists the sinks
    for p in procs:
        if "Retry" in p.get("auto", ()):
            say("FAIL", f"{p['name']} auto-terminates Retry - self-loop it with a bounded FlowFile Expiration")
        if args.profile == "dev":
            dropped = sorted(r for r in p.get("auto", ()) if r.lower() in FAILURE_RELS)
            if dropped and p.get("type") not in SINK_TYPES:
                say("WARN", f"dev profile: {p['name']} auto-terminates {dropped} - route to a stage sink beside it")
    if args.profile == "prod":
        sinks = [p["name"] for p in procs if p.get("type") in SINK_TYPES]
        if sinks:
            say("INFO", f"prod pass: {len(sinks)} log sinks to keep (key failures / notify) or auto-terminate: "
                        + ", ".join(sinks))
    return out


def metrics(pg):
    comps, conns = pg["comps"], [c for c in pg["conns"] if c["src"] in pg["comps"] and c["dst"] in pg["comps"]]
    boxes = [c for c in comps.values() if c["kind"] != "LABEL"]
    if not boxes:
        return None
    segs, lens = [], []
    for c in conns:
        if c["src"] == c["dst"]:
            continue
        pts = path(c, comps)
        lens.append(sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pts, pts[1:])))
        segs.extend((p, q, id(c)) for p, q in zip(pts, pts[1:]))
    crossings = sum(1 for i, (a, b, ca) in enumerate(segs) for (c, d, cb) in segs[i + 1:]
                    if ca != cb and segs_cross(a, b, c, d))
    xs = [b["x"] for b in boxes] + [b["x"] + b["w"] for b in boxes]
    ys = [b["y"] for b in boxes] + [b["y"] + b["h"] for b in boxes]
    return dict(components=len(boxes), connections=len(conns),
                mean_len=round(sum(lens) / len(lens)) if lens else 0, max_len=round(max(lens)) if lens else 0,
                crossings=crossings, width=round(max(xs) - min(xs)), height=round(max(ys) - min(ys)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--group", help="check only process groups with this name")
    ap.add_argument("--profile", choices=("dev", "prod"), help="dev: failures go to stage sinks; prod: list sinks")
    ap.add_argument("--allow-upward", action="append", metavar="SRC->DST", help="a deliberate upward loop")
    ap.add_argument("--metrics", action="store_true", help="print per-PG length/crossing/extent numbers")
    ap.add_argument("--stamp", metavar="DIR", help="on a pass, write DIR/<sha256 of the file>")
    args = ap.parse_args()

    failed_any = False
    for path_ in args.files:
        fails = 0
        for pg in load(path_):
            if args.group and pg["name"].rsplit("/", 1)[-1] != args.group:
                continue
            findings = check_pg(pg, args)
            fails += sum(1 for lvl, _ in findings if lvl == "FAIL")
            for lvl, msg in findings:
                print(f"{lvl} [{pg['name']}] {msg}")
            if args.metrics:
                m = metrics(pg)
                if m:
                    print(f"METRICS [{pg['name']}] " + " ".join(f"{k}={v}" for k, v in m.items()))
        status = "FAIL" if fails else "PASS"
        print(f"{status} {path_}" + (f" ({fails} failures)" if fails else ""))
        if fails:
            failed_any = True
        elif args.stamp:
            os.makedirs(args.stamp, exist_ok=True)
            digest = hashlib.sha256(open(path_, "rb").read()).hexdigest()
            with open(os.path.join(args.stamp, digest), "w") as fh:
                fh.write(f"{os.path.abspath(path_)}\t{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    sys.exit(1 if failed_any else 0)


if __name__ == "__main__":
    main()
