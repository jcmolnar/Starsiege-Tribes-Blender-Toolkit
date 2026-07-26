#!/usr/bin/env python3
"""Gate: did the .dts's CEL tracks survive into the .glb?

    python tools/check_cel_tracks.py <shape.dts> <shape.glb>

★Why this gate exists.★  The model suite passed 21/21 while six of those shapes were
silently losing animation.  Every gate it had -- sequence order, wire compatibility,
material map files, keyframe caps -- was sound, and not one of them asked whether the
animation actually survived.  A DTS sequence can animate NODES (transforms) or OBJECTS
(visibility / material / mesh-frame cel keys), and only the node half has a glTF
representation; the object half rides as a `dtsCel` node extra.  A converter that
forgets to write it produces a file that passes everything and renders a destroyed
turret with its intact body still drawn over the wreckage.

So: read the cel tracks straight out of the .dts, read the extras out of the .glb, and
compare key for key.  An object the LOD collapse dropped has no node and is reported as
DROPPED, not failed -- that is legitimate.  An object that IS in the .glb but has no
extras, or whose keys differ, is a FAILURE.

Exit codes:  0 all cel tracks accounted for (including "the .dts has none")
             1 bad arguments / unreadable input
             2 cel tracks were LOST
"""
import json
import os
import struct
import sys


def dts_cel_tracks(path):
    """{objectName: {seqName: [(pos, flags, keyValue, matIndex)]}} straight from the .dts.

    Record sizes are the engine's own; see tools/dts2glb.py dts_cel_tracks for the
    citations.  ★The v7 and v8 keyframe flag masks DIFFER★ -- v7 keeps the four flags in
    the high bits of a 32-bit field (ts_shape.cpp:991-994), v8 in 0x8000..0x1000 of a
    16-bit one (ts_shape.h:157-163).  Reading v7 with the v8 masks yields all-zero flags,
    which reads as a valid track that happens to do nothing.
    """
    d = open(path, 'rb').read()
    i = d.find(b"TS::Shape")
    if i < 0:
        return None, None
    o = i + 10
    (ver, nNodes, nSeq, nSub, nKf, nXf, nNames,
     nObj, nDet, nMesh) = struct.unpack_from("<10i", d, o)
    if ver < 5:
        return None, None
    o += 40
    if ver >= 2:
        o += 4
    if ver >= 4:
        o += 4
    o += 4 + 12
    if ver > 7:
        o += 24

    node_rec = 20 if ver <= 7 else 10
    sub_rec = 12 if ver <= 7 else 6
    sub_fmt = "<3i" if ver <= 7 else "<3h"
    kf_rec = 12 if ver <= 7 else 8
    kf_fmt = "<fII" if ver <= 7 else "<fHH"
    xf_rec = 40 if ver < 7 else (32 if ver == 7 else 20)
    obj_rec = 72 if ver <= 7 else 28

    seq_off = o + nNodes * node_rec
    ss_off = seq_off + nSeq * 32
    kf_off = ss_off + nSub * sub_rec
    xf_off = kf_off + nKf * kf_rec
    nm_off = xf_off + nXf * xf_rec
    obj_off = nm_off + nNames * 24
    if obj_off + nObj * obj_rec > len(d):
        return None, None

    names = [d[nm_off + 24 * k:nm_off + 24 * k + 24].split(b'\x00')[0]
             .decode('latin1') for k in range(nNames)]
    subs = [struct.unpack_from(sub_fmt, d, ss_off + k * sub_rec) for k in range(nSub)]
    kfs = [struct.unpack_from(kf_fmt, d, kf_off + k * kf_rec) for k in range(nKf)]
    seqnames = []
    for s in range(nSeq):
        ni = struct.unpack_from("<i", d, seq_off + s * 32)[0]
        if not (0 <= ni < nNames):
            return None, None
        seqnames.append(names[ni])
    for (si, nk, fk) in subs:
        if not (0 <= si < nSeq and 0 <= nk <= nKf and 0 <= fk <= nKf and fk + nk <= nKf):
            return None, None

    if ver <= 7:
        M_VIS, M_CV, M_CM, M_CF, M_MAT = (0x80000000, 0x40000000,
                                          0x20000000, 0x10000000, 0x0fffffff)
    else:
        M_VIS, M_CV, M_CM, M_CF, M_MAT = 0x8000, 0x4000, 0x2000, 0x1000, 0x0fff

    out = {}
    for k in range(nObj):
        b = obj_off + k * obj_rec
        nmIdx = struct.unpack_from("<h", d, b)[0]
        if ver <= 7:
            nss, fss = struct.unpack_from("<ii", d, b + 64)
        else:
            nss, fss = struct.unpack_from("<hh", d, b + 24)
        if not (0 <= nmIdx < nNames):
            return None, None
        if not (0 <= nss < 4096 and 0 <= fss <= nSub and fss + nss <= nSub):
            return None, None
        if not nss:
            continue
        per_seq = {}
        for s in range(fss, fss + nss):
            si, nk, fk = subs[s]
            keys = []
            for j in range(fk, fk + nk):
                pos, kv, mi = kfs[j]
                flags = ((1 if mi & M_VIS else 0) | (2 if mi & M_CV else 0)
                         | (4 if mi & M_CM else 0) | (8 if mi & M_CF else 0))
                keys.append((round(pos, 4), flags, kv, mi & M_MAT))
            if keys:
                keys.sort(key=lambda t: t[0])
                per_seq[seqnames[si]] = keys
        if per_seq:
            out.setdefault(names[nmIdx], {}).update(per_seq)
    return out, seqnames


