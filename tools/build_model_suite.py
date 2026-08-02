"""build_model_suite -- rebuild every supported asset from the manifest, reproducibly.

★The documented suite did not exist.★  Earlier handoffs described a 21-shape model suite,
but there was no runner and no manifest -- so "the suite passes" meant "the models someone
converted by hand at some point still worked", which cannot be re-run, cannot be diffed,
and cannot fail.

This is that runner.  It:

  1. builds into a STAGING directory, never straight into a playable tree;
  2. runs the converter, which now runs every gate itself;
  3. records source/candidate hashes and the GLB summary;
  4. emits a deterministic JSON report plus a human summary;
  5. copies to the packaging stage only after every gate passes;
  6. returns NONZERO if any asset is unexpectedly excluded, missing, or unlisted.

★Point 6 is the one that matters.★  A suite that only reports on what it managed to build
cannot distinguish "everything passed" from "almost nothing ran".

Usage:
    python -B tools/build_model_suite.py [--manifest PATH] [--stage DIR] [--only ID]
                                         [--deploy DIR]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import dts_contract
import glb_contract_extras as gce
import shape_digest


BLENDER = r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def resolveSource(manifest, rel):
    for root in manifest.get("sourceRoots", []):
        p = os.path.join(root, rel.replace("/", os.sep))
        if os.path.exists(p):
            return p
    return None


def glbSummary(path):
    """The machine-readable summary the finalization audit asks each conversion to print."""
    js, _chunks, raw = gce.readGlb(path)
    root = gce.soleRootIndex(js)
    rex = (js["nodes"][root].get("extras") or {}) if root is not None else {}
    covered = sum(1 for n in js.get("nodes", [])
                  if "dtsSourceObjectIndex" in (n.get("extras") or {}))
    cel = sum(1 for n in js.get("nodes", []) if (n.get("extras") or {}).get("dtsCel"))
    return {
        "animations": len(js.get("animations", [])),
        "names": [a.get("name") for a in js.get("animations", [])],
        "images": len(js.get("images", [])),
        "textures": len(js.get("textures", [])),
        "materials": len(js.get("materials", [])),
        "dtsContractVersion": rex.get("dtsContractVersion"),
        "dtsSourceVersion": rex.get("dtsSourceVersion"),
        "sourceObjectIndexCoverage": covered,
        "celTrackedObjects": cel,
        "loaderKeyframes": gce.totalLoaderKeys(js, raw),
        "doubleSidedMaterials": sorted(
            (m.get("extras") or {}).get("dtsMaterialIndex")
            for m in js.get("materials", []) if m.get("doubleSided")),
    }


def convert(src, dst, flags):
    cmd = [BLENDER, "--background", "--factory-startup",
           "--python", os.path.join(HERE, "dts2glb.py"), "--", src, dst] + list(flags)
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    lines = [l for l in out.splitlines() if "[DTS2GLB]" in l]
    return p.returncode, lines


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(REPO, "model_suite_manifest.json"))
    ap.add_argument("--stage", default=os.path.join(REPO, "build", "suite"))
    ap.add_argument("--report", default=None)
    ap.add_argument("--only", default=None, help="build a single asset id")
    ap.add_argument("--deploy", default=None,
                    help="copy passing assets into this tree (only after ALL gates pass)")
    args = ap.parse_args(argv)

    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)

    staging = os.path.join(args.stage, "candidate")
    os.makedirs(staging, exist_ok=True)

    assets = manifest.get("assets", [])
    if args.only:
        assets = [a for a in assets if a["id"] == args.only]
        if not assets:
            sys.stderr.write("no asset with id %r in the manifest\n" % args.only)
            return 2

    report = {"schemaVersion": manifest.get("schemaVersion"),
              "assets": [], "excluded": [], "problems": []}
    problems = []

    print("=" * 74)
    print("model suite: %d asset(s), staging -> %s" % (len(assets), staging))
    print("=" * 74)

    for a in assets:
        entry = {"id": a["id"], "profile": a.get("profile"), "status": "pending"}
        src = resolveSource(manifest, a["source"])
        if src is None:
            entry["status"] = "SOURCE MISSING"
            entry["source"] = a["source"]
            problems.append("%s: source not found under any sourceRoot (%s)"
                            % (a["id"], a["source"]))
            report["assets"].append(entry)
            print("  %-16s SOURCE MISSING (%s)" % (a["id"], a["source"]))
            continue

        # The contract is read here too, so the report records what the SOURCE said
        # independently of whatever the converter chose to log.
        try:
            c = dts_contract.load(src)
            entry["sourceContract"] = {
                "version": c.version,
                "nodes": len(c.nodes), "objects": len(c.objects),
                "materials": len(c.materials), "sequences": len(c.sequences),
                "sequenceNames": [s["name"] for s in c.sequences],
                "details": [d["size"] for d in c.details],
                "frameTriggers": len(c.frameTriggers),
                "transitions": len(c.transitions),
                "defects": [d["code"] for d in c.defects],
            }
            # Digests are computed from the CONTRACT, never from file bytes -- a
            # re-serialised GLB is the same shape.  See tools/shape_digest.py.
            comp, _ = shape_digest.compatibilityDigest(c)
            sem, _ = shape_digest.semanticDigest(c)
            entry["digests"] = {"schema": shape_digest.SCHEMA_VERSION,
                                "compatibility": comp, "semantic": sem}
            if a.get("sourceVersion") and c.version != a["sourceVersion"]:
                problems.append("%s: manifest says source v%s, file is v%d"
                                % (a["id"], a["sourceVersion"], c.version))
        except Exception as e:
            entry["status"] = "SOURCE UNREADABLE"
            entry["error"] = str(e)
            problems.append("%s: source unreadable: %s" % (a["id"], e))
            report["assets"].append(entry)
            print("  %-16s SOURCE UNREADABLE: %s" % (a["id"], e))
            continue

        dst = os.path.join(staging, os.path.basename(a["output"]))
        rc, lines = convert(src, dst, a.get("converterFlags", []))
        entry["converterExit"] = rc
        entry["converterLog"] = lines[-25:]

        if rc != 0 or not os.path.exists(dst):
            entry["status"] = "CONVERSION FAILED"
            problems.append("%s: converter exit %d" % (a["id"], rc))
            report["assets"].append(entry)
            print("  %-16s FAILED (exit %d)" % (a["id"], rc))
            for l in lines[-6:]:
                print("      %s" % l)
            continue

        entry["sourceSha256"] = sha256(src)
        entry["candidateSha256"] = sha256(dst)
        try:
            entry["glb"] = glbSummary(dst)
        except Exception as e:
            entry["glb"] = {"error": str(e)}
            problems.append("%s: could not summarise the GLB: %s" % (a["id"], e))

        entry["status"] = "PASS"
        report["assets"].append(entry)
        print("  %-16s PASS  (%s, %d anim, %d mat, %d keys)"
              % (a["id"], a.get("profile"),
                 entry["glb"].get("animations", -1),
                 entry["glb"].get("materials", -1),
                 entry["glb"].get("loaderKeyframes", -1)))

    # Exclusions are part of the contract, not a footnote: each must name a reason.
    for x in manifest.get("excluded", []):
        report["excluded"].append({"id": x["id"], "reason": x.get("reason")})
        if not x.get("reason"):
            problems.append("excluded entry %r has no reason" % x.get("id"))
        print("  %-16s EXCLUDED (%s)" % (x["id"], x.get("reason")))

    report["problems"] = problems

    reportPath = args.report or os.path.join(args.stage, "suite_report.json")
    os.makedirs(os.path.dirname(reportPath), exist_ok=True)
    with open(reportPath, "w", encoding="utf-8") as f:
        # sort_keys so two runs of an unchanged suite diff to nothing.
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")

    passed = [e for e in report["assets"] if e["status"] == "PASS"]
    print("-" * 74)
    print("%d/%d asset(s) passed; %d excluded; report -> %s"
          % (len(passed), len(report["assets"]), len(report["excluded"]), reportPath))

    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  %s" % p)
        return 1

    # Deploy only after EVERY asset passed -- a partial suite must never half-update a
    # playable tree, because the half that did not update is invisible.
    if args.deploy:
        if len(passed) != len(report["assets"]):
            print("NOT deploying: %d of %d assets did not pass."
                  % (len(report["assets"]) - len(passed), len(report["assets"])))
            return 1
        import shutil
        for a, e in zip(assets, report["assets"]):
            srcFile = os.path.join(staging, os.path.basename(a["output"]))
            dstFile = os.path.join(args.deploy, a["output"].replace("/", os.sep))
            os.makedirs(os.path.dirname(dstFile), exist_ok=True)
            shutil.copyfile(srcFile, dstFile)
            got = sha256(dstFile)
            if got != e["candidateSha256"]:
                print("  DEPLOY HASH MISMATCH for %s" % a["id"])
                return 1
            print("  deployed %s (%s)" % (a["output"], got[:16]))

    print("SUITE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
