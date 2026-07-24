r"""Port a Starsiege Herc (mech) into Tribes as a wearable PlayerData armor.

Starsiege and Tribes are both Darkstar, so a Herc .dts needs no geometry
conversion -- the Tribes engine reads its version-7 shape natively and upgrades
it in memory (Shape::read, engine\Ts3\code\ts_shape.cpp). Three things do need
doing, and this tool does them:

  1. TEXTURES.  Herc skins are Darkstar PBMP (palette indices, no colours) and
     the colours live in a Starsiege world palette (Terrain\*.Sim.vol ->
     temperate.d.ppl).  Tribes would colour them with ITS palette and get
     garbage, so we bake the Starsiege palette in and emit ordinary 8-bit
     Windows BMPs, which Tribes loads directly.

  2. SEQUENCE NAMES.  Tribes binds animation slots to sequence NAMES via
     PlayerData::animData (base\scripts.vol -> armordata.cs).  A Herc calls its
     sequences Seq00_walk / Seq01_run / Seq02_stand..., so they are renamed to
     the names Tribes looks for.  Names live in fixed 24-byte slots, so this is
     an in-place patch -- no offsets move.

  3. A DATABLOCK.  Emits a PlayerData .cs whose animData maps every engine
     animation slot to a sequence the Herc actually has (see WHY_PLAYERDATA).

WHY_PLAYERDATA: Tribes vehicles (FlierData/CarData) are rigid bodies and never
play sequences -- a Herc mounted as a vehicle would slide with frozen legs.
Tribes *players* pick their animation by projecting velocity onto each
sequence's measured root motion (Player::pickAnimation, player.cpp:608) and
scale playback to ground speed, which is exactly how a mech should walk.  The
Herc rig turns out to match a Tribes player rig: node 0 is "bounds", forward
locomotion is +Y root translation, and the idle has no root subsequence.

Usage:
    python tools/starsiege_to_tribes.py tr_talon --game "C:\Users\Joe\Documents\Starsiege" -o out\
    python tools/starsiege_to_tribes.py --list
"""
import argparse
import os
import struct
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(_TOOLS_DIR)
sys.path.insert(0, _ADDON_DIR)
sys.path.insert(0, _TOOLS_DIR)

from kaitaistruct import KaitaiStream, BytesIO      # noqa: E402
from dts import Dts                                  # noqa: E402
from dts_viewer import read_vol_index, parse_ppl, parse_pbmp   # noqa: E402


# ---------------------------------------------------------------- archives

class GameFiles:
    """Every .vol under the Starsiege folder, indexed once."""

    def __init__(self, gamedir):
        self.gamedir = gamedir
        self.vols = {}
        for root, _dirs, files in os.walk(gamedir):
            for fn in sorted(files):
                if fn.lower().endswith('.vol'):
                    p = os.path.join(root, fn)
                    try:
                        idx = read_vol_index(p)
                    except Exception:
                        continue
                    if idx:
                        self.vols[p] = idx

    def find(self, name):
        """Raw bytes of `name` from whichever archive holds it."""
        target = name.lower()
        for volpath, idx in self.vols.items():
            if target in idx:
                off, size = idx[target]
                with open(volpath, 'rb') as f:
                    f.seek(off)
                    return f.read(size), volpath
        return None, None

    def list_mechs(self):
        """Shapes that look like Hercs: many nodes, the 12-sequence rig."""
        out = []
        for volpath, idx in self.vols.items():
            if not volpath.lower().endswith('gameobjects.vol'):
                continue
            data = open(volpath, 'rb').read()
            for name, (off, size) in sorted(idx.items()):
                if not name.endswith('.dts'):
                    continue
                buf = data[off:off + size]
                try:
                    sh = Dts(KaitaiStream(BytesIO(buf))).shape.data.obj_data
                except Exception:
                    continue
                if sh.num_nodes >= 40 and sh.num_seq >= 4:
                    out.append((name, sh.num_nodes, sh.num_seq, len(sh.details)))
        return out


# ---------------------------------------------------------------- textures

def write_bmp8(path, width, height, indices, palette):
    """Write an 8-bit Windows BMP with an embedded palette.

    Tribes loads plain palettised BMPs, so baking the Starsiege palette in here
    frees the shape from needing a .ppl at all.
    """
    row_padded = (width + 3) & ~3
    pixel_bytes = row_padded * height
    # BMP rows run bottom-up; PBMP data is top-down.
    rows = []
    for y in range(height - 1, -1, -1):
        row = bytes(indices[y * width:(y + 1) * width])
        rows.append(row + b'\x00' * (row_padded - width))
    pixels = b''.join(rows)

    pal = bytearray()
    for i in range(256):
        r, g, b = palette[i] if i < len(palette) else (0, 0, 0)
        pal += bytes((b, g, r, 0))          # BMP palette is BGRA

    offbits = 14 + 40 + 1024
    fh = b'BM' + struct.pack('<IHHI', offbits + pixel_bytes, 0, 0, offbits)
    ih = struct.pack('<IiiHHIIiiII', 40, width, height, 1, 8, 0,
                     pixel_bytes, 2835, 2835, 256, 256)
    with open(path, 'wb') as f:
        f.write(fh + ih + bytes(pal) + pixels)