def glb_json(path):
    d = open(path, 'rb').read()
    if d[0:4] != b'glTF':
        return None
    off = 12
    while off + 8 <= len(d):
        clen, ct = struct.unpack_from('<I4s', d, off)
        off += 8
        if ct == b'JSON':
            return json.loads(d[off:off + clen].decode('utf-8'))
        off += clen
    return None


def parse_extra(s):
    out = {}
    for seg in s.split(';'):
        if '=' not in seg:
            continue
        name, keys = seg.split('=', 1)
        ks = []
        for k in keys.split('|'):
            f = k.split(',')
            if len(f) != 4:
                continue
            ks.append((round(float(f[0]), 4), int(f[1]), int(f[2]), int(f[3])))
        if ks:
            out[name] = ks
    return out


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 1
    dts, glb = argv[1], argv[2]
    if not os.path.isfile(dts) or not os.path.isfile(glb):
        print("check_cel_tracks: missing input")
        return 1

    want, _seqs = dts_cel_tracks(dts)
    js = glb_json(glb)
    if js is None:
        print("check_cel_tracks: %s is not a GLB" % glb)
        return 1

    if not want:
        print("cel tracks: the .dts has none -- nothing to carry.  OK")
        return 0

    got, by_name = {}, {}
    for n in js.get('nodes', []):
        if n.get('name'):
            by_name[n['name']] = n
            ex = (n.get('extras') or {}).get('dtsCel')
            if ex:
                got[n['name']] = parse_extra(ex)

    dropped, missing, mismatched, ok = [], [], [], 0
    for obj, per_seq in sorted(want.items()):
        if obj not in by_name:
            dropped.append(obj)          # LOD collapse removed it -- legitimate
            continue
        have = got.get(obj)
        if not have:
            missing.append(obj)
            continue
        for seq, keys in sorted(per_seq.items()):
            hk = have.get(seq)
            if hk is None:
                mismatched.append('%s/%s absent' % (obj, seq))
            elif hk != keys:
                mismatched.append('%s/%s %s != %s' % (obj, seq, hk[:2], keys[:2]))
            else:
                ok += 1

    total = sum(len(v) for v in want.values())
    print("cel tracks: %d/%d track(s) carried, %d object(s) dropped by LOD collapse"
          % (ok, total, len(dropped)))
    if dropped:
        print("   dropped (no node in the GLB, legitimate): %s" % dropped[:8])
    if missing:
        print("!! LOST: %d object(s) are in the GLB but carry NO dtsCel extra: %s"
              % (len(missing), missing[:8]))
    if mismatched:
        print("!! MISMATCH: %d track(s) differ from the .dts: %s"
              % (len(mismatched), mismatched[:6]))
    if missing or mismatched:
        print("VERDICT: cel animation would be LOST in game -- destroyed objects keep "
              "their intact body drawn, doors do not open.")
        return 2
    print("VERDICT: every cel track present in the GLB survived intact.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
