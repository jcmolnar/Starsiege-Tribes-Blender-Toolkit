"""check_roundtrip_contract -- is this GLB an EXACT round trip of that DTS?

★This is a FIDELITY gate, not a compatibility gate, and the difference matters.★
`check_sequence_contract.py --compat` asks "will the engine drive this shape?" -- names
resolve, mounts exist, details cover.  A shape can pass that while having lost its
surface types, its exact bounds, its translucent flag and its frame triggers.  This tool
asks the stricter question: does the converted file say EXACTLY what the source said?

Passing compatibility is not evidence of fidelity.  Run both.

Output: one line per mismatch, each naming a path into the contract, e.g.

    materials[1].flags: source 0x2403, converted 0x0403
    sequences[2].duration: source 0.26666668, converted 0.30000001
    objects[12].cel.fire.keys[1].visible: source 0, converted missing

Exit codes (as specified in the finalization audit):
    0  exact pass
    1  fidelity mismatch
    2  source or GLB could not be parsed
    3  unsupported source feature
"""

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dts_contract
import glb_contract_extras as gce


# Comparison tolerance.  Values cross the boundary through repr() -- the shortest string
# that reads back to the same double -- so an exact conversion is EXACTLY equal.  The
# epsilon exists only to absorb Blender's float32 storage of transform-derived values,
# never to excuse a metadata difference.
EPS = 1e-6


def _binOffset(raw):
    off = 12
    while off + 8 <= len(raw):
        clen, ctype = struct.unpack_from("<I4s", raw, off)
        off += 8
        if ctype[0:3] == b"BIN":
            return off
        off += clen
    return None


def _triCount(js, meshIndex):
    """Faces the LOADER will build, which is not the same as glTF triangles.

    ★A doubleSided material yields TWO DTS faces per glTF triangle.★  This engine culls by
    winding per face (ts_CelAnimMesh.cpp:88-96) and has no two-sided material, so a .dts
    stores a two-sided card as the same triangle twice, reversed -- chaingun's muzzle
    flashes are 8 faces that are 4 mirrored pairs.  Blender merges each pair on import and
    re-exports one triangle plus `doubleSided`, and ts_gltf now expands it again.

    Counting raw glTF triangles here would report "source 8, converted 4" on a perfectly
    correct conversion.
    """
    n = 0
    mats = js.get("materials", [])
    for prim in js["meshes"][meshIndex].get("primitives", []):
        if prim.get("mode", 4) != 4:            # 4 == TRIANGLES
            continue
        if "indices" in prim:
            tris = js["accessors"][prim["indices"]]["count"] // 3
        else:
            tris = js["accessors"][prim["attributes"]["POSITION"]]["count"] // 3
        mi = prim.get("material")
        if mi is not None and 0 <= mi < len(mats) and mats[mi].get("doubleSided"):
            tris *= 2
        n += tris
    return n


def _glbPositionsByNode(js, raw):
    """{meshIndex: [(x,y,z), ...]} decoded from the BIN chunk.

    Reads the accessor payload for real rather than trusting its `min`/`max` -- the whole
    point of this comparison is to catch a payload that disagrees with its metadata.
    """
    binOff = _binOffset(raw)
    if binOff is None:
        raise ValueError("no BIN chunk")

    out = {}
    for mi, mesh in enumerate(js.get("meshes", [])):
        pts = []
        for prim in mesh.get("primitives", []):
            ai = prim.get("attributes", {}).get("POSITION")
            if ai is None:
                continue
            a = js["accessors"][ai]
            if a.get("componentType") != 5126 or a.get("type") != "VEC3":
                raise ValueError("POSITION accessor %d is not float32 VEC3" % ai)
            bv = js["bufferViews"][a["bufferView"]]
            base = binOff + bv.get("byteOffset", 0) + a.get("byteOffset", 0)
            stride = bv.get("byteStride", 12)
            for i in range(a["count"]):
                pts.append(struct.unpack_from("<3f", raw, base + i * stride))
        out[mi] = pts
    return out


class Mismatch(object):
    def __init__(self, path, src, got):
        self.path, self.src, self.got = path, src, got

    def __str__(self):
        return "%s: source %s, converted %s" % (self.path, self.src, self.got)