def convert_texture(name, games, ppl_tables, outdir):
    """Pull a Herc skin out of the archives and bake it to a Tribes BMP."""
    raw, src = games.find(name)
    if raw is None:
        return None, 'not found in any .vol'
    if raw[:4] != b'PBMP':
        # already an ordinary bitmap -- copy through untouched
        out = os.path.join(outdir, name)
        with open(out, 'wb') as f:
            f.write(raw)
        return out, 'copied (not PBMP)'
    try:
        w, h, indices, pidx, embedded = parse_pbmp(raw)
    except Exception as e:
        return None, 'PBMP parse failed: %s' % e
    palette = embedded
    if palette is None and ppl_tables:
        palette = ppl_tables.get(pidx) or ppl_tables.get(None)
    if palette is None:
        return None, 'no palette (PiDX=%s)' % pidx
    out = os.path.join(outdir, name)
    write_bmp8(out, w, h, indices, palette)
    return out, '%dx%d from %s' % (w, h, os.path.basename(src))


# ---------------------------------------------------------------- sequences

# Herc sequence  ->  the name Tribes' animData looks for.
# Tribes has no walk/fast-run slots, so Seq00_walk becomes "run" (a Herc's walk
# IS its run) and the genuine run/frun stay available under their own names for
# a faster armor variant.
SEQUENCE_RENAMES = {
    'Seq02_stand': 'root',
    'Seq00_walk':  'run',
    'Seq01_run':   'fastrun',
    'Seq08_frun':  'sprint',
    'Seq09_fall':  'fall',
    'Seq10_land':  'landing',
    'Seq04_squat': 'crouch root',
    'Seq07_cockpit': 'looks',
    'Seq07_cpit':  'looks',
}


def _names_offset(buf, sh, version):
    """Byte offset of the shape's 24-byte name table.

    Computed from the record sizes ahead of it, then verified by reading the
    names back and comparing with what the parser produced -- so a wrong guess
    fails loudly instead of corrupting the file.
    """
    if version != 7:
        raise ValueError('only version 7 (Starsiege) shapes supported here')
    p = 4 + 4                       # 'PERS' + size
    clen = struct.unpack_from('<H', buf, p)[0]
    p += 2 + ((clen + 1) & ~1) + 4  # classname + padding + version
    p += 11 * 4                     # the eleven counts
    p += 4                          # radius
    p += 12                         # center Point3F
    p += sh.num_nodes * 20          # Nodev7
    p += sh.num_seq * 32            # VectorSequence
    p += sh.num_subseq * 12         # Subsequencev7
    p += sh.num_keyframes * 12      # Keyframev7
    p += sh.num_transforms * 32     # Transformv7 (quat16 + 2x Point3F)
    return p


def rename_sequences(buf, renames, verbose=True):
    """Patch sequence names in place. Returns (new_bytes, [(old, new)])."""
    d = Dts(KaitaiStream(BytesIO(buf)))
    sh = d.shape.data.obj_data
    version = d.shape.data.version
    base = _names_offset(buf, sh, version)

    def slot(i):
        return buf[base + i * 24: base + i * 24 + 24]

    def clean(b):
        return b.split(b'\x00')[0].decode('latin-1', 'replace')

    # verify our arithmetic before writing anything
    for i, parsed in enumerate(sh.names):
        if slot(i) != parsed:
            raise ValueError(
                'name table offset check failed at %d (%r != %r)'
                % (i, clean(slot(i)), clean(parsed)))

    out = bytearray(buf)
    applied = []
    seq_name_indices = {q.name for q in sh.sequences}
    for i in seq_name_indices:
        old = clean(slot(i))
        new = renames.get(old)
        if not new:
            continue
        if len(new) > 23:
            raise ValueError('name too long for 24-byte slot: %r' % new)
        out[base + i * 24: base + i * 24 + 24] = \
            new.encode('latin-1').ljust(24, b'\x00')
        applied.append((old, new))
    if verbose:
        for old, new in applied:
            print('    %-16s -> %s' % (old, new))
    return bytes(out), applied


# ---------------------------------------------------------------- datablock

