"""dts_contract -- the ONE authoritative reader for a .dts shape's wire contract.

Every tool in this toolkit used to grow its own copy of the DTS offset chain:
`dts_sequence_owner_counts` walked it for sequences, `dts_cel_tracks` walked the
SAME chain again for objects, and `dts_material_maps` gave up entirely and scanned
the file for a byte signature that "looked like" a material list.  Three readers,
three chances to drift, and one of them (materials) was a guess that had already
produced a documented false negative.

This module replaces all three.  It reads the shape ONCE, deterministically, and
hands back an immutable snapshot that the converter and both gates consume.

★Every offset here is a running sum of the engine's own record sizes.★  Nothing is
scanned for and nothing is stride-solved.  The one table no offset chain reaches --
the MaterialList, which sits behind a run of variable-size Persistent mesh blocks --
is anchored on its own `TS::MaterialList` Persistent tag, and the block version that
immediately follows that tag gives the record size outright.  See readMaterials().

★A shape that reads cleanly is not the same as a shape that is CORRECT.★  Problems in
otherwise-readable data (zero-duration sequences, the NaN keyframes they produce, a
wrapped Int16 keyframe index) are recorded in `contract.defects` rather than raised.
Raising made 8 stock shapes unreadable, which meant no tool could explain why they were
excluded -- and the engine loads them today.  See the DEFECT_ block below.

Read order (TS::Shape::read, ts_shape.cpp:1150-1207 lock branch, no per-vector
count prefix -- the counts are all in the header):

    header
    nodes, sequences, subsequences, keyframes, transforms, names,
    objects, details, transitions, frameTriggers,
    fnDefaultMaterials (v>=5), fAlwaysNode (v>=6),
    meshes (Persistent blocks -- variable size),
    hasMaterials (Int32), MaterialList (Persistent block)

Record sizes, each verified in the header it comes from:

    Node       v<=7  Int32 x5                     = 20   ts_shape.h:214-221
               v8    Int16 x5                     = 10
    Sequence   v>=5  Int32 x8                     = 32   ts_shape.h:167-184
    SubSeq     v<=7  Int32 x3                     = 12   ts_shape.h:186-194
               v8    Int16 x3                     =  6
    Keyframe   v<=7  RealF + UInt32 x2            = 12   ts_shape.cpp:985-989
               v8    RealF + UInt16 x2            =  8   ts_shape.h:127-141
    Transform  v<7   QuatF + Point3F x2           = 40   ts_transform.h:31-37
               v==7  Quat16 + Point3F x2          = 32   ts_transform.h:170-181
               v8    Quat16 + Point3F             = 20   ts_transform.h:91-95
    Name       char[24]                                  ts_types.h MaxNameSize
    Object     v<=7  I16,I16,I32,I32,TMat3F,I32,I32 = 72 ts_shape.cpp:1005-1013
               v8    I16,I16,I32,I16,pad,Point3F,I16,I16 = 28  ts_shape.h:227-249
    Detail     Int32 + RealF                      =  8   ts_shape.h:250-256
    Transition v<7   20 + V6Transform(40)         = 60   ts_shape.h:201-212
               v==7  20 + V7Transform(32)         = 52
               v8    20 + Transform(20)           = 40
    FrameTrig  RealF + Int32                      =  8   ts_shape.h:319-329
    Material   Params, minus the fields the list VERSION drops   ts_material.cpp:276-295
               v1/v2 48   v3 60   v4+ 64

Versions below 5 are REFUSED, not guessed: v4/v3 read oldSequencesV4/V3 with
different record layouts (ts_shape.cpp:1125-1129).
"""

import json
import math
import struct
from types import MappingProxyType


# ---------------------------------------------------------------------------

class ContractError(Exception):
    """The source could not be read as a DTS shape at all (exit code 2)."""


class UnsupportedSource(Exception):
    """The source is a DTS shape this reader deliberately refuses (exit code 3)."""


# Defect codes.  ★A DEFECT IS NOT A PARSE FAILURE.★
#
# The first version of this reader raised on a NaN keyframe position and on a
# subsequence whose firstKeyframe had wrapped, and refused 8 stock shapes outright.
# That was wrong, and the engine says so: the ELF-hang fix at ts_shapeInst.cpp:1394
# documents zero-duration sequences as "real shipped data, not corruption" -- stock
# shotgun.DTS and repairgun.DTS both carry `reload` with fCyclic=1, fDuration=0, and
# the engine answers position 0 for them.  Those shapes load and play today.
#
# The NaN keyframe positions in marmor/bighead/min/Newmino/tempcyborg/chewy/marmorgnoll
# are the SECOND-ORDER consequence of the same exporter bug: it wrote time/duration into
# every key of a zero-duration sequence, so every position came out 0/0 = NaN.
#
# Refusing to parse means the converter cannot even REPORT why an asset is excluded, and
# the audit is explicit that unsupported assets must be explicit manifest exclusions,
# never silent passes.  So the reader reads what the ENGINE would read, and records what
# is wrong with it.  Gates decide what to do about it.
DEFECT_NAN_KEY = "nanKeyframePosition"
DEFECT_ZERO_DURATION = "zeroDurationSequence"
DEFECT_INDEX_WRAP = "subSequenceIndexWrap"
DEFECT_KEY_OUT_OF_RANGE = "keyframePositionOutOfRange"
DEFECT_NONUNIT_TRANSITION_SCALE = "nonUnitTransitionScale"


# Material::resolveRender switches on the EXACT flag word (ts_material.cpp:206-256),
# so these are the only legal values.  Anything else reaches its AssertFatal.
MAT_FLAGS = 0x0f
MAT_NULL, MAT_PALETTE, MAT_RGB, MAT_TEXTURE = 0x00, 0x01, 0x02, 0x03
SHADING_NONE, SHADING_FLAT, SHADING_SMOOTH = 0x100, 0x200, 0x400
TEX_TRANSPARENT, TEX_TRANSLUCENT = 0x1000, 0x2000
SKY_PLACEHOLDER = 0xf000

