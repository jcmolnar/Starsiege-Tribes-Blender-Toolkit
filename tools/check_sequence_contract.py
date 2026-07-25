#!/usr/bin/env python3
"""check_sequence_contract.py -- does a converted .glb still honour the WIRE contract?

Usage:
  python tools/check_sequence_contract.py <source.dts> <converted.glb>
  python tools/check_sequence_contract.py --scan <dts-dir>        (contract RISK only)

★Why this exists.★  For a CHARACTER the wire carries a SLOT, not a sequence index
(playerUpdate.cpp:1269 writes 6 bits of `serverAnimation`, and animData binds slot->clip
by NAME), so re-pointing a player's clips is wire-safe.  Script threads are different:
they carry a 4-bit SEQUENCE INDEX (ThreadSequenceBits = 4, MaxSequenceIndex = 16,
shapebase.h:21-29, packed at shapeBase.cpp:1337-1382).  So for anything SCRIPT drives --
stations, turrets, sensors, mines, static shapes -- ★sequence indices 0..15 are a wire
contract and no client code can repair a mis-ordered asset.★

The conversion pipeline can silently break exactly that:

  * Blender merges NLA tracks that share a NAME, and the glTF exporter emits one
    animation per merged track.  A .dts with two sequences called the same thing
    therefore comes out with ONE animation -- and EVERY INDEX AFTER IT SHIFTS DOWN.
  * ts_gltf assigns TS sequence indices in glTF animation order (buildAnimations loops
    animations 0..n and pushes a Sequence per iteration), so glTF animation index IS
    the sequence index the engine will use.

Nothing in the engine can detect this at runtime: a station would simply play the wrong
animation for a wire value it received correctly.

Checks performed:
  1. index-for-index name equality over the contracted range 0..15;
  2. sequence COUNT, and any duplicate names in the source (the collapse hazard);
  3. presence of the load-bearing names script looks up by string --
     "power", "activate", "use", "deploy", "root" -- reported, not enforced, since
     which ones matter depends on the object class.
"""

import glob
import json
import os
import struct
import sys

MAX_SEQUENCE_INDEX = 16          # shapebase.h:21-29
LOAD_BEARING = ("power", "activate", "use", "deploy", "root")


def log(m):
    print("[SEQCHK] {}".format(m), flush=True)


# ---------------------------------------------------------------- .dts sequences
def dts_sequences(path):
    """Sequence names IN ORDER, by the deterministic offset walk (same record sizes
    and read order as dts2glb.py -- see ts_shape.cpp:1122-1142)."""
    raw = open(path, 'rb').read()
    i = raw.find(b"TS::Shape")
    if i < 0:
        return None, "no TS::Shape tag"
    o = i + 10
    (ver, nNodes, nSeq, nSubSeq, nKF, nXF, nNames, nObj, nDet,
     nMesh) = struct.unpack_from("<10i", raw, o)
    o += 40
    if ver >= 2:
        o += 4
    if ver >= 4:
        o += 4
    o += 16
    if ver > 7:
        o += 24
    if ver < 5:
        return None, "version %d below the v5 layout" % ver

    nrec = 20 if ver <= 7 else 10
    srec = 12 if ver <= 7 else 6
    kfrec = 12 if ver <= 7 else 8
    xfrec = 40 if ver < 7 else (32 if ver == 7 else 20)
    seq_off = o + nNodes * nrec
    name_off = (seq_off + nSeq * 32 + nSubSeq * srec + nKF * kfrec + nXF * xfrec)
    if name_off + nNames * 24 > len(raw):
        return None, "name table past EOF"
    names = [raw[name_off + 24 * k:name_off + 24 * k + 24].split(b"\x00")[0]
             .decode("latin1") for k in range(nNames)]
    out = []
    for s in range(nSeq):
        ni = struct.unpack_from("<i", raw, seq_off + s * 32)[0]
        out.append(names[ni] if 0 <= ni < nNames else "<oob %d>" % ni)
    return out, None


# ---------------------------------------------------------------- .glb animations
def glb_animations(path):
    data = open(path, 'rb').read()
    if data[0:4] != b'glTF':
        return None
    off = 12
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        if ctype == b'JSON':
            js = json.loads(data[off:off + clen].decode('utf-8'))
            return [a.get('name', '') for a in js.get('animations', [])]
        off += clen
    return None