class Checker(object):
    def __init__(self, contract, js, raw=None):
        self.c = contract
        self.js = js
        self.glbRaw = raw
        self.bad = []

    def note(self, path, src, got):
        self.bad.append(Mismatch(path, src, got))

    def eqf(self, path, src, got):
        if got is None:
            return self.note(path, repr(src), "missing")
        try:
            g = float(got)
        except (TypeError, ValueError):
            return self.note(path, repr(src), repr(got))
        if abs(g - float(src)) > EPS:
            self.note(path, repr(float(src)), repr(g))

    def eqi(self, path, src, got):
        if got is None:
            return self.note(path, str(src), "missing")
        if int(got) != int(src):
            self.note(path, str(src), str(got))

    def eqs(self, path, src, got):
        if got is None:
            return self.note(path, repr(src), "missing")
        if str(got) != str(src):
            self.note(path, repr(src), repr(got))

    # -- root -------------------------------------------------------------

    def checkRoot(self):
        root = gce.soleRootIndex(self.js)
        if root is None:
            self.note("scene.roots", "exactly 1 (contract requires it)",
                      "%d" % len(self.js.get("scenes", [{}])[0].get("nodes", [])))
            return
        ex = (self.js["nodes"][root].get("extras") or {})
        if "dtsContractVersion" not in ex:
            self.note("root.dtsContractVersion", str(gce.CONTRACT_VERSION),
                      "missing -- this GLB carries no DTS contract at all")
            return
        self.eqi("root.dtsContractVersion", gce.CONTRACT_VERSION,
                 ex.get("dtsContractVersion"))
        self.eqi("root.dtsSourceVersion", self.c.version, ex.get("dtsSourceVersion"))
        self.eqf("root.dtsRadius", self.c.shape["radius"], ex.get("dtsRadius"))

        self._vec3("root.dtsCenter", self.c.shape["center"], ex.get("dtsCenter"))
        if self.c.shape["hasExactBounds"]:
            self._vec3("root.dtsBoundsMin", self.c.shape["boundsMin"],
                       ex.get("dtsBoundsMin"))
            self._vec3("root.dtsBoundsMax", self.c.shape["boundsMax"],
                       ex.get("dtsBoundsMax"))
        self.eqi("root.dtsTransitionCount", len(self.c.transitions),
                 ex.get("dtsTransitionCount"))

    def _vec3(self, path, src, got):
        if got is None:
            return self.note(path, repr(src), "missing")
        parts = str(got).split(",")
        if len(parts) != 3:
            return self.note(path, repr(src), repr(got))
        for i, p in enumerate(parts):
            self.eqf("%s[%d]" % (path, i), src[i], p)

    # -- materials --------------------------------------------------------

    def checkMaterials(self):
        mats = self.js.get("materials", [])
        seen = {}
        for gi, m in enumerate(mats):
            ex = (m.get("extras") or {})
            si = ex.get("dtsMaterialIndex")
            if si is None:
                self.note("materials[glTF %d].dtsMaterialIndex" % gi,
                          "a source index", "missing")
                continue
            si = int(si)
            if si in seen:
                self.note("materials[%d].dtsMaterialIndex" % si,
                          "one glTF material", "glTF %d and %d both claim it"
                          % (seen[si], gi))
                continue
            seen[si] = gi
            if not (0 <= si < len(self.c.materials)):
                self.note("materials[glTF %d].dtsMaterialIndex" % gi,
                          "0..%d" % (len(self.c.materials) - 1), str(si))
                continue
            src = self.c.materials[si]
            p = "materials[%d]" % si
            # flags is the field the loader used to rebuild from alphaMode, so it is the
            # single most important value in this whole comparison.
            if ex.get("dtsFlags") is None:
                self.note(p + ".flags", "0x%X" % src["flags"], "missing")
            elif int(ex["dtsFlags"]) != src["flags"]:
                self.note(p + ".flags", "0x%X" % src["flags"],
                          "0x%X" % int(ex["dtsFlags"]))
            self.eqs(p + ".mapFile", src["mapFile"], ex.get("dtsMapFile"))
            self.eqf(p + ".alpha", src["alpha"], ex.get("dtsAlpha"))
            self.eqi(p + ".paletteIndex", src["paletteIndex"], ex.get("dtsIndex"))
            r, g, b = src["rgb"]
            self.eqi(p + ".rgb", (r << 16) | (g << 8) | b, ex.get("dtsRGB"))
            self.eqi(p + ".type", src["type"], ex.get("dtsType"))
            self.eqi(p + ".useDefaultProps", src["useDefaultProps"],
                     ex.get("dtsUseDefaultProps"))
            # Only compare the physical properties where the engine READS them
            # (Material::getElasticity/getFriction consult DefaultMaterialProps whenever
            # fUseDefaultProps is set, ts_material.cpp:303-320).
            if not src["useDefaultProps"]:
                self.eqf(p + ".elasticity", src["elasticity"], ex.get("dtsElasticity"))
                self.eqf(p + ".friction", src["friction"], ex.get("dtsFriction"))

        for si in range(len(self.c.materials)):
            if si not in seen:
                self.note("materials[%d]" % si,
                          "carried into the GLB (%r)" % self.c.materials[si]["mapFile"],
                          "absent")

    # -- sequences --------------------------------------------------------

    def checkSequences(self):
        anims = self.js.get("animations", [])
        srcNames = [s["name"] for s in self.c.sequences]
        gotNames = [a.get("name") for a in anims]

        if gotNames != srcNames:
            self.note("sequences[].name (ORDERED)", repr(srcNames), repr(gotNames))

        byName = {}
        for a in anims:
            byName.setdefault(a.get("name"), a)

        for s in self.c.sequences:
            a = byName.get(s["name"])
            p = "sequences[%d]" % s["index"]
            if a is None:
                self.note(p + ".name", repr(s["name"]), "missing")
                continue
            ex = (a.get("extras") or {})
            self.eqi(p + ".cyclic", s["cyclic"], ex.get("dtsCyclic"))
            self.eqi(p + ".priority", s["priority"], ex.get("dtsPriority"))
            self.eqf(p + ".duration", s["duration"], ex.get("dtsDuration"))
            self.eqi(p + ".frameTriggerCount", s["nFrameTriggers"],
                     ex.get("dtsFrameTriggerCount"))

            # frame trigger records
            want = self.c.frameTriggers[s["firstFrameTrigger"]:
                                        s["firstFrameTrigger"] + s["nFrameTriggers"]]
            gotRaw = ex.get("dtsFrameTriggers") or ""
            gotRecs = [r for r in gotRaw.split(";") if r]
            if len(gotRecs) != len(want):
                self.note(p + ".frameTriggers", "%d record(s)" % len(want),
                          "%d record(s)" % len(gotRecs))
                continue
            for i, t in enumerate(want):
                f = gotRecs[i].split(",")
                q = "%s.frameTriggers[%d]" % (p, i)
                if len(f) != 3:
                    self.note(q, "position,value,forward", repr(gotRecs[i]))
                    continue
                self.eqf(q + ".position", t["position"], f[0])
                self.eqi(q + ".value", t["value"], f[1])
                self.eqi(q + ".forward", 1 if t["forward"] else 0, f[2])

    # -- objects / cel ----------------------------------------------------

    def checkObjects(self):
        byIndex = {}
        for n in self.js.get("nodes", []):
            ex = (n.get("extras") or {})
            if "dtsSourceObjectIndex" in ex:
                byIndex.setdefault(int(ex["dtsSourceObjectIndex"]), n)

        # ★The "bounds" object is EXPECTED to be absent.★  A stock .dts carries an
        # invisible AABB proxy on node 0 that the engine only uses to derive
        # fRadius/fCenter/fBounds -- it is never drawn, because rendering walks from a
        # DETAIL ROOT and node 0 is not one.  The exact box now travels as
        # dtsBoundsMin/Max root extras (compared in checkRoot), and the engine already
        # handles a shape whose node 0 owns no object: ts_shadow.cpp:692 falls back to
        # shape->getShapeBox() and documents that fallback as EXACT, not approximate.
        #
        # Counting it as missing geometry would make every correct conversion fail here,
        # which is how a gate gets disabled instead of fixed.
        expected = [o for o in self.c.objects if o["name"].lower() != "bounds"]
        missing = [o for o in expected if o["index"] not in byIndex]
        if missing:
            self.note("objects[].dtsSourceObjectIndex coverage",
                      "%d/%d" % (len(expected), len(expected)),
                      "%d/%d (missing %s)" % (len(expected) - len(missing), len(expected),
                                              [o["name"] for o in missing][:4]))

        for oi, tracks in sorted(self.c.celTracks().items()):
            n = byIndex.get(oi)
            p = "objects[%d]" % oi
            if n is None:
                self.note(p + ".cel", "%d track(s)" % len(tracks),
                          "object not present in the GLB")
                continue
            cel = (n.get("extras") or {}).get("dtsCel") or ""
            for seqName, keys in sorted(tracks.items()):
                if seqName + "=" not in cel:
                    self.note("%s.cel.%s" % (p, seqName),
                              "%d key(s)" % len(keys), "missing")

    # -- geometry ---------------------------------------------------------

    def checkGeometry(self):
        """Compare actual vertex positions, per source object.

        ★This is the check whose absence made the whole tool lie.★  Everything else here
        compares metadata; a controlled test moved one exported vertex by +10 units and
        the checker still printed "exact round trip", because geometry was never looked
        at and every negative control mutated a field that WAS looked at.

        Coordinate convention, established empirically against a known-good conversion
        rather than assumed:

            glTF = ( x, z, -y )   from DTS       (Z-up source -> Y-up glTF)
            DTS  = ( x, -z, y )   from glTF

        ★Vertex COUNTS legitimately differ.★  A DTS face references a vertex and a texture
        vertex independently, so one position can carry several UVs; glTF has one
        attribute stream, so the exporter splits it.  chaingun's mesh 1 is 19 source
        vertices against 67 exported ones.  Comparing counts would therefore fail every
        correct conversion -- so this compares the SET of distinct positions, which is
        exactly what a moved vertex changes.
        """
        self.topo = dict((t["mesh"], t) for t in gce.faceTopologyReport(self.c))
        if self.c.meshes is None:
            # Never let an unavailable check read as a passing one.
            self.note("geometry", "comparable mesh data",
                      "UNAVAILABLE -- dts.py could not read this shape's meshes, so "
                      "geometry was NOT compared")
            return

        try:
            gv = _glbPositionsByNode(self.js, self.glbRaw)
        except Exception as e:
            self.note("geometry", "readable GLB accessors", "could not decode: %s" % e)
            return

        for n in self.js.get("nodes", []):
            ex = (n.get("extras") or {})
            if "dtsSourceObjectIndex" not in ex or "mesh" not in n:
                continue
            oi = int(ex["dtsSourceObjectIndex"])
            if not (0 <= oi < len(self.c.objects)):
                continue
            obj = self.c.objects[oi]
            mi = obj["meshIndex"]
            if not (0 <= mi < len(self.c.meshes)):
                continue
            src = self.c.meshes[mi]
            p = "objects[%d:%s].geometry" % (oi, obj["name"])

            got = gv.get(id(n)) or gv.get(n["mesh"])
            if got is None:
                self.note(p, "%d vertex position(s)" % len(src["vertices"]), "no accessor")
                continue

            # glTF -> DTS, then match positions within a TOLERANCE.
            #
            # ★This used to round to 5dp and compare the rounded sets exactly.★  The
            # INTENT was right -- absorb float32 round-off, catch real movement -- but
            # round-then-exact-compare is discontinuous: two positions astride a rounding
            # boundary land in different buckets no matter how close they actually are.
            # It failed 5 shapes (kn_gorg, kn_mino, tr_basl, tr_gorg, tr_mino) and every
            # one of the 12 reported mismatches was EXACTLY 1.0e-05 -- one unit in the
            # last rounded place, the signature of a bucket boundary rather than of moved
            # geometry, which would give varied magnitudes.  One float32 ulp at those
            # magnitudes is 3.6e-07, ~27x smaller than what was being flagged.
            #
            # TOL is deliberately explicit rather than "a bit more rounding".  At 1e-4 it
            # is ~280 float32 ulps at magnitude 3 -- ample for accumulated round-off --
            # while the +10-unit mutation that once passed this gate as an "exact round
            # trip" is 100,000x larger.  Widening this to make reds go green is exactly
            # how that false certification happened, so test_roundtrip_mutations.py pins
            # both ends: sub-tolerance jitter must PASS, real displacement must FAIL.
            TOL = 1e-4

            gotPts = [(v[0], -v[2], v[1]) for v in got]
            srcPts = [(v[0], v[1], v[2]) for v in src["vertices"]]

            def near(a, b):
                return (abs(a[0] - b[0]) <= TOL and abs(a[1] - b[1]) <= TOL
                        and abs(a[2] - b[2]) <= TOL)

            def unmatched(needles, haystack):
                # Deduplicate first so one source position with several UVs (and hence
                # several exported vertices) is not reported repeatedly.
                out, seen = [], set()
                for q in needles:
                    k = (round(q[0], 6), round(q[1], 6), round(q[2], 6))
                    if k in seen:
                        continue
                    seen.add(k)
                    if not any(near(q, h) for h in haystack):
                        out.append(q)
                return out

            missing = unmatched(srcPts, gotPts)
            extra = unmatched(gotPts, srcPts)
            nDistinct = len(set((round(v[0], 6), round(v[1], 6), round(v[2], 6))
                                for v in srcPts))
            if missing or extra:
                bits = []
                if missing:
                    bits.append("%d source position(s) absent e.g. %s"
                                % (len(missing), sorted(missing)[:2]))
                if extra:
                    bits.append("%d position(s) not in the source e.g. %s"
                                % (len(extra), sorted(extra)[:2]))
                self.note(p, "%d distinct position(s)" % nDistinct, "; ".join(bits))
                continue

            # Face count, adjusted for the ONE difference that is provably inert:
            # a triangle stored twice with the same winding and material renders the
            # same pixels twice, so Blender collapsing it changes nothing visible.
            # See glb_contract_extras.faceTopologyReport for why the OTHER difference
            # (a mirrored pair inside a single-sided material) is NOT forgiven -- the
            # converter refuses that shape outright, so it can never reach this gate.
            topo = self.topo.get(mi, {})
            expected = src["numFaces"] - topo.get("redundantFaces", 0)
            nGlbTris = _triCount(self.js, n["mesh"])
            if nGlbTris != expected:
                self.note(p + ".faceCount",
                          "%d (%d source - %d redundant duplicate(s))"
                          % (expected, src["numFaces"], topo.get("redundantFaces", 0)),
                          str(nGlbTris))

    # -- textures ---------------------------------------------------------

    def checkNoTextures(self):
        try:
            gce.assertNoEmbeddedImages(self.js)
        except gce.ExtrasError as e:
            self.note("images/textures", "none (dtsMapFile is authoritative)", str(e))

    def run(self):
        self.checkRoot()
        self.checkMaterials()
        self.checkSequences()
        self.checkObjects()
        self.checkGeometry()
        self.checkNoTextures()
        return self.bad