LEGAL_MATERIAL_FLAGS = frozenset(
    [SKY_PLACEHOLDER]
    + [s | m for s in (SHADING_NONE, SHADING_FLAT, SHADING_SMOOTH)
       for m in (MAT_PALETTE, MAT_RGB, MAT_TEXTURE)]
    + [s | MAT_TEXTURE | t for s in (SHADING_NONE, SHADING_FLAT, SHADING_SMOOTH)
       for t in (TEX_TRANSPARENT, TEX_TRANSLUCENT)]
)

# Material::SurfaceType, ts_material.h:143-159.  DefaultType..PackedEarthType.
MAX_SURFACE_TYPE = 0xE

# Keyframe::fMatIndex bit layout.  ★v7 keeps the same four flags in the HIGH bits of
# 32-bit fields (ts_shape.cpp:991-994) rather than v8's 16-bit masks.★  Reading v7
# with the v8 masks yields zero flags on every key -- a track that "cares about
# nothing", which looks exactly like a clean parse.
_KEY_MASKS_V7 = (0x80000000, 0x40000000, 0x20000000, 0x10000000, 0x0fffffff)
_KEY_MASKS_V8 = (0x8000, 0x4000, 0x2000, 0x1000, 0x0fff)

CEL_VISIBLE, CEL_CARES_VIS, CEL_CARES_MAT, CEL_CARES_FRAME = 1, 2, 4, 8

_NAME_LEN = 24


def _namelike(s):
    return len(s) >= 1 and any(c.isalnum() for c in s) and all(32 <= ord(c) <= 126 for c in s)


def _finite(*vals):
    for v in vals:
        if not isinstance(v, float):
            continue
        if math.isnan(v) or math.isinf(v):
            return False
    return True


def _cstr(raw):
    return raw.split(b"\x00")[0].decode("latin1")


def _freeze(v):
    """Recursively make a parsed structure read-only.

    ★"Immutable" has to mean RECURSIVELY immutable.★  Blocking __setattr__ on the Contract
    only protected the top-level names; an audit did

        c.shape["radius"] = 123.0
        c.nodes[0]["name"] = "MUTATED"

    and both succeeded.  Since every gate in this toolkit compares against this object as
    the source of truth, a consumer that mutates it in place does not produce a failure --
    it produces a WRONG PASS, and one that no later run can reproduce.

    dicts become MappingProxyType, lists become tuples.  Both still read normally, so no
    consumer changes; only writes raise.
    """
    if isinstance(v, dict):
        return MappingProxyType(dict((k, _freeze(x)) for k, x in v.items()))
    if isinstance(v, list):
        return tuple(_freeze(x) for x in v)
    return v


def _thaw(v):
    """Plain mutable copy, for json.dumps (which cannot serialise MappingProxyType)."""
    if isinstance(v, (dict, MappingProxyType)):
        return dict((k, _thaw(x)) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return [_thaw(x) for x in v]
    return v


# ---------------------------------------------------------------------------

class Contract(object):
    """An immutable snapshot of one .dts shape.

    Attribute access only; nothing here mutates after load().  `to_json()` is
    deterministic (sorted keys, fixed float repr) so a snapshot can be committed
    as a fixture and diffed.
    """

    __slots__ = ("path", "version", "shape", "nodes", "objects", "meshCount",
                 "details", "sequences", "subSequences", "keyframes",
                 "transformCount", "transitions", "frameTriggers", "materials",
                 "materialListVersion", "defects", "meshes",
                 "names", "alwaysNode", "nDefaultMaterials", "_frozen")

    def __init__(self, **kw):
        object.__setattr__(self, "_frozen", False)
        for k in self.__slots__:
            if k != "_frozen":
                # _freeze recursively: see its docstring for the audit that showed
                # top-level protection alone let a consumer rewrite the source truth.
                object.__setattr__(self, k, _freeze(kw.get(k)))
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, k, v):
        if getattr(self, "_frozen", False):
            raise AttributeError("Contract is immutable (tried to set %r)" % k)
        object.__setattr__(self, k, v)

    # -- convenience views -------------------------------------------------

    def sequenceNames(self):
        return [s["name"] for s in self.sequences]

    def nodeNames(self):
        return [n["name"] for n in self.nodes]

    def nodeByName(self, name):
        for n in self.nodes:
            if n["name"] == name:
                return n
        return None

    def celTracks(self):
        """{sourceObjectIndex: {sequenceName: [key, ...]}} for objects that have any.

        ★Keyed by source object INDEX, not by name.★  Names are not unique in a
        .dts and Blender appends `.001` to duplicates on import, so a name-keyed
        cel table silently drops or misroutes tracks.  Each key is a dict with
        pos / flags / keyValue / matIndex.
        """
        out = {}
        for obj in self.objects:
            if obj["celTracks"]:
                out[obj["index"]] = obj["celTracks"]
        return out

    def duplicateObjectNames(self):
        seen, dupes = {}, set()
        for obj in self.objects:
            if obj["name"] in seen:
                dupes.add(obj["name"])
            seen[obj["name"]] = 1
        return sorted(dupes)

    def duplicateSequenceNames(self):
        names = self.sequenceNames()
        return sorted(set(n for n in names if names.count(n) > 1))

    def nodeOwnership(self):
        """set of (nodeName, sequenceName) the .dts actually animates."""
        owned = set()
        seqNames = self.sequenceNames()
        for n in self.nodes:
            for k in range(n["firstSubSequence"], n["firstSubSequence"] + n["nSubSequences"]):
                si = self.subSequences[k]["sequenceIndex"]
                if 0 <= si < len(seqNames):
                    owned.add((n["name"], seqNames[si]))
        return owned

    # -- serialization -----------------------------------------------------

    def to_dict(self):
        return {
            "version": self.version,
            "shape": self.shape,
            "nodes": self.nodes,
            "objects": self.objects,
            "meshCount": self.meshCount,
            "details": self.details,
            "sequences": self.sequences,
            "subSequences": self.subSequences,
            "keyframes": self.keyframes,
            "transformCount": self.transformCount,
            "transitions": self.transitions,
            "frameTriggers": self.frameTriggers,
            "materials": self.materials,
            "materialListVersion": self.materialListVersion,
            "defects": self.defects,
            "meshes": self.meshes,
            "names": self.names,
            "alwaysNode": self.alwaysNode,
            "nDefaultMaterials": self.nDefaultMaterials,
        }

    def to_json(self):
        """Deterministic -- running it twice must produce byte-identical output."""
        return json.dumps(_thaw(self.to_dict()), sort_keys=True, indent=2,
                          separators=(",", ": "))