def emit_playerdata(shapename, dbname, sh_radius, outpath, renamed):
    """Write a PlayerData whose animData only ever names sequences we have.

    Slots Tribes expects but a Herc lacks (jet, deaths, signals, strafes) are
    pointed at existing sequences rather than left to resolve to -1, which the
    engine silently turns into sequence 0 -- that would make a mech play its
    walk cycle while dying.
    """
    have = {new for _old, new in renamed}
    idle = 'root' if 'root' in have else 'run'
    run = 'run' if 'run' in have else idle
    fall = 'fall' if 'fall' in have else idle
    land = 'landing' if 'landing' in have else idle
    crouch = 'crouch root' if 'crouch root' in have else idle

    # slot -> (sequence name, direction).  direction -1 plays it reversed,
    # which is how Tribes gets "side right" out of "side left".
    anim = {
        0: (idle, 1), 1: (run, 1), 2: (run, -1), 3: (run, 1), 4: (run, -1),
        5: (idle, 1), 6: (run, 1),
        7: (crouch, 1), 8: (crouch, 1), 9: (crouch, -1),
        10: (crouch, 1), 11: (crouch, -1), 12: (crouch, 1), 13: (crouch, -1),
        14: (fall, 1), 15: (land, 1), 16: (land, 1),
        17: (fall, 1), 18: (land, 1), 19: (fall, 1),
        20: (idle, 1), 21: (idle, 1), 22: (idle, 1), 23: (idle, 1),
        24: (idle, 1),
    }
    for s in range(25, 38):          # deaths
        anim[s] = (crouch, 1)
    for s in range(38, 51):          # signals, celebrations, taunts, poses
        anim[s] = (idle, 1)

    lines = []
    lines.append('//' + '-' * 74)
    lines.append('// %s -- Starsiege Herc as a Tribes armor.' % dbname)
    lines.append('//')
    lines.append('// Generated by tools/starsiege_to_tribes.py.')
    lines.append('// A Herc walks because Tribes players pick their animation from measured')
    lines.append('// root motion; vehicles never animate. See the tool docstring.')
    lines.append('//')
    lines.append('// !! NOT YET SAFE TO WEAR -- the shape still needs Tribes player utility')
    lines.append('// !! nodes. Player::initResources (program/code/player.cpp) looks up')
    lines.append('// !!   "dummyalways root"  -> getTransform(rnode)   [player.cpp:367]')
    lines.append('// !!   "dummy hand" / "dummy unused" / "dummy midback" / "dummy lowback"')
    lines.append('// !!                      -> getNode(mountNode[i])  [player.cpp:493]')
    lines.append('// !! and BOTH accessors are unbounded --')
    lines.append('// !!   getTransform(i) { return fTransformList[i]; }   (ts_shapeInst.h:670)')
    lines.append('// !!   getNode(i)      { return fNodeInstanceList[i]; } (ts_shapeInst.h:641)')
    lines.append('// !! so a missing node yields index -1 and reads out of bounds.')
    lines.append('// !! Also wanted (safe if absent): "lowerback" for view pitch,')
    lines.append('// !! "dummy eye", "dummyalways chasecam".')
    lines.append('//')
    lines.append('// Deploy %s.dts + its .bmp skins into base\\, then:' % shapename)
    lines.append('//     exec("%s.cs");' % dbname.lower())
    lines.append('//' + '-' * 74)
    lines.append('')
    lines.append('PlayerData %s' % dbname)
    lines.append('{')
    lines.append('   className = "Armor";')
    lines.append('   shapeFile = "%s";' % shapename)
    lines.append('   //  no damage skins / flame / shield shapes: those are Tribes-player')
    lines.append('   //  assets and a Herc has none. Leaving them unset is safe.')
    lines.append('   validateShape = false;')
    lines.append('')
    lines.append('   visibleToSensor = true;')
    lines.append('   mapFilter = 1;')
    lines.append('   mapIcon = "M_player";')
    lines.append('   canCrouch = true;')
    lines.append('')
    lines.append('   // Scaled up from a Tribes trooper: the shape is ~%.1fm in radius.' % sh_radius)
    lines.append('   maxDamage = 4.0;')
    lines.append('   maxForwardSpeed = 6;')
    lines.append('   maxBackwardSpeed = 4;')
    lines.append('   maxSideSpeed = 4;')
    lines.append('   groundForce = 40 * 30.0;')
    lines.append('   mass = 60.0;')
    lines.append('   groundTraction = 3.0;')
    lines.append('   maxEnergy = 100;')
    lines.append('   drag = 1.0;')
    lines.append('   density = 1.2;')
    lines.append('')
    lines.append('   minDamageSpeed = 40;')
    lines.append('   damageScale = 0.002;')
    lines.append('   jumpImpulse = 0;      // Hercs do not jump')
    lines.append('   jumpSurfaceMinDot = 0.2;')
    lines.append('   minJetEnergy = 1;')
    lines.append('   jetForce = 0;         // and do not jet')
    lines.append('   jetEnergyDrain = 0;')
    lines.append('')
    lines.append('   // animation name, sound, direction, firstPerson, chaseCam,')
    lines.append('   // thirdPerson, signalThread, priority')
    for s in sorted(anim):
        nm, d = anim[s]
        lines.append('   animData[%d] = { "%s", none, %d, true, true, true, false, %d };'
                     % (s, nm, d, 0 if s == 0 else 3))
    lines.append('};')
    lines.append('')
    with open(outpath, 'w') as f:
        f.write('\n'.join(lines))
    return outpath