def main(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: check_roundtrip_contract.py <source.dts> <converted.glb>\n")
        return 2
    src, glb = argv[1], argv[2]

    try:
        contract = dts_contract.load(src)
    except dts_contract.UnsupportedSource as e:
        sys.stderr.write("unsupported source: %s\n" % e)
        return 3
    except dts_contract.ContractError as e:
        sys.stderr.write("cannot read source: %s\n" % e)
        return 2

    if contract.defects:
        sys.stderr.write("source carries %d defect(s); an exact round trip is not "
                         "defined for it:\n" % len(contract.defects))
        for d in contract.defects[:8]:
            sys.stderr.write("  [%s] %s\n" % (d["code"], d["detail"]))
        return 3

    try:
        js, _chunks, raw = gce.readGlb(glb)
    except gce.ExtrasError as e:
        sys.stderr.write("cannot read GLB: %s\n" % e)
        return 2

    bad = Checker(contract, js, raw).run()
    if bad:
        print("FAIL: %d fidelity mismatch(es) between\n  %s\n  %s"
              % (len(bad), src, glb))
        for m in bad:
            print("  %s" % m)
        return 1

    print("PASS: %s is an exact round trip of %s" % (os.path.basename(glb),
                                                     os.path.basename(src)))
    print("  %d node(s), %d object(s), %d material(s), %d sequence(s), "
          "%d frame trigger(s), %d transition(s)"
          % (len(contract.nodes), len(contract.objects), len(contract.materials),
             len(contract.sequences), len(contract.frameTriggers),
             len(contract.transitions)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