# ---------------------------------------------------------------------------

class _Reader(object):
    def __init__(self, path):
        self.path = path
        try:
            self.data = open(path, "rb").read()
        except Exception as e:
            raise ContractError("cannot open %s: %s" % (path, e))
        if len(self.data) < 64:
            raise ContractError("%s is too small to be a DTS shape" % path)
        self.defects = []

    # -- header ------------------------------------------------------------

    def readHeader(self):
        d = self.data
        i = d.find(b"TS::Shape")
        if i < 0:
            raise ContractError("no 'TS::Shape' persistent tag in %s" % self.path)
        o = i + 10
        try:
            (self.version, self.nNodes, self.nSeq, self.nSubSeq, self.nKeyframes,
             self.nTransforms, self.nNames, self.nObjects, self.nDetails,
             self.nMeshes) = struct.unpack_from("<10i", d, o)
        except struct.error as e:
            raise ContractError("truncated shape header: %s" % e)
        o += 40

        self.nTransitions = 0
        self.nFrameTriggers = 0
        if self.version >= 2:
            self.nTransitions = struct.unpack_from("<i", d, o)[0]
            o += 4
        if self.version >= 4:
            self.nFrameTriggers = struct.unpack_from("<i", d, o)[0]
            o += 4

        self.radius = struct.unpack_from("<f", d, o)[0]
        o += 4
        self.center = list(struct.unpack_from("<3f", d, o))
        o += 12

        # v8 stores an explicit bounds box; older versions synthesise one from
        # radius/center at load (ts_shape.cpp:1145-1160) and then shrink it to the
        # 'bounds' mesh, so there is nothing exact to carry.
        self.boundsMin = None
        self.boundsMax = None
        if self.version > 7:
            self.boundsMin = list(struct.unpack_from("<3f", d, o))
            self.boundsMax = list(struct.unpack_from("<3f", d, o + 12))
            o += 24

        if self.version < 5:
            raise UnsupportedSource(
                "DTS version %d predates the v5 record layout (oldSequencesV3/V4 have "
                "different record sizes) -- refusing to guess" % self.version)

        for label, n, lim in (("nodes", self.nNodes, 4096),
                              ("sequences", self.nSeq, 4096),
                              ("subSequences", self.nSubSeq, 1 << 20),
                              ("keyframes", self.nKeyframes, 1 << 22),
                              ("transforms", self.nTransforms, 1 << 22),
                              ("names", self.nNames, 1 << 16),
                              ("objects", self.nObjects, 4096),
                              ("details", self.nDetails, 256),
                              ("meshes", self.nMeshes, 4096)):
            if not (0 <= n <= lim):
                raise ContractError("implausible %s count %d -- header not understood"
                                    % (label, n))

        self.tableStart = o
        return o

    # -- offset chain ------------------------------------------------------

    def layout(self):
        v = self.version
        self.nodeRec, self.nodeFmt = (20, "<5i") if v <= 7 else (10, "<5h")
        self.subRec, self.subFmt = (12, "<3i") if v <= 7 else (6, "<3h")
        self.kfRec, self.kfFmt = (12, "<fII") if v <= 7 else (8, "<fHH")
        self.xfRec = 40 if v < 7 else (32 if v == 7 else 20)
        self.objRec = 72 if v <= 7 else 28
        self.trnRec = 60 if v < 7 else (52 if v == 7 else 40)
        self.seqRec = 32
        self.detRec = 8
        self.ftRec = 8

        o = self.tableStart
        self.nodeOff = o
        self.seqOff = self.nodeOff + self.nNodes * self.nodeRec
        self.ssOff = self.seqOff + self.nSeq * self.seqRec
        self.kfOff = self.ssOff + self.nSubSeq * self.subRec
        self.xfOff = self.kfOff + self.nKeyframes * self.kfRec
        self.nameOff = self.xfOff + self.nTransforms * self.xfRec
        self.objOff = self.nameOff + self.nNames * _NAME_LEN
        self.detOff = self.objOff + self.nObjects * self.objRec
        self.trnOff = self.detOff + self.nDetails * self.detRec
        self.ftOff = self.trnOff + (self.nTransitions * self.trnRec if self.version >= 2 else 0)
        self.tailOff = self.ftOff + (self.nFrameTriggers * self.ftRec if self.version >= 4 else 0)

        if self.tailOff > len(self.data):
            raise ContractError(
                "computed table chain runs past EOF (%d > %d) -- layout not understood"
                % (self.tailOff, len(self.data)))

        # fnDefaultMaterials (v>=5) then fAlwaysNode (v>=6), ts_shape.cpp:1221-1233.
        o = self.tailOff
        self.nDefaultMaterials = struct.unpack_from("<i", self.data, o)[0]
        o += 4
        self.alwaysNode = -1
        if self.version >= 6:
            self.alwaysNode = struct.unpack_from("<i", self.data, o)[0]
            o += 4
        self.meshBlockOff = o

    # -- tables ------------------------------------------------------------

    def readNames(self):
        d = self.data
        self.names = [_cstr(d[self.nameOff + _NAME_LEN * k:self.nameOff + _NAME_LEN * (k + 1)])
                      for k in range(self.nNames)]

    def _name(self, idx, what):
        if not (0 <= idx < self.nNames) or not _namelike(self.names[idx]):
            raise ContractError("%s name index %d does not resolve to a name -- "
                                "the offset chain is wrong for this file" % (what, idx))
        return self.names[idx]

    def readNodes(self):
        d = self.data
        out = []
        for k in range(self.nNodes):
            nm, par, nss, fss, dt = struct.unpack_from(self.nodeFmt, d,
                                                       self.nodeOff + k * self.nodeRec)
            name = self._name(nm, "node %d" % k)
            if k == 0 and par != -1:
                raise ContractError("node 0 parent is %d, not -1" % par)
            # ★A PARENT OF -1 IS LEGAL ON ANY NODE, not just node 0.★  The engine's own
            # invariant is `fParent == -1 || (fParent >= 0 && fParent < n)`
            # (ts_shapeInst.cpp:1878-1880) -- a .dts may carry SEVERAL roots, and 40
            # shapes in the stock corpus do.  Requiring a lower index unconditionally
            # rejected every one of them.
            if k and par != -1 and not (0 <= par < k):
                raise ContractError("node %d parent %d is neither -1 nor a lower index "
                                    "(ShapeInstance::init asserts this)" % (k, par))
            if not (0 <= nss <= self.nSubSeq and 0 <= fss <= self.nSubSeq
                    and fss + nss <= self.nSubSeq):
                raise ContractError("node %d subsequence range %d+%d out of %d"
                                    % (k, fss, nss, self.nSubSeq))
            out.append({"index": k, "name": name, "nameIndex": nm, "parent": par,
                        "nSubSequences": nss, "firstSubSequence": fss,
                        "defaultTransform": dt})
        self.nodes = out

    def readSubSequences(self):
        d = self.data
        out = []
        for k in range(self.nSubSeq):
            si, nk, fk = struct.unpack_from(self.subFmt, d, self.ssOff + k * self.subRec)
            if not (0 <= si < self.nSeq):
                raise ContractError("subsequence %d names sequence %d of %d -- the "
                                    "offset chain is wrong for this file"
                                    % (k, si, self.nSeq))
            if not (0 <= nk <= self.nKeyframes and 0 <= fk <= self.nKeyframes
                    and fk + nk <= self.nKeyframes):
                # ★An Int16 keyframe index WRAPS above 32,767, and the engine reads the
                # wrapped value exactly as we do.★  Wookie.dts has 33,050 keyframes, so
                # its subsequence 20 reads firstKeyframe -32,748.  That shape is genuinely
                # unrepresentable in the v8 format it claims -- but recording the defect
                # lets a gate say so by name, where raising here only said "unreadable".
                self.defects.append({
                    "code": DEFECT_INDEX_WRAP, "subSequence": k, "sequence": si,
                    "firstKeyframe": fk, "nKeyframes": nk,
                    "detail": "subsequence %d indexes keys %d+%d of %d -- the Int16 "
                              "fFirstKeyframe has wrapped; this shape cannot be "
                              "represented in TS::Shape v8" % (k, fk, nk, self.nKeyframes)})
            out.append({"index": k, "sequenceIndex": si, "nKeyframes": nk,
                        "firstKeyframe": fk})
        self.subSequences = out

    def readKeyframes(self):
        d = self.data
        masks = _KEY_MASKS_V7 if self.version <= 7 else _KEY_MASKS_V8
        mVis, mCV, mCM, mCF, mMat = masks
        out = []
        nanKeys = 0
        for k in range(self.nKeyframes):
            pos, kv, mi = struct.unpack_from(self.kfFmt, d, self.kfOff + k * self.kfRec)
            if not _finite(pos):
                # Recorded, not raised -- see the DEFECT_ note above.  The value is kept
                # as-is so a consumer sees exactly what the engine sees.
                nanKeys += 1
                if nanKeys <= 4:
                    self.defects.append({
                        "code": DEFECT_NAN_KEY, "keyframe": k,
                        "detail": "keyframe %d position is not finite (the exporter "
                                  "divided by a zero sequence duration)" % k})
            elif not (0.0 <= pos <= 1.0):
                self.defects.append({
                    "code": DEFECT_KEY_OUT_OF_RANGE, "keyframe": k, "pos": pos,
                    "detail": "keyframe %d position %r is outside 0..1 "
                              "(ts_shape.cpp:136 asserts this range)" % (k, pos)})
            flags = ((CEL_VISIBLE if mi & mVis else 0)
                     | (CEL_CARES_VIS if mi & mCV else 0)
                     | (CEL_CARES_MAT if mi & mCM else 0)
                     | (CEL_CARES_FRAME if mi & mCF else 0))
            out.append({"index": k, "pos": pos, "flags": flags,
                        "keyValue": int(kv), "matIndex": int(mi & mMat)})
        if nanKeys > 4:
            self.defects.append({
                "code": DEFECT_NAN_KEY, "keyframe": -1,
                "detail": "%d keyframe positions in total are not finite" % nanKeys})
        self.keyframes = out

    def readSequences(self):
        d = self.data
        out = []
        for s in range(self.nSeq):
            (nm, cyc, dur, prio, fft, nft, nifl,
             fifl) = struct.unpack_from("<iifiiiii", d, self.seqOff + s * self.seqRec)
            name = self._name(nm, "sequence %d" % s)
            if not _finite(dur):
                raise ContractError("sequence %d '%s' duration is not finite" % (s, name))
            if dur <= 0.0:
                # Real shipped data, not corruption -- stock shotgun.DTS/repairgun.DTS
                # carry `reload` with fCyclic=1, fDuration=0, and the engine answers
                # position 0 for them (ts_shapeInst.cpp:1394-1402).  It is still a thing
                # a converter must handle deliberately rather than divide by.
                self.defects.append({
                    "code": DEFECT_ZERO_DURATION, "sequence": s, "name": name,
                    "cyclic": int(cyc),
                    "detail": "sequence %d '%s' has duration %g -- a single-pose "
                              "sequence; every time/duration ratio would be a divide "
                              "by zero" % (s, name, dur)})
            # ★A ZERO COUNT MAKES THE 'first' FIELD MEANINGLESS -- and stock relies on
            # that.★  chaingun.DTS carries fFirstIFLSubSequence = 2011637646 in all
            # three of its sequences: the Dynamix exporter never initialised the field,
            # so every sequence got the same stack garbage.  The engine is only safe
            # because fNumIFLSubSequences gates every read of it (ts_shape.cpp:457,
            # ts_shapeInst.cpp:1495 runs the loop count-first) -- and where that gate is
            # missing, garbage in these fields is precisely what walks AdvancePosition
            # into a wild SubSequence (see the crashfix note at ts_shapeInst.cpp:1369).
            # Validating `first` unconditionally would reject most of the stock corpus.
            if self.version >= 4 and nft:
                if not (0 <= nft <= self.nFrameTriggers and 0 <= fft <= self.nFrameTriggers
                        and fft + nft <= self.nFrameTriggers):
                    raise ContractError("sequence %d '%s' frame-trigger range %d+%d out of %d"
                                        % (s, name, fft, nft, self.nFrameTriggers))
            elif self.version >= 4:
                fft = 0
            if nifl:
                if not (0 <= nifl <= self.nSubSeq and 0 <= fifl <= self.nSubSeq
                        and fifl + nifl <= self.nSubSeq):
                    raise ContractError("sequence %d '%s' IFL subsequence range %d+%d out of %d"
                                        % (s, name, fifl, nifl, self.nSubSeq))
            else:
                fifl = 0
            out.append({"index": s, "name": name, "nameIndex": nm,
                        "cyclic": int(cyc), "duration": dur, "priority": int(prio),
                        "firstFrameTrigger": fft, "nFrameTriggers": nft,
                        "nIFLSubSequences": nifl, "firstIFLSubSequence": fifl})
        self.sequences = out

    def readObjects(self):
        d = self.data
        seqNames = [s["name"] for s in self.sequences]
        out = []
        for k in range(self.nObjects):
            b = self.objOff + k * self.objRec
            nm, flags = struct.unpack_from("<hh", d, b)
            meshIndex = struct.unpack_from("<i", d, b + 4)[0]
            if self.version <= 7:
                nodeIndex = struct.unpack_from("<i", d, b + 8)[0]
                offset = list(struct.unpack_from("<3f", d, b + 52))
                nss, fss = struct.unpack_from("<ii", d, b + 64)
            else:
                nodeIndex = struct.unpack_from("<h", d, b + 8)[0]
                offset = list(struct.unpack_from("<3f", d, b + 12))
                nss, fss = struct.unpack_from("<hh", d, b + 24)
            name = self._name(nm, "object %d" % k)
            if not (0 <= nss <= self.nSubSeq and 0 <= fss <= self.nSubSeq
                    and fss + nss <= self.nSubSeq):
                raise ContractError("object %d '%s' subsequence range %d+%d out of %d"
                                    % (k, name, fss, nss, self.nSubSeq))
            if not (-1 <= nodeIndex < self.nNodes):
                raise ContractError("object %d '%s' node index %d out of %d"
                                    % (k, name, nodeIndex, self.nNodes))
            if not (0 <= meshIndex < self.nMeshes):
                raise ContractError("object %d '%s' mesh index %d out of %d"
                                    % (k, name, meshIndex, self.nMeshes))

            # Cel tracks: object subsequences carry visibility/material/frame keys
            # rather than transforms (ts_shape.h:127-163).
            cel = {}
            for s in range(fss, fss + nss):
                ss = self.subSequences[s]
                keys = [self.keyframes[j] for j in
                        range(ss["firstKeyframe"], ss["firstKeyframe"] + ss["nKeyframes"])]
                if not keys:
                    continue
                keys = sorted(keys, key=lambda t: t["pos"])   # findCelKey bisects on pos
                cel[seqNames[ss["sequenceIndex"]]] = [
                    {"pos": t["pos"], "flags": t["flags"],
                     "keyValue": t["keyValue"], "matIndex": t["matIndex"]} for t in keys]

            out.append({"index": k, "name": name, "nameIndex": nm, "flags": int(flags),
                        "meshIndex": meshIndex, "nodeIndex": nodeIndex,
                        "objectOffset": offset, "nSubSequences": nss,
                        "firstSubSequence": fss, "celTracks": cel})
        self.objects = out

    def readDetails(self):
        d = self.data
        out = []
        for k in range(self.nDetails):
            root, size = struct.unpack_from("<if", d, self.detOff + k * self.detRec)
            if not _finite(size):
                raise ContractError("detail %d size is not finite" % k)
            if not (-1 <= root < self.nNodes):
                raise ContractError("detail %d root node %d out of %d"
                                    % (k, root, self.nNodes))
            out.append({"index": k, "rootNodeIndex": root, "size": size,
                        "rootNodeName": self.nodes[root]["name"] if 0 <= root < self.nNodes else None})
        self.details = out

    def readTransitions(self):
        d = self.data
        out = []
        if self.version < 2:
            self.transitions = out
            return
        for k in range(self.nTransitions):
            b = self.trnOff + k * self.trnRec
            ss, es = struct.unpack_from("<ii", d, b)
            sp, ep, dur = struct.unpack_from("<3f", d, b + 8)
            if not (0 <= ss < self.nSeq and 0 <= es < self.nSeq):
                raise ContractError("transition %d references sequence %d/%d of %d"
                                    % (k, ss, es, self.nSeq))
            if not _finite(sp, ep, dur):
                raise ContractError("transition %d has a non-finite position/duration" % k)
            # The trailing Transform is VERSION-SHAPED, and the loader only builds v8
            # shapes -- so normalise it here, exactly the way the engine does.
            #
            #   v8  Transform    Quat16(8) + Point3F(12)                = 20
            #   v7  V7Transform  Quat16(8) + Point3F(12) + Point3F(12)  = 32
            #   v6  V6Transform  QuatF(16) + Point3F(12) + Point3F(12)  = 40
            #
            # ★V7Transform::AssignToNew copies fRotate and fTranslate and DROPS fScale★
            # (ts_transform.h:176-180), and Shape::read applies exactly that on load,
            # warning only when the dropped scale is not unity (ts_shape.cpp:1303-1309).
            # So the leading 20 bytes of a v7 record ARE the v8 transform -- this is the
            # engine's own conversion, not a truncation I invented.
            #
            # Found because tr_talon (the only shape in the corpus with transitions, and
            # a v7 file) converted cleanly through every gate and would then have been
            # REFUSED by the loader for carrying a 64-nibble transform where it wants 40.
            raw = d[b + 20:b + self.trnRec]
            if self.version >= 8:
                v8 = raw[:20]
            elif self.version == 7:
                v8 = raw[:20]                      # Quat16 + Point3F, fScale dropped
                sc = struct.unpack_from("<3f", d, b + 20 + 20)
                if any(abs(v - 1.0) > 1e-4 for v in sc):
                    self.defects.append({
                        "code": DEFECT_NONUNIT_TRANSITION_SCALE, "transition": k,
                        "scale": list(sc),
                        "detail": "transition %d carries a non-unit transform scale "
                                  "(%.4f %.4f %.4f) which the v7->v8 upgrade DROPS; the "
                                  "engine itself only warns about this"
                                  % (k, sc[0], sc[1], sc[2])})
            else:
                # v6 stores a full QuatF; converting it means re-quantising to Quat16,
                # which is a real conversion rather than a slice.  No shape in the corpus
                # needs it, so refuse rather than write an untested encoder.
                raise UnsupportedSource(
                    "transition transforms in a v%d shape use V6Transform (QuatF), which "
                    "this reader does not convert to the v8 Quat16 form" % self.version)

            out.append({"index": k, "startSequence": ss, "endSequence": es,
                        "startPosition": sp, "endPosition": ep, "duration": dur,
                        "transformRaw": v8.hex()})
        self.transitions = out

    def readFrameTriggers(self):
        d = self.data
        out = []
        if self.version < 4:
            self.frameTriggers = out
            return
        for k in range(self.nFrameTriggers):
            pos, val = struct.unpack_from("<fi", d, self.ftOff + k * self.ftRec)
            if not _finite(pos):
                raise ContractError("frame trigger %d position is not finite" % k)
            # isForward() is the SIGN of fPosition and getPosition() returns |pos|
            # (ts_shape.h:325-328) -- decode it here, once, so no consumer has to
            # re-derive the convention.
            out.append({"index": k, "position": abs(pos), "value": int(val),
                        "forward": bool(pos >= 0.0)})
        self.frameTriggers = out

    # -- materials ---------------------------------------------------------

    def readMaterials(self):
        """The one table no offset chain reaches -- anchored on its own Persistent tag.

        The MaterialList sits AFTER the mesh blocks, which are variable-size Persistent
        objects (ts_shape.cpp:1353-1392), so arithmetic cannot reach it.  The old
        reader scanned the whole file for any <nDetails,nMaterials> pair whose
        following bytes "looked like" materials, and required at least one NAMED map
        file to reject false positives -- which meant a legitimately untextured shape
        could not be read at all.

        None of that is necessary, because ★the list announces itself.★  Every
        Persistent block is framed:

            'PERS' | Int32 blockSize | Int16 nameLen | name (no NUL) | Int32 version | payload

        so `TS::MaterialList` is a literal, findable anchor, and the version that
        immediately follows it is the SAME version Material::read switches on
        (ts_material.cpp:276-295).  That turns the record size from a guess into a
        read:

            v1  48, map[16] @16     v2  48, map[32] @16
            v3  60, map[32] @16     v4+ 64, map[32] @16

        ★fnDefaultMaterials is NOT the material count.★  That was the assumption that
        replaced one guess with another: ammopad.DTS carries fnDefaultMaterials = 4
        against a list of 8 materials.  fnDefaultMaterials sizes ShapeInstance's
        identity remap array; the list length is the list's own field.
        """
        d = self.data
        TAG = b"TS::MaterialList"

        at = d.find(TAG, max(0, self.meshBlockOff - len(TAG) - 16))
        if at < 0:
            at = d.find(TAG)
        if at < 0:
            # A shape may legitimately ship no material list (Shape::read guards on the
            # `hasMaterials` Int32, ts_shape.cpp:1387-1392).
            self.materials = []
            self.materialListVersion = None
            return

        o = at + len(TAG)
        version = struct.unpack_from("<i", d, o)[0]
        nDet, nMat = struct.unpack_from("<ii", d, o + 4)
        base = o + 12

        if version == 1:
            rec, mapOff, mapLen = 48, 16, 16
        elif version == 2:
            rec, mapOff, mapLen = 48, 16, 32
        elif version == 3:
            rec, mapOff, mapLen = 60, 16, 32
        else:
            rec, mapOff, mapLen = 64, 16, 32

        if not (1 <= nDet <= 32) or not (0 <= nMat <= 4096):
            raise ContractError("material list at 0x%X declares %d detail(s) x %d "
                                "material(s) -- not believable" % (at, nDet, nMat))
        total = nDet * nMat
        if base + total * rec > len(d):
            raise ContractError("material list at 0x%X (v%d, %d records of %d bytes) "
                                "runs past EOF" % (at, version, total, rec))

        mats = self._decodeMaterials(d, base, total, rec, mapOff, mapLen)
        if mats is None:
            raise ContractError(
                "material list at 0x%X (v%d) did not decode as %d record(s) of %d "
                "bytes -- the layout for this version is not understood"
                % (at, version, total, rec))

        self.materials = mats
        self.materialListVersion = version
        self.materialListDetails = nDet
        self.materialListOffset = at
        self.materialRecordSize = rec

    def _decodeMaterials(self, d, base, total, rec, mapOff, mapLen):
        out = []
        for m in range(total):
            b = base + m * rec
            flags, alpha, index = struct.unpack_from("<ifi", d, b)
            r, g, bl, rgbFlags = struct.unpack_from("<4B", d, b + 12)
            fld = d[b + mapOff:b + mapOff + mapLen]
            if not self._plausibleName(fld, mapLen):
                return None
            if flags not in LEGAL_MATERIAL_FLAGS:
                return None
            if not _finite(alpha) or not (-0.001 <= alpha <= 1.001):
                return None

            surfType, elasticity, friction, useDefault = 0, 1.0, 1.0, 1
            if rec >= 60:
                surfType, elasticity, friction = struct.unpack_from("<iff", d, b + 48)
                if not (0 <= surfType <= MAX_SURFACE_TYPE):
                    return None
            if rec >= 64:
                # ★fUseDefaultProps is a UInt32 the engine tests as `!= 0`, NOT a 0/1
                # bool.★  It is declared UInt32 precisely because "read routines require
                # this to be the same under all compilers" (ts_material.h:186-189), and
                # the exporter leaves whatever was in the byte above the bool: real
                # shipped values include 319, 16777217 and 2147483649.  Rejecting
                # anything > 1 threw out 8 stock shapes whose materials are perfectly
                # fine -- eweb, blastgrn, energycell and friends.
                useDefault = struct.unpack_from("<I", d, b + 60)[0]

            # Elasticity/friction are only CONSUMED when fUseDefaultProps == 0
            # (Material::getElasticity/getFriction, ts_material.cpp:303-320); otherwise
            # the values come from DefaultMaterialProps and these fields are dead bytes.
            # energycell.DTS carries -0.0 / -8.8e-44 there with useDefault set -- inert.
            # So only validate them where the engine would actually read them, and even
            # then record rather than reject: it is a property of the asset, not proof
            # that we are looking at the wrong bytes.
            if rec >= 60 and useDefault == 0:
                if not _finite(elasticity, friction):
                    return None

            out.append({
                "index": m,
                "flags": int(flags),
                "mapFile": _cstr(fld),
                "alpha": alpha,
                "paletteIndex": int(index),
                "rgb": [int(r), int(g), int(bl)],
                "rgbFlags": int(rgbFlags),
                "type": int(surfType),
                "elasticity": elasticity,
                "friction": friction,
                "useDefaultProps": int(useDefault),
            })
        return out

    @staticmethod
    def _plausibleName(b, n):
        if len(b) < n:
            return False
        z = b.find(b"\x00")
        if z < 0:
            return False
        if z == 0:
            return all(c == 0 for c in b)
        return all(32 <= c <= 126 for c in b[:z]) and all(c == 0 for c in b[z:])


