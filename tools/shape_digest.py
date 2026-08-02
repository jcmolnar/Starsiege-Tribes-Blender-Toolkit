"""shape_digest -- versioned COMPATIBILITY and SEMANTIC digests for a converted shape.

★Never hash the GLB file.★  Re-serialising a glTF -- reordering JSON keys, changing chunk
padding, moving a buffer view, updating the Blender version string in `asset.generator` --
produces different bytes for an identical shape.  A raw file hash would therefore reject
correct clients constantly and teach everyone to disable the check, which is worse than
having none.

Two digests, because they answer different questions and must not be conflated:

  COMPATIBILITY digest -- "will this client and this server agree about the wire?"
      Sequence indices and names, engine-resolved mounts, detail thresholds.  Two shapes
      with the same compatibility digest drive identically for every wire value even if
      their geometry differs completely.

  SEMANTIC digest -- "is this the same asset?"
      The canonical decoded hierarchy, quantised geometry, indices, materials, bounds and
      animation metadata.  ★A compatibility digest alone is NOT an integrity check★: a
      client can preserve every sequence name and mount while replacing the player mesh
      with a wireframe or a giant flat billboard.

SECURITY STATEMENT, which must travel with this feature wherever it is described:
    ★This is mismatch detection, not attestation.★  A deliberately modified executable
    computes and returns whatever it likes.  It reliably catches stale assets, accidental
    edits, partial downloads and ordinary modified content; it does NOT stop a determined
    cheat, and must never be described as anti-cheat.

Canonicalisation rules (all deliberate, all part of the digest input via SCHEMA):
  - source identity first: everything is keyed by dtsSourceObjectIndex / material index /
    sequence index, never by array position or by name;
  - floats quantised to a fixed number of decimals, so float32 storage noise cannot change
    a digest while a real edit still does;
  - triangles emitted in source face order;
  - no dependence on JSON key order, chunk padding, buffer-view placement, or any Blender
    metadata;
  - the schema version is hashed IN, so a rule change cannot silently collide with an old
    digest.
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dts_contract
import glb_contract_extras as gce
import engine_mount_contract as emc


SCHEMA_VERSION = 1

# Quantisation. 4 decimals on positions is ~0.1mm at Tribes scale -- far below anything a
# modeller would call a change, far above float32 round-off through the import/export
# chain.  Angles/normals get the same treatment.
POS_DECIMALS = 4
UV_DECIMALS = 4


def _q(v, nd):
    # Normalise -0.0 to 0.0 so a sign bit cannot change a digest.
    r = round(float(v), nd)
    return 0.0 if r == 0.0 else r


def _hash(obj):
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------

def compatibilityDigest(contract):
    """The wire contract only: what a server needs to know two clients agree about."""
    seqs = [{"i": s["index"], "name": s["name"], "cyclic": int(s["cyclic"]),
             "priority": int(s["priority"])}
            for s in contract.sequences]

    # Mount families, resolved through the ONE classifier rather than a local list.
    mounts = emc.mountsIn([n["name"] for n in contract.nodes])
    mountView = dict((base, sorted(e["name"] for e in entries))
                     for base, entries in mounts.items())

    body = {
        "schema": SCHEMA_VERSION,
        "kind": "compatibility",
        # Indices 0..15 are the wire contract for non-player shapes (shapebase.h:21-29),
        # so the first 16 are recorded positionally and the rest by name.
        "sequences": seqs[:16],
        "sequencesBeyond16": sorted(s["name"] for s in seqs[16:]),
        "mounts": mountView,
        "details": [_q(d["size"], POS_DECIMALS) for d in contract.details],
    }
    return _hash(body), body


def semanticDigest(contract):
    """The asset itself: hierarchy, geometry, materials, bounds, animation metadata."""
    nodes = [{"i": n["index"], "name": n["name"], "parent": n["parent"]}
             for n in contract.nodes]

    objects = [{"i": o["index"], "name": o["name"], "node": o["nodeIndex"],
                "mesh": o["meshIndex"],
                "cel": dict((seq, [[_q(k["pos"], 4), k["flags"], k["keyValue"],
                                    k["matIndex"]] for k in keys])
                            for seq, keys in sorted(o["celTracks"].items()))}
               for o in contract.objects]

    meshes = []
    for m in (contract.meshes or []):
        meshes.append({
            "i": m["index"],
            "v": [[_q(p[0], POS_DECIMALS), _q(p[1], POS_DECIMALS), _q(p[2], POS_DECIMALS)]
                  for p in m["vertices"]],
            # Source face order is the canonical order; no sorting, so a reordering that
            # changes rendering order is visible in the digest.
            "f": [[f["material"]] + list(f["vertices"]) for f in m["faces"]],
        })

    materials = [{"i": m["index"], "flags": m["flags"], "map": m["mapFile"],
                  "alpha": _q(m["alpha"], 4), "rgb": list(m["rgb"]),
                  "type": m["type"], "useDefault": m["useDefaultProps"]}
                 for m in contract.materials]

    sh = contract.shape
    body = {
        "schema": SCHEMA_VERSION,
        "kind": "semantic",
        "sourceVersion": contract.version,
        "nodes": nodes,
        "objects": objects,
        "meshes": meshes,
        "materials": materials,
        "details": [{"root": d["rootNodeIndex"], "size": _q(d["size"], POS_DECIMALS)}
                    for d in contract.details],
        "sequences": [{"i": s["index"], "name": s["name"], "cyclic": int(s["cyclic"]),
                       "priority": int(s["priority"]),
                       "duration": _q(s["duration"], 6),
                       "triggers": s["nFrameTriggers"], "ifl": s["nIFLSubSequences"]}
                      for s in contract.sequences],
        "frameTriggers": [{"pos": _q(t["position"], 6), "value": t["value"],
                           "forward": bool(t["forward"])}
                          for t in contract.frameTriggers],
        "transitions": [{"s": t["startSequence"], "e": t["endSequence"],
                         "sp": _q(t["startPosition"], 6), "ep": _q(t["endPosition"], 6),
                         "dur": _q(t["duration"], 6)}
                        for t in contract.transitions],
        "bounds": {"min": [_q(v, POS_DECIMALS) for v in (sh["boundsMin"] or [])],
                   "max": [_q(v, POS_DECIMALS) for v in (sh["boundsMax"] or [])],
                   "center": [_q(v, POS_DECIMALS) for v in sh["center"]],
                   "radius": _q(sh["radius"], POS_DECIMALS)},
    }
    return _hash(body), body


def digestsForSource(path):
    c = dts_contract.load(path)
    comp, _cb = compatibilityDigest(c)
    sem, _sb = semanticDigest(c)
    return {"schema": SCHEMA_VERSION, "compatibility": comp, "semantic": sem}


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: shape_digest.py <shape.dts> [--body]\n")
        return 2
    try:
        c = dts_contract.load(argv[1])
    except dts_contract.ContractError as e:
        sys.stderr.write("cannot read source: %s\n" % e)
        return 2

    comp, cb = compatibilityDigest(c)
    sem, sb = semanticDigest(c)
    if "--body" in argv:
        print(json.dumps({"compatibility": cb, "semantic": sb},
                         indent=2, sort_keys=True))
        return 0
    print("%s" % os.path.basename(argv[1]))
    print("  schema        %d" % SCHEMA_VERSION)
    print("  compatibility %s" % comp)
    print("  semantic      %s" % sem)
    print("  NOTE: mismatch detection, NOT attestation -- a modified executable can lie.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