# ---------------------------------------------------------------- checks
def check_pair(dts_path, glb_path):
    seqs, err = dts_sequences(dts_path)
    if seqs is None:
        log("!! %s: %s" % (os.path.basename(dts_path), err))
        return 2
    anims = glb_animations(glb_path)
    if anims is None:
        log("!! %s: not a GLB" % os.path.basename(glb_path))
        return 2

    name = os.path.basename(dts_path)
    log("%s: .dts %d sequence(s) -> .glb %d animation(s)" % (name, len(seqs), len(anims)))

    dupes = sorted(set(s for s in seqs if seqs.count(s) > 1))
    if dupes:
        log("   duplicate name(s) in the .dts: %s  ->  %d sequences collapse to %d"
            % (dupes, len(seqs), len(set(seqs))))

    bad = []
    n = min(len(seqs), len(anims), MAX_SEQUENCE_INDEX)
    for i in range(n):
        if seqs[i] != anims[i]:
            bad.append((i, seqs[i], anims[i]))
    if len(seqs) < MAX_SEQUENCE_INDEX or len(anims) < MAX_SEQUENCE_INDEX:
        # short tables are fine; only the overlap is contracted
        pass

    if bad:
        log("   *** CONTRACT VIOLATION in indices 0..%d ***" % (MAX_SEQUENCE_INDEX - 1))
        for i, a, b in bad[:MAX_SEQUENCE_INDEX]:
            log("     index %-2d  .dts '%s'   !=   .glb '%s'" % (i, a, b))
        log("     A script thread transmits this index in 4 bits (shapebase.h:21-29);")
        log("     the object will play the WRONG sequence for a correct wire value.")
    else:
        log("   indices 0..%d match" % (n - 1))

    present = [nm for nm in LOAD_BEARING if nm in anims]
    missing = [nm for nm in LOAD_BEARING if nm in seqs and nm not in anims]
    log("   load-bearing names present: %s" % (present if present else "none"))
    if missing:
        log("   *** LOST in conversion: %s *** -- script looks these up BY NAME" % missing)
        return 1
    return 1 if bad else 0


def scan(dirpath):
    """Risk survey over .dts only: which shapes COULD break if converted."""
    files = sorted(glob.glob(os.path.join(dirpath, "*.dts")) +
                   glob.glob(os.path.join(dirpath, "*.DTS")))
    seen = set()
    at_risk, clean, failed = [], 0, 0
    for f in files:
        k = os.path.basename(f).lower()
        if k in seen:
            continue
        seen.add(k)
        seqs, err = dts_sequences(f)
        if seqs is None:
            failed += 1
            continue
        dupes = sorted(set(s for s in seqs if seqs.count(s) > 1))
        # only the CONTRACTED range matters: a duplicate at index >= 16 shifts nothing
        # that is transmitted, because a script thread cannot address it.
        first_dupe = None
        for i, s in enumerate(seqs):
            if seqs.count(s) > 1 and s in dupes:
                first_dupe = i
                break
        if dupes and first_dupe is not None and first_dupe < MAX_SEQUENCE_INDEX:
            at_risk.append((os.path.basename(f), len(seqs), dupes, first_dupe))
        elif dupes:
            at_risk.append((os.path.basename(f), len(seqs), dupes, first_dupe))
        else:
            clean += 1
    log("scanned %d shape(s): %d clean, %d with duplicate sequence names, %d unparsed"
        % (len(seen), clean, len(at_risk), failed))
    inrange = [a for a in at_risk if a[3] is not None and a[3] < MAX_SEQUENCE_INDEX]
    log("*** %d have a duplicate INSIDE the contracted range 0..15 ***" % len(inrange))
    for nm, n, d, fi in inrange[:20]:
        log("   %-24s %2d seq, first duplicate at index %d: %s" % (nm, n, fi, d))
    if len(at_risk) > len(inrange):
        log("(%d more have duplicates only at index >= 16, which no script thread can "
            "address -- not a wire risk)" % (len(at_risk) - len(inrange)))
    return 0


def main():
    if '--scan' in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith('--')]
        return scan(rest[0])
    rest = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(rest) != 2:
        raise SystemExit(__doc__)
    return check_pair(rest[0], rest[1])


if __name__ == '__main__':
    sys.exit(main())