# ---------------------------------------------------------------------------

def _loadMeshGeometry(path):
    """Decoded per-mesh geometry, read through the repository's own `dts.py`.

    ★Without this the round-trip gate cannot see geometry AT ALL, and it said "exact"
    anyway.★  A controlled test moved one exported vertex by +10 units and the checker
    still returned PASS -- because every field it compared was metadata, and every
    negative control I had written mutated one of those same metadata fields.  Testing an
    instrument against its own coverage proves only that the coverage is self-consistent.

    `dts.py` is the Kaitai-generated, version-aware parser already in this repository; it
    reads the mesh Persistent blocks (which the offset chain above deliberately does not
    walk, because they are variable-size).  Using it here is also what the finalization
    audit asked for -- one parser for the records it already understands, rather than a
    third hand-rolled binary walk.

    Vertices are PackedVertex bytes plus a per-frame origin/scale:
        world = origin + packed * scale
    Faces carry a material index and three VertexIndexPairs (vertex + texture vertex).

    Returns None if dts.py cannot read the file, in which case geometry comparison is
    reported as UNAVAILABLE rather than silently skipped -- an absent check must never
    look like a passing one.
    """
    try:
        import os
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo not in sys.path:
            sys.path.insert(0, repo)
        import dts as _dts
    except Exception:
        return None

    try:
        doc = _dts.Dts.from_file(path)
    except Exception:
        return None

    out = []
    try:
        for mi, m in enumerate(doc.meshes):
            frames = []
            for fr in m.frames:
                o, s = fr.origin, fr.scale
                frames.append({"origin": [o.x, o.y, o.z], "scale": [s.x, s.y, s.z],
                               "firstVert": int(fr.first_vert)})
            verts = []
            boxMin = boxMax = None
            # Frame 0 is the rest pose and the only frame this loader represents.
            if frames:
                o = frames[0]["origin"]
                s = frames[0]["scale"]
                fv = frames[0]["firstVert"]
                n = int(m.num_vertices_per_frame) or int(m.num_vertices)
                for v in m.vertices[fv:fv + n]:
                    verts.append([o[0] + v.x * s[0],
                                  o[1] + v.y * s[1],
                                  o[2] + v.z * s[2]])

                # ★THE FIRST TWO VERTICES OF EVERY FRAME ARE THE AABB, NOT GEOMETRY.★
                #
                # CelAnimMesh::getBox unpacks fVerts[fv] and fVerts[fv+1] as the frame's
                # box min and max (ts_CelAnimMesh.cpp:1568-1574).  They are referenced by
                # no face, and the importer therefore never emits them as vertices.
                #
                # Discovered the hard way: the first geometry comparison reported
                # "2 source position(s) absent" on EVERY object of a known-good
                # conversion.  Two systematic missing vertices per mesh is the signature
                # of a convention, not of lost geometry -- and treating it as loss would
                # have made a correct converter look broken, which is how a gate gets
                # switched off instead of fixed.
                if len(verts) >= 2:
                    boxMin, boxMax = verts[0], verts[1]
                    verts = verts[2:]
            faces = []
            for f in m.faces:
                faces.append({"material": int(f.material),
                              "vertices": [int(p.vertex_index) for p in f.vip],
                              "textureVertices": [int(getattr(p, "texture_index", 0))
                                                  for p in f.vip]})
            out.append({
                "index": mi,
                "numVertices": int(m.num_vertices),
                "numVerticesPerFrame": int(m.num_vertices_per_frame),
                "numFaces": int(m.num_faces),
                "numTextureVertices": int(m.num_texture_vertices),
                "numFrames": int(m.num_frames),
                "radius": float(m.radius),
                "frames": frames,
                "vertices": verts,
                # Face vertex indices are into the ORIGINAL frame array, which still
                # includes the two AABB entries -- subtract vertexBase to index
                # `vertices` above.
                "vertexBase": 2 if boxMin is not None else 0,
                "boxMin": boxMin,
                "boxMax": boxMax,
                "faces": faces,
            })
    except Exception:
        return None
    return out


