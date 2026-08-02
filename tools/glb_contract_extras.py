"""glb_contract_extras -- write the DTS contract v1 into a GLB's glTF `extras`.

The converter used to carry exactly ONE thing across the DTS->GLB boundary: each
material's map filename.  Everything else about a material was rebuilt by the loader
from `alphaMode`, which is a two-bit approximation of a nine-field record -- so exact
shading mode, transparent-vs-translucent, palette index, RGB, fAlpha, surface type,
elasticity, friction and fUseDefaultProps were all silently replaced with defaults.
★Surface type and friction are gameplay-consumed, not cosmetic.★

This module carries the whole contract.

WHY `extras` AND NOT A REGISTERED glTF EXTENSION
    `extras` is plain JSON on any object, so Blender's exporter preserves it, cgltf
    hands it back as a NUL-terminated substring, and no validator anywhere rejects the
    file.  A custom extension would need registration and would make every generic tool
    treat the file as requiring an unknown capability.

WHY VALUES ARE STRINGS RATHER THAN NESTED JSON
    cgltf does NOT parse extras -- it hands over the raw JSON substring
    (cgltf.h:3021-3031) and the loader scans it.  Flat scalars and delimited strings are
    cheap and bounded to scan; nested objects would mean writing a JSON parser inside
    ts_gltf.cpp.  Grammars are documented per field below.

BACKWARD COMPATIBILITY
    A GLB with no `dtsContractVersion` on its root node is a GENERIC authored glTF and
    the loader must keep treating it as one.  Nothing here is required.
"""

import json
import struct


CONTRACT_VERSION = 1


class ExtrasError(Exception):
    pass


# Characters that would corrupt a delimited record.  A name containing one of these is
# refused rather than mangled -- silently escaping it would make the round-trip gate
# compare a mangled value against a clean source and report a false mismatch.
_DELIMS = ";=|,\"\\"


def _checkDelims(what, s):
    for ch in _DELIMS:
        if ch in s:
            raise ExtrasError("%s %r contains the reserved delimiter %r -- refusing to "
                              "write a record that cannot be read back exactly"
                              % (what, s, ch))


def _f(v):
    """Round-trip float formatting.

    `repr` on a Python float is the shortest string that reads back to the SAME double
    (PEP 3141 / repr since 3.1).  Anything shorter -- '%.6g' say -- makes the round-trip
    gate compare a truncated value against the exact source and report drift the
    conversion did not actually introduce.
    """
    return repr(float(v))


# ---------------------------------------------------------------------------
# GLB container read/modify/write