# ---------------------------------------------------------------- driver

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mech', nargs='?', help='shape base name, e.g. tr_talon')
    ap.add_argument('--game', default=r'C:\Users\Joe\Documents\Starsiege',
                    help='Starsiege install folder')
    ap.add_argument('--ppl', default='temperate.d.ppl',
                    help='world palette to bake into the skins')
    ap.add_argument('-o', '--outdir', default='herc_out')
    ap.add_argument('--datablock', default=None,
                    help='PlayerData name (default: derived from the shape)')
    ap.add_argument('--list', action='store_true',
                    help='list Herc-like shapes and exit')
    args = ap.parse_args()

    print('indexing %s ...' % args.game)
    games = GameFiles(args.game)
    print('  %d .vol archives' % len(games.vols))

    if args.list:
        print('\nHerc-like shapes:')
        for name, nn, ns, nd in games.list_mechs():
            print('   %-18s nodes=%-4d seqs=%-3d LODs=%d' % (name, nn, ns, nd))
        return 0

    if not args.mech:
        ap.error('give a mech name, or --list')

    shape_file = args.mech if args.mech.lower().endswith('.dts') else args.mech + '.dts'
    raw, src = games.find(shape_file)
    if raw is None:
        print('ERROR: %s not found. Try --list.' % shape_file)
        return 1
    shapename = os.path.splitext(os.path.basename(shape_file))[0].lower()
    dbname = args.datablock or ('Herc' + shapename.replace('_', '').capitalize())

    os.makedirs(args.outdir, exist_ok=True)
    print('\n%s  (%d bytes, from %s)' % (shape_file, len(raw), os.path.basename(src)))

    d = Dts(KaitaiStream(BytesIO(raw)))
    sh = d.shape.data.obj_data
    print('  version %d, %d nodes, %d sequences, %d meshes, radius %.2f'
          % (d.shape.data.version, sh.num_nodes, sh.num_seq, sh.num_meshes, sh.radius))

    # --- palette -------------------------------------------------------
    ppl_tables = None
    ppl_raw, ppl_src = games.find(args.ppl)
    if ppl_raw:
        ppl_tables = parse_ppl(ppl_raw)
        print('  palette %s from %s (%d tables)'
              % (args.ppl, os.path.basename(ppl_src), len(ppl_tables)))
    else:
        print('  WARNING: palette %s not found; PBMP skins cannot be coloured' % args.ppl)

    # --- textures ------------------------------------------------------
    print('\n  textures:')
    wanted = []
    for m in d.materials.params:
        mf = getattr(m, 'map_file', b'').split(b'\x00')[0].decode('latin-1', 'replace')
        if mf and mf not in wanted:
            wanted.append(mf)
    if not wanted:
        print('    (none -- all materials are flat colours)')
    for mf in wanted:
        out, note = convert_texture(mf, games, ppl_tables, args.outdir)
        print('    %-18s %s' % (mf, note if out else 'SKIPPED: ' + note))

    # --- sequences -----------------------------------------------------
    print('\n  sequence renames:')
    patched, applied = rename_sequences(raw, SEQUENCE_RENAMES)
    if not applied:
        print('    (none matched -- unusual sequence naming?)')

    dts_out = os.path.join(args.outdir, shapename + '.dts')
    with open(dts_out, 'wb') as f:
        f.write(patched)
    print('\n  wrote %s' % dts_out)

    # re-parse the patched file so a corrupt write cannot pass silently
    chk = Dts(KaitaiStream(BytesIO(patched))).shape.data.obj_data
    names = [n.split(b'\x00')[0].decode('latin-1', 'replace') for n in chk.names]
    print('  verify: %d meshes, sequences now = %s'
          % (len(Dts(KaitaiStream(BytesIO(patched))).meshes),
             [names[q.name] for q in chk.sequences]))

    cs_out = os.path.join(args.outdir, dbname.lower() + '.cs')
    emit_playerdata(shapename, dbname, sh.radius, cs_out, applied)
    print('  wrote %s  (PlayerData %s)' % (cs_out, dbname))

    print('\nnext: copy %s\\* into your Tribes base\\ folder, then in the console:'
          % args.outdir)
    print('    exec("%s.cs");' % dbname.lower())
    return 0


if __name__ == '__main__':
    sys.exit(main())