def load(path):
    """Read `path` and return an immutable Contract.

    Raises ContractError if the file cannot be read as a DTS shape, and
    UnsupportedSource if it is a DTS this reader deliberately refuses.  It never
    returns a partially-populated object: a shape that cannot be read exactly is
    an error, because every consumer of this module is deciding whether a
    conversion was LOSSLESS.
    """
    r = _Reader(path)
    r.readHeader()
    r.layout()
    r.readNames()
    r.readNodes()
    r.readSubSequences()
    r.readKeyframes()
    r.readSequences()
    r.readObjects()
    r.readDetails()
    r.readTransitions()
    r.readFrameTriggers()
    r.readMaterials()

    shape = {
        "radius": r.radius,
        "center": r.center,
        "boundsMin": r.boundsMin,
        "boundsMax": r.boundsMax,
        "hasExactBounds": r.boundsMin is not None,
    }

    return Contract(
        path=path,
        version=r.version,
        shape=shape,
        nodes=r.nodes,
        objects=r.objects,
        meshCount=r.nMeshes,
        details=r.details,
        sequences=r.sequences,
        subSequences=r.subSequences,
        keyframes=r.keyframes,
        transformCount=r.nTransforms,
        transitions=r.transitions,
        frameTriggers=r.frameTriggers,
        materials=r.materials,
        materialListVersion=getattr(r, "materialListVersion", None),
        defects=r.defects,
        meshes=_loadMeshGeometry(path),
        names=r.names,
        alwaysNode=r.alwaysNode,
        nDefaultMaterials=r.nDefaultMaterials,
    )