def readGlb(path):
    """-> (json dict, [(chunkType, payload|None)], rawBytes).  JSON payload is None."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 12 or data[0:4] != b"glTF":
        raise ExtrasError("%s is not a binary glTF (.glb)" % path)
    off, chunks, js = 12, [], None
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<I4s", data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == b"JSON":
            js = json.loads(chunk.decode("utf-8"))
            chunks.append([ctype, None])
        else:
            chunks.append([ctype, chunk])
    if js is None:
        raise ExtrasError("%s has no JSON chunk" % path)
    return js, chunks, data


def writeGlb(path, js, chunks):
    newJson = json.dumps(js, separators=(",", ":")).encode("utf-8")
    while len(newJson) % 4:
        newJson += b" "
    out = bytearray()
    for ctype, payload in chunks:
        body = newJson if ctype == b"JSON" else payload
        if ctype != b"JSON":
            while len(body) % 4:
                body = body + b"\x00"
        out += struct.pack("<I4s", len(body), ctype) + body
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, 12 + len(out)) + bytes(out))


def soleRootIndex(js):
    """The index of the ONE real scene root, or None.

    This must agree with GltfShapeBuilder::buildNodes (ts_gltf.cpp:662-690), which
    promotes a sole scene root to TS node 0 and synthesises a 'bounds' root otherwise.
    Root-node extras are meaningless if the loader would not read that same node.
    """
    scenes = js.get("scenes") or []
    if scenes:
        roots = scenes[js.get("scene", 0)].get("nodes", [])
    else:
        child = set()
        for n in js.get("nodes", []):
            for c in n.get("children", []):
                child.add(c)
        roots = [i for i in range(len(js.get("nodes", []))) if i not in child]
    return roots[0] if len(roots) == 1 else None


# ---------------------------------------------------------------------------
# Root-node extras

def rootExtras(contract):
    """Shape-wide contract: version, exact bounds, transitions.

    ★Exact bounds are the point.★  buildBounds() measures the REST pose and inflates an
    animated shape by 25% as scaffolding.  Stock bounds cover every authored animation
    pose (rpgmalehuman's is ~2.5x its bind-pose AABB) and drive culling, LOD selection,
    collision and framing -- so a measured-plus-guess box is not source parity and must
    not be reported as such.

    dtsTransitions grammar, records separated by ';':
        startSeq,endSeq,startPos,endPos,duration,<transformHex>
    """
    sh = contract.shape
    ex = {
        "dtsContractVersion": CONTRACT_VERSION,
        "dtsSourceVersion": contract.version,
        "dtsRadius": float(sh["radius"]),
        "dtsCenter": ",".join(_f(v) for v in sh["center"]),
        "dtsTransitionCount": len(contract.transitions),
    }
    if sh["hasExactBounds"]:
        ex["dtsBoundsMin"] = ",".join(_f(v) for v in sh["boundsMin"])
        ex["dtsBoundsMax"] = ",".join(_f(v) for v in sh["boundsMax"])

    recs = []
    for t in contract.transitions:
        recs.append("%d,%d,%s,%s,%s,%s" % (
            t["startSequence"], t["endSequence"], _f(t["startPosition"]),
            _f(t["endPosition"]), _f(t["duration"]), t["transformRaw"]))
    ex["dtsTransitions"] = ";".join(recs)
    return ex


# ---------------------------------------------------------------------------
# Animation extras

def animationExtras(contract, seqName):
    """Per-sequence metadata the loader currently hardcodes or drops.

    dtsFrameTriggers grammar, records separated by ';':
        position,value,forward
    ★`forward` is carried as its own field, not as the sign of `position`.★  The engine
    encodes direction in the SIGN of FrameTrigger::fPosition and hands back |pos| from
    getPosition() (ts_shape.h:325-328).  Re-deriving that convention independently in a
    Python writer and a C++ reader is how the two drift; the loader passes these three
    values to the Shape::FrameTrigger constructor and lets IT apply the convention once.

    Frame triggers drive player footstep sounds and decals through findTriggerFrames,
    so a converted player currently looks right while losing every footfall.
    """
    seqs = [s for s in contract.sequences if s["name"] == seqName]
    if not seqs:
        return None
    s = seqs[0]

    trigs = contract.frameTriggers[s["firstFrameTrigger"]:
                                   s["firstFrameTrigger"] + s["nFrameTriggers"]]
    recs = ["%s,%d,%d" % (_f(t["position"]), t["value"], 1 if t["forward"] else 0)
            for t in trigs]

    return {
        "dtsCyclic": int(s["cyclic"]),
        "dtsPriority": int(s["priority"]),
        "dtsSequenceIndex": int(s["index"]),
        "dtsDuration": _f(s["duration"]),
        "dtsFrameTriggerCount": len(trigs),
        "dtsFrameTriggers": ";".join(recs),
        "dtsIFLCount": int(s["nIFLSubSequences"]),
        "dtsIFL": "",
    }


# ---------------------------------------------------------------------------
# Material extras

def materialExtras(contract, dtsIndex):
    """Every field of Material::Params, so the loader restores rather than invents.

    dtsRGB is the packed unsigned (red << 16) | (green << 8) | blue.
    dtsFlags is the EXACT flag word Material::resolveRender switches on -- the loader
    must validate it against that switch rather than trust it, because an unrecognised
    word reaches an AssertFatal (ts_material.cpp:253-255).
    """
    if not (0 <= dtsIndex < len(contract.materials)):
        raise ExtrasError("material index %d out of range (%d in source)"
                          % (dtsIndex, len(contract.materials)))
    m = contract.materials[dtsIndex]
    _checkDelims("material map file", m["mapFile"])
    r, g, b = m["rgb"]
    return {
        "dtsMaterialIndex": int(m["index"]),
        "dtsFlags": int(m["flags"]),
        "dtsMapFile": m["mapFile"],
        "dtsAlpha": _f(m["alpha"]),
        "dtsIndex": int(m["paletteIndex"]),
        "dtsRGB": (int(r) << 16) | (int(g) << 8) | int(b),
        "dtsType": int(m["type"]),
        "dtsElasticity": _f(m["elasticity"]),
        "dtsFriction": _f(m["friction"]),
        "dtsUseDefaultProps": int(m["useDefaultProps"]),
    }


# ---------------------------------------------------------------------------
# Texture exclusion

def doubleSidedMaterials(contract):
    """Which source materials are genuinely two-sided, measured from the faces.

    ★Blender's DTS importer marks EVERY material doubleSided, so the exported flag is a
    blanket default and means nothing.★  Trusting it makes the loader emit a reversed face
    for the entire model -- chaingun's body went from 97 faces to 194 -- which doubles
    draw cost and invites z-fighting between the coincident pairs.

    Two-sidedness is a per-FACE property in a .dts: this engine culls by winding and has
    no two-sided material (ts_CelAnimMesh.cpp:88-96), so a card that must be seen from
    both sides is stored as the same triangle TWICE with reversed winding.  chaingun's
    muzzle flashes are 8 faces forming 4 mirrored pairs; its body is not paired at all.

    A material is reported two-sided only when EVERY face using it is one of a mirrored
    pair.  That maps the per-face source property onto the per-material glTF flag without
    inventing two-sidedness the source never had.

    Returns a set of source material indices.
    """
    if contract.meshes is None:
        return set()

    # ★A triangle triple is equivalent under ROTATION, not just as written.★  chaingun
    # stores the mirror of (2,4,5) as (5,4,2) -- same three indices, opposite winding, but
    # rotated so that a literal reversed-tuple test finds nothing.  The first version of
    # this function did exactly that and reported "no two-sided materials" on a shape
    # whose flash cards are entirely mirrored pairs.  Canonicalise each winding by
    # rotating its smallest index to the front, then the mirror is well-defined.
    def canon(v):
        i = v.index(min(v))
        return (v[i], v[(i + 1) % 3], v[(i + 2) % 3])

    def mirror(v):
        return canon((v[0], v[2], v[1]))

    paired, total = {}, {}
    for mesh in contract.meshes:
        byMat = {}
        for f in mesh["faces"]:
            byMat.setdefault(f["material"], set()).add(canon(tuple(f["vertices"])))
        for mat, faces in byMat.items():
            for v in faces:
                total[mat] = total.get(mat, 0) + 1
                if mirror(v) in faces:
                    paired[mat] = paired.get(mat, 0) + 1

    return set(m for m in total
               if total[m] > 0 and paired.get(m, 0) == total[m])


def faceTopologyReport(contract):
    """Per-mesh accounting of the two ways a DTS face set cannot survive glTF exactly.

    glTF has ONE triangle per (vertex-triple, winding) and expresses two-sidedness
    PER MATERIAL.  A .dts expresses it PER FACE and may repeat a face outright.  Two
    consequences, which need opposite treatment:

    `redundantFaces` -- the same triangle with the SAME winding and material, stored
        twice.  mortar_turret's 'hulk 5' does this (faces 7 and 9 are both [3,10,6]
        material 0).  It renders identical pixels twice, so Blender merging it is
        provably inert -- it removes z-fighting rather than causing a visual change.
        These are ACCOUNTED FOR, not forgiven blindly: the expected converted face
        count is reduced by exactly this number.

    `partialTwoSidedFaces` -- a mirrored pair inside a material whose OTHER faces are
        single-sided.  ★This is a real, unrepresentable loss.★  doubleSidedMaterials()
        can only flag the whole material, so flagging it would double every other face
        and not flagging it drops the second side.  mortar_turret has 2 such faces of
        44.  A shape with any of these cannot be an exact conversion and must say so
        rather than quietly render one side.
    """
    out = []
    if contract.meshes is None:
        return out

    def canon(v):
        i = v.index(min(v))
        return (v[i], v[(i + 1) % 3], v[(i + 2) % 3])

    twoSided = doubleSidedMaterials(contract)

    for mesh in contract.meshes:
        seen = {}
        for f in mesh["faces"]:
            key = (canon(tuple(f["vertices"])), f["material"])
            seen[key] = seen.get(key, 0) + 1
        redundant = sum(n - 1 for n in seen.values() if n > 1)

        partial = 0
        byMat = {}
        for (tri, mat), _n in seen.items():
            byMat.setdefault(mat, set()).add(tri)
        for mat, tris in byMat.items():
            if mat in twoSided:
                continue                       # whole material is two-sided: expressible
            for t in tris:
                if canon((t[0], t[2], t[1])) in tris:
                    partial += 1

        out.append({"mesh": mesh["index"],
                    "faces": len(mesh["faces"]),
                    "redundantFaces": redundant,
                    "partialTwoSidedFaces": partial})
    return out


def assertNoEmbeddedImages(js):
    """A strict DTS GLB must reference no image at all.

    ★The engine does not decode embedded glTF image buffer views.★  It uses `image.uri`
    or `image.name` as a ResourceManager FILENAME, and only falls back to `dtsMapFile`
    when no image reference exists (ts_gltf.cpp:792-818).  So an embedded image does not
    merely bloat the file -- it OUTRANKS the authoritative map name and resolves to a
    texture that does not exist.

    It also breaks skin swapping outright: `base.larmor.BMP` is the Tribes
    <skinbase>.<armour>.BMP convention and the engine rewrites that prefix per player at
    runtime.  A baked texture freezes one armour appearance forever.

    This matters NOW because chaingun ships `chaingun.png` and `pulse.png` beside its
    .DTS, and the importer resolves sibling PNGs -- so this is the first asset whose
    default export would actually embed images.  The three already-deployed GLBs carry
    none, so they never exercised this path.
    """
    problems = []
    if js.get("images"):
        problems.append("%d image(s)" % len(js["images"]))
    if js.get("textures"):
        problems.append("%d texture(s)" % len(js["textures"]))
    for i, m in enumerate(js.get("materials", [])):
        pbr = m.get("pbrMetallicRoughness", {})
        if "baseColorTexture" in pbr:
            problems.append("material %d has a baseColorTexture" % i)
    if problems:
        raise ExtrasError(
            "DTS-derived GLB references textures (%s). The engine reads image.uri as a "
            "ResourceManager filename and it OUTRANKS dtsMapFile, so this would resolve "
            "to the wrong texture and break per-player skin remap."
            % ", ".join(problems))


# ---------------------------------------------------------------------------
# Loader-equivalent keyframe accounting

def _binOffset(raw):
    off = 12
    while off + 8 <= len(raw):
        clen, ctype = struct.unpack_from("<I4s", raw, off)
        off += 8
        if ctype[0:3] == b"BIN":
            return off
        off += clen
    return None


def readScalarAccessor(js, raw, index):
    """The ACTUAL float values of a SCALAR accessor, decoded from the BIN chunk.

    ★Synthesising timestamps from min/max/count is not loader-equivalent.★  The previous
    version did that and claimed it could "never be an UNDER-count", which is false: two
    channels with identical endpoints and counts but different irregular interior times
    have a real union larger than the union of two uniform grids.  Decimation fits each
    channel independently, so irregular per-channel times are the NORMAL case here, and
    the native loader reads the real values.  An undercount lets a GLB through that the
    loader then truncates at the Int16 cap -- silently.
    """
    a = js["accessors"][index]
    if a.get("type") != "SCALAR":
        raise ValueError("accessor %d is %s, expected SCALAR" % (index, a.get("type")))
    ctype = a.get("componentType")
    if ctype != 5126:                                    # FLOAT
        raise ValueError("accessor %d componentType %s, expected FLOAT" % (index, ctype))
    if "bufferView" not in a:
        return [0.0] * a.get("count", 0)                 # sparse/zero-filled

    binOff = _binOffset(raw)
    if binOff is None:
        raise ValueError("no BIN chunk")
    bv = js["bufferViews"][a["bufferView"]]
    base = binOff + bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride", 4)
    return [struct.unpack_from("<f", raw, base + i * stride)[0]
            for i in range(a["count"])]


def loaderKeyCount(js, raw, animation):
    """Count keyframes exactly as GltfShapeBuilder::buildAnimations will.

    The loader's algorithm, mirrored step for step:
      per (animation, node): collect the translation + rotation input times,
      add 0 and the authored duration, keep interior values strictly inside
      (0, duration), sort, dedupe with a 1e-5 threshold, one keyframe per survivor.

    `raw` is the whole GLB image, because the timestamps are read for real.
    """
    perNode = {}
    duration = 0.0
    for ch in animation.get("channels", []):
        path = ch.get("target", {}).get("path")
        if path not in ("translation", "rotation"):
            continue
        nd = ch["target"].get("node")
        ai = animation["samplers"][ch["sampler"]]["input"]
        times = readScalarAccessor(js, raw, ai)
        if times:
            duration = max(duration, max(times))
        perNode.setdefault(nd, []).extend(times)

    total = 0
    for nd, times in perNode.items():
        keep = [t for t in times if 0.0 < t < duration] + [0.0, duration]
        keep.sort()
        out = []
        for t in keep:
            if not out or (t - out[-1]) > 1e-5:
                out.append(t)
        total += len(out)
    return total


def totalLoaderKeys(js, raw):
    return sum(loaderKeyCount(js, raw, a) for a in js.get("animations", []))
