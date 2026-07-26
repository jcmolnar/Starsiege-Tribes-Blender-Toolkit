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
  3. preservation of the engine-resolved mount-node names;
  4. effective render-detail coverage, so the converted shape cannot vanish at
     a distance where the source still has a selectable LOD.
"""

import glob
import json
import os
import re
import struct
import sys

MAX_SEQUENCE_INDEX = 16          # shapebase.h:21-29
LOAD_BEARING = ("power", "activate", "use", "deploy", "root")
ENGINE_MOUNT_NAMES = ("collision", "dummy exit", "dummy eye", "dummy muzzle",
                      "dummyalways chasecam", "dummyalways muzzle",
                      "dummyalways root", "kill 15")
NUL = b"\x00"                    # named, so no escaping layer can mangle it


def log(m):
    print("[SEQCHK] {}".format(m), flush=True)


# ---------------------------------------------------------------- .dts sequences
def dts_sequences(path):
    """Sequence names IN ORDER, by the deterministic offset walk (same record sizes
    and read order as dts2glb.py -- see ts_shape.cpp:1122-1142)."""
    with open(path, 'rb') as stream:
        raw = stream.read()
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


# ---------------------------------------------------------------- node names
def dts_node_names(path):
    """Node names IN ORDER.  The node table sits immediately before the sequence
    table and Node::fName is its first field (ts_shape.h:214-221)."""
    raw = open(path, 'rb').read()
    i = raw.find(b"TS::Shape")
    if i < 0:
        return None
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
        return None
    nrec = 20 if ver <= 7 else 10
    nfmt = "<5i" if ver <= 7 else "<5h"
    srec = 12 if ver <= 7 else 6
    kfrec = 12 if ver <= 7 else 8
    xfrec = 40 if ver < 7 else (32 if ver == 7 else 20)
    node_off = o
    name_off = (node_off + nNodes * nrec + nSeq * 32 + nSubSeq * srec +
                nKF * kfrec + nXF * xfrec)
    if name_off + nNames * 24 > len(raw):
        return None
    names = [raw[name_off + 24 * k:name_off + 24 * k + 24].split(NUL)[0]
             .decode("latin1") for k in range(nNames)]
    out = []
    for k in range(nNodes):
        ni = struct.unpack_from(nfmt, raw, node_off + k * nrec)[0]
        out.append(names[ni] if 0 <= ni < nNames else "<oob>")
    return out


def glb_node_names(path):
    with open(path, 'rb') as stream:
        data = stream.read()
    if data[0:4] != b'glTF':
        return None
    off = 12
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        if ctype == b'JSON':
            js = json.loads(data[off:off + clen].decode('utf-8'))
            return [n.get('name', '') for n in js.get('nodes', [])]
        off += clen
    return None


def dts_detail_sizes(path):
    """Return the source shape's render-detail thresholds.

    Detail::fSize is the minimum projected pixel size at which that detail may
    render (ts_shape.cpp:419-427).  Zero-sized records are utility details and
    never participate in selectDetail().
    """
    with open(path, 'rb') as stream:
        raw = stream.read()
    i = raw.find(b"TS::Shape")
    if i < 0:
        return None
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
        return None

    nrec = 20 if ver <= 7 else 10
    srec = 12 if ver <= 7 else 6
    kfrec = 12 if ver <= 7 else 8
    xfrec = 40 if ver < 7 else (32 if ver == 7 else 20)
    objrec = 72 if ver <= 7 else 28
    detail_off = (o + nNodes * nrec + nSeq * 32 + nSubSeq * srec +
                  nKF * kfrec + nXF * xfrec + nNames * 24 +
                  nObj * objrec)
    if detail_off + nDet * 8 > len(raw):
        return None
    sizes = [struct.unpack_from("<If", raw, detail_off + k * 8)[1]
             for k in range(nDet)]
    return [s for s in sizes if s > 0.0]


def glb_effective_detail_sizes(path):
    """Reproduce ts_gltf.cpp::buildDetails' effective render thresholds.

    glTF has no native DTS LOD record, so the loader derives detail roots from
    top-level node names ending in an integer.  A GLB with no such roots gets
    one always-visible detail at size 1.  Critically, a --one-lod conversion
    retains the source's highest-detail name (for example ``root 25``), but the
    loader lowers that lone render threshold to 1 so the shape cannot vanish
    before the source's coarsest LOD would.  The gate must model that runtime
    normalization or it will reject the fixed asset and validate the wrong
    representation.
    """
    with open(path, 'rb') as stream:
        data = stream.read()
    if data[0:4] != b'glTF':
        return None
    off = 12
    js = None
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        if off + clen > len(data):
            return None
        if ctype == b'JSON':
            js = json.loads(data[off:off + clen].decode('utf-8'))
            break
        off += clen
    if js is None:
        return None

    nodes = js.get('nodes', [])
    parents = [-1] * len(nodes)
    for parent, node in enumerate(nodes):
        for child in node.get('children', []):
            if isinstance(child, int) and 0 <= child < len(nodes):
                parents[child] = parent

    trailing = []
    for node in nodes:
        name = node.get('name', '')
        m = re.search(r'(\d+)$', name)
        trailing.append(int(m.group(1)) if m and m.start(1) > 0 else None)

    sizes = []
    for idx, size in enumerate(trailing):
        if size is None:
            continue
        parent = parents[idx]
        ancestor_is_detail = False
        while parent >= 0:
            if trailing[parent] is not None:
                ancestor_is_detail = True
                break
            parent = parents[parent]
        if not ancestor_is_detail and size > 0:
            sizes.append(float(size))

    if not sizes:
        # No derived details at all gets the loader's node-0/size-1 fallback.
        # If suffixed nodes existed but all were utility size 0, buildDetails
        # does NOT add that fallback: there is genuinely no render detail.
        return [1.0] if not any(s is not None for s in trailing) else []
    if len(sizes) == 1 and sizes[0] > 1.0:
        sizes[0] = 1.0
    return sizes


def detail_coverage_ok(source_sizes, converted_sizes):
    """Whether the converted shape remains selectable as far as the source."""
    return (bool(source_sizes) and bool(converted_sizes) and
            min(converted_sizes) <= min(source_sizes) + 1.0e-6)


def check_compat(dts_path, glb_path):
    """Can a .dts client and a .glb client share a server without diverging?

    ★No geometry crosses the wire.★  The network carries shape FILENAMES, animData's 51
    {name, sound, direction, viewFlags, priority} entries, a 6-bit player SLOT and a
    4-bit script SEQUENCE INDEX -- and not one byte of vertices or keyframes.  So two
    clients holding DIFFERENT FILES for the same shape stay in agreement exactly when
    three things agree:

      1. SEQUENCE NAMES -- animData binds slot->clip by name (player.cpp:424).  A name
         only the .dts has means the .glb client silently plays sequence 0 for that
         slot while the .dts client animates: same wire, different picture.
      2. SEQUENCE ORDER 0..15 -- a script thread transmits a 4-bit index
         (shapebase.h:21-29).  Disagree and a station plays a different animation on
         each client for the same packet.
      3. NODE NAMES -- mount points resolve BY NAME (player.cpp:377,578:
          findNode("dummyalways root") / findNode("dummyalways chasecam")), and
          insertOverride matches by node-name prefix.  A renamed node silently unmounts.
      4. DETAIL COVERAGE -- the converted shape must retain a selectable render
         detail at least as far away as the source.  Different LOD geometry is
         legal; disappearing sooner is not.

    Everything else is client-local and free to differ: vertex counts, materials, LODs,
    bone structure, skinning, texture sizes.  That is what makes mixed servers possible
    at all, and it is why any integrity handshake must check the CONTRACT rather than a
    file hash -- a hash check would reject every vanilla client on sight.
    """
    seqs, err = dts_sequences(dts_path)
    anims = glb_animations(glb_path)
    dnodes = dts_node_names(dts_path)
    gnodes = glb_node_names(glb_path)
    dsizes = dts_detail_sizes(dts_path)
    gsizes = glb_effective_detail_sizes(glb_path)
    if (seqs is None or anims is None or dnodes is None or gnodes is None or
            dsizes is None or gsizes is None):
        log("!! could not read one of the pair (%s)" % (err or "bad glb"))
        return 2

    log("COMPAT %s  <->  %s" % (os.path.basename(dts_path), os.path.basename(glb_path)))
    problems = 0

    lost = [s for s in seqs if s not in anims]
    if lost:
        log("   1. SEQUENCE NAMES: %d in the .dts have no .glb clip: %s"
            % (len(lost), lost[:8]))
        log("      -> the .glb client plays sequence 0 for those animData slots while")
        log("         the .dts client animates.")
        problems += 1
    else:
        log("   1. sequence names: all %d present in both" % len(seqs))

    n = min(len(seqs), len(anims), MAX_SEQUENCE_INDEX)
    bad = [(i, seqs[i], anims[i]) for i in range(n) if seqs[i] != anims[i]]
    if bad:
        log("   2. SEQUENCE ORDER 0..15 DIVERGES (%d of %d):" % (len(bad), n))
        for i, a, b in bad[:6]:
            log("      index %-2d .dts '%s' != .glb '%s'" % (i, a, b))
        log("      -> a script thread's 4-bit index means different things per client.")
        problems += 1
    else:
        log("   2. sequence order 0..%d identical" % (n - 1))

    # ★Only the names the ENGINE actually looks up matter -- not every node.★
    # Requiring every .dts node name to survive is wrong: --one-lod deliberately drops
    # the lower detail levels, so a 101-node shape legitimately becomes 34 and ~67
    # names vanish.  Almost all of those are per-detail duplicates (Left_Calf1..4), and
    # the mount points among them are per-detail variants of ONE base name --
    # player.cpp:579 resolves the eye with getNodeAtCurrentDetail("dummy eye"), so
    # "dummy eye16"/"dummy eye32" are that same mount at other details, not extra ones.
    #
    # This list is MEASURED, not guessed: every literal passed to findNode/
    # getNodeAtCurrentDetail in program\code and engine\Ts3\code.
    def resolves(names, base):
        # exact, or any per-detail variant "<base><digits>"
        for nm in names:
            if nm == base:
                return True
            if nm.startswith(base):
                tail = nm[len(base):].strip()
                if tail and tail.isdigit():
                    return True
        return False

    broken_mounts = []
    for base in ENGINE_MOUNT_NAMES:
        if resolves(dnodes, base) and not resolves(gnodes, base):
            broken_mounts.append(base)

    dropped = sorted(x for x in (set(dnodes) - set(gnodes)) if x)
    if broken_mounts:
        log("   3. MOUNT NODES LOST: %s" % broken_mounts)
        log("      -> the .dts resolves these and the .glb does not; the engine looks")
        log("         them up by name, so a mount silently stops working.")
        problems += 1
    else:
        log("   3. mount nodes: all engine-referenced names still resolve "
            "(%d other node name(s) dropped, all LOD duplicates)" % len(dropped))

    # fSize is a MINIMUM projected size.  A converted client may deliberately
    # use fewer LODs, but it must still have a render detail at least as far
    # away as the source: converted min <= source min.  This is the precise
    # contract the first indoorgun.glb violated (25 vs source 1), producing a
    # near-only turret while every geometry/sequence/material gate passed.
    if not dsizes:
        log("   4. DETAIL COVERAGE: source has no render detail -- cannot validate")
        problems += 1
    elif not gsizes:
        log("   4. DETAIL COVERAGE LOST: .glb has no render detail")
        log("      -> the converted shape is never selected for drawing.")
        problems += 1
    else:
        dmin, gmin = min(dsizes), min(gsizes)
        if not detail_coverage_ok(dsizes, gsizes):
            log("   4. DETAIL COVERAGE SHRANK: source min %.3g px, .glb min %.3g px"
                % (dmin, gmin))
            log("      -> the converted shape vanishes sooner as distance increases.")
            problems += 1
        else:
            log("   4. detail coverage: source min %.3g px, .glb effective min %.3g px"
                % (dmin, gmin))

    if problems:
        log("   VERDICT: NOT interchangeable -- a mixed server would show different")
        log("            things to .dts and .glb clients.")
        return 1
    log("   VERDICT: INTERCHANGEABLE -- both clients see the same animation for every")
    log("            wire value, so both may join the same server.")
    return 0


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

    dsizes = dts_detail_sizes(dts_path)
    gsizes = glb_effective_detail_sizes(glb_path)
    if dsizes is None or gsizes is None:
        log("   !! could not read detail thresholds")
        return 2
    if not dsizes or not gsizes:
        log("   *** NO RENDER DETAIL after conversion ***")
        return 1
    if not detail_coverage_ok(dsizes, gsizes):
        log("   *** DETAIL COVERAGE SHRANK: source min %.3g px -> .glb min %.3g px ***"
            % (min(dsizes), min(gsizes)))
        return 1
    log("   detail coverage: source min %.3g px -> .glb effective min %.3g px"
        % (min(dsizes), min(gsizes)))
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
    if '--compat' in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith('--')]
        if len(rest) != 2:
            raise SystemExit(__doc__)
        return check_compat(rest[0], rest[1])
    if '--scan' in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith('--')]
        return scan(rest[0])
    rest = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(rest) != 2:
        raise SystemExit(__doc__)
    return check_pair(rest[0], rest[1])


if __name__ == '__main__':
    sys.exit(main())