# ---------------------------------------------------------------------------

def materialMapFiles(path):
    """Back-compat shim for the retired `dts2glb.dts_material_maps`.

    That function located the material list by SIGNATURE -- scanning the whole file for
    any <nDetails,nMaterials> pair whose following bytes looked plausible, across four
    candidate record layouts, and requiring at least one NAMED map file so an untextured
    shape could not be read at all.  It is replaced by readMaterials(), which anchors on
    the `TS::MaterialList` Persistent tag and takes the record size from the block
    version.  Kept only so any external caller keeps working.
    """
    return [m["mapFile"] for m in load(path).materials]


def main(argv):
    import sys
    if len(argv) < 2:
        sys.stderr.write("usage: dts_contract.py <shape.dts> [--json]\n")
        return 2
    try:
        c = load(argv[1])
    except UnsupportedSource as e:
        sys.stderr.write("unsupported: %s\n" % e)
        return 3
    except ContractError as e:
        sys.stderr.write("error: %s\n" % e)
        return 2

    if "--json" in argv:
        sys.stdout.write(c.to_json() + "\n")
        return 0

    print("DTS v%d  %s" % (c.version, c.path))
    print("  %d node(s), %d object(s), %d mesh(es), %d material(s)"
          % (len(c.nodes), len(c.objects), c.meshCount, len(c.materials)))
    print("  radius %.10g  center (%.10g, %.10g, %.10g)"
          % (c.shape["radius"], c.shape["center"][0], c.shape["center"][1],
             c.shape["center"][2]))
    if c.shape["hasExactBounds"]:
        print("  bounds (%.6g %.6g %.6g)..(%.6g %.6g %.6g)"
              % tuple(c.shape["boundsMin"] + c.shape["boundsMax"]))
    print("  details: %s" % ", ".join("%s@%g" % (d["rootNodeName"], d["size"])
                                      for d in c.details))
    print("  sequences:")
    for s in c.sequences:
        print("    %2d %-24s cyclic=%d dur=%.10g prio=%d triggers=%d ifl=%d"
              % (s["index"], s["name"], s["cyclic"], s["duration"], s["priority"],
                 s["nFrameTriggers"], s["nIFLSubSequences"]))
    print("  materials:")
    for m in c.materials:
        print("    %2d flags=0x%X map='%s' rgb=(%d,%d,%d) type=%d elas=%g fric=%g def=%d"
              % (m["index"], m["flags"], m["mapFile"], m["rgb"][0], m["rgb"][1],
                 m["rgb"][2], m["type"], m["elasticity"], m["friction"],
                 m["useDefaultProps"]))
    cel = c.celTracks()
    if cel:
        print("  cel tracks on %d object(s):" % len(cel))
        for oi in sorted(cel):
            print("    obj %2d %-24s %s" % (oi, c.objects[oi]["name"],
                                            ", ".join("%s(%d keys)" % (k, len(v))
                                                      for k, v in sorted(cel[oi].items()))))
    if c.frameTriggers:
        print("  %d frame trigger(s)" % len(c.frameTriggers))
    if c.transitions:
        print("  %d transition(s)" % len(c.transitions))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
