"""
Headless DTS -> GLB converter that PRESERVES SEQUENCE NAMES.

Usage:
  blender.exe --background --factory-startup --python tools/dts2glb.py -- <in.dts> <out.glb>

Why this is not just "import, then export_scene.gltf":

  The DTS importer lays every sequence end-to-end on ONE timeline and keys each
  NODE into its own Action ("Left_LegAction", "camAction", ...).  Blender's glTF
  exporter defaults to export_animation_mode='ACTIONS', which emits one glTF
  animation per Action -- so a 12-sequence shape came out as 48 animations named
  after NODES, and every DTS sequence name was lost.

  That is not cosmetic.  ts_gltf.cpp:1056 takes Shape::Sequence::fName straight
  from the glTF animation name, and the engine looks sequences up BY NAME:

    * Player::initResources (player.cpp:388) does GetSequenceIndex("looks").
      A shape with no "looks" returns -1, and player.cpp:700 feeds that to
      Thread::SetSequence every frame -> &fSequences[-1] -> garbage IFL counts ->
      wild SubSequence -> access violation inside findCelKey.  The 1998 authors
      asserted against exactly this (player.cpp:394 AssertFatal "DOH!"), but
      AssertFatal is inert in a Release build.
    * animData binds slot -> clip BY NAME too, and a name the shape lacks is
      silently coerced to sequence 0 (player.cpp:412) -- so a mis-named rig looks
      "stuck in idle" and the runtime never reports it.

  The importer already records the sequence boundaries as timeline markers,
  "<seq>" and "End of <seq>" (main.py:543, :1406, :1594).  This script turns each
  marked range into an NLA track named after the sequence, on every animated
  object, and exports with export_animation_mode='NLA_TRACKS' -- Blender then
  merges same-named tracks across objects into ONE glTF animation per sequence.

  No new Actions are authored: an NLA strip can reference a SUB-RANGE of an
  existing action via action_frame_start/action_frame_end, which sidesteps
  Blender 5.0's layered/slotted Action authoring API entirely.
"""

import bpy
import sys
import os
import json
import struct
import traceback

ADDON_MODULE = "Tribes DTS Blender"   # folder name == module name
IMPORT_OP_ID = "dynamix.dts"          # main.py:427  ImportDTS.bl_idname

END_PREFIX = "End of "


def log(msg):
    print("[DTS2GLB] {}".format(msg), flush=True)


def parse_args():
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit("expected: ... --python dts2glb.py -- <in.dts> <out.glb>")
    rest = argv[argv.index("--") + 1:]
    if len(rest) < 2:
        raise SystemExit("expected 2 args after --: <in.dts> <out.glb>")
    return os.path.abspath(rest[0]), os.path.abspath(rest[1])


def enable_addon():
    try:
        bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
        log("addon_enable('{}') OK".format(ADDON_MODULE))
    except Exception as e:
        log("addon_enable failed ({}); trying direct import fallback".format(e))
        import importlib
        addon_dir = os.path.join(bpy.utils.resource_path('USER'), "scripts", "addons")
        if addon_dir not in sys.path:
            sys.path.insert(0, addon_dir)
        mod = importlib.import_module(ADDON_MODULE)
        mod.register()
        log("direct import + register() OK")
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
    except Exception:
        pass


def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False, confirm=False)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.actions,
                 bpy.data.armatures, bpy.data.images, bpy.data.cameras,
                 bpy.data.lights):
        for item in list(coll):
            coll.remove(item)


def action_fcurves(a):
    """Blender 5.0 removed Action.fcurves (slotted/layered actions).  Walk
    layers -> strips -> channelbags; fall back to the legacy flat list."""
    fcs = getattr(a, "fcurves", None)
    if fcs is not None:
        return list(fcs)
    out = []
    for layer in getattr(a, "layers", []):
        for strip in getattr(layer, "strips", []):
            for bag in getattr(strip, "channelbags", []):
                out.extend(bag.fcurves)
    return out


def sequence_ranges():
    """Pair the importer's "<seq>" / "End of <seq>" markers into ordered ranges.

    Returns [(name, start_frame, end_frame), ...] sorted by start frame.  A
    sequence whose end marker is missing runs to the next sequence's start (or
    the scene end), which is how the importer lays them out anyway."""
    marks = list(bpy.context.scene.timeline_markers)
    starts = {}
    ends = {}
    for m in marks:
        if m.name.startswith(END_PREFIX):
            ends[m.name[len(END_PREFIX):]] = m.frame
        else:
            starts[m.name] = m.frame

    ordered = sorted(starts.items(), key=lambda kv: kv[1])
    out = []
    for i, (name, fs) in enumerate(ordered):
        fe = ends.get(name)
        if fe is None:
            fe = ordered[i + 1][1] - 1 if i + 1 < len(ordered) else bpy.context.scene.frame_end
            log("  !! '{}' has no end marker -- running to frame {}".format(name, fe))
        if fe <= fs:
            fe = fs + 1          # NLA strips must span at least one frame
        out.append((name, int(fs), int(fe)))
    return out


def build_nla_tracks(ranges):
    """One NLA track per sequence, per animated object, each strip referencing
    that sequence's sub-range of the object's single imported action."""
    animated = 0
    strips = 0
    for obj in bpy.data.objects:
        ad = obj.animation_data
        if not ad or not ad.action:
            continue
        act = ad.action
        animated += 1

        for name, fs, fe in ranges:
            track = ad.nla_tracks.new()
            track.name = name
            try:
                strip = track.strips.new(name, int(fs), act)
            except Exception as e:
                log("  !! strip '{}' on '{}' failed: {}".format(name, obj.name, e))
                continue
            # Point the strip at this sequence's slice of the action.  Order
            # matters: widen the action range before pinning the strip range, or
            # Blender clamps one against the other.
            strip.frame_start = float(fs)
            strip.frame_end = float(fe)
            strip.action_frame_start = float(fs)
            strip.action_frame_end = float(fe)
            strip.extrapolation = 'HOLD'
            strip.blend_type = 'REPLACE'
            strip.use_animated_influence = False
            strips += 1

        # Drop the active action so only the tracks describe motion.  The strips
        # keep their own reference, so the action itself stays alive.
        ad.action = None

    log("NLA: {} sequence(s) x {} animated object(s) = {} strip(s)".format(
        len(ranges), animated, strips))
    return animated, strips


def flip_winding_to_gltf_ccw():
    """DTS front-faces CLOCKWISE (ts_CelAnimMesh.cpp:88-96 keeps a face whose
    screen-space cross is >= 0 in a Y-DOWN space); glTF requires COUNTER-clockwise
    (spec 3.7.4).  The DTS importer copies face index order verbatim -- it has no
    winding conversion at all -- so Blender ends up holding CW-outward polygons and
    the exported glTF violates the spec.  ts_gltf.cpp:753 correctly assumes CCW and
    swaps vertices 1/2, which makes that a SECOND reversal, and the model renders
    inside-out: near faces culled, far faces visible through the hole.  Joe's
    report of the first build was exactly that -- "the back is see-through and I
    see through it into the front, which is solid and textured".

    MEASURED on the pre-fix tr_talon.glb: 140 of 151 meshes had negative signed
    volume (CW-outward), total -81.67.  Flip here so the GLB we emit is
    spec-correct; the loader stays spec-correct for natively-authored glTF, which
    is the pipeline this whole effort is actually for.

    Safe to flip geometrically: the importer sets no custom split normals, so
    Blender recomputes them from winding and they come out pointing outward."""
    import bmesh
    n = 0
    for me in bpy.data.meshes:
        if not me.polygons:
            continue
        bm = bmesh.new()
        bm.from_mesh(me)
        for f in bm.faces:
            f.normal_flip()
        bm.to_mesh(me)
        bm.free()
        me.update()
        n += 1
    log("winding: flipped {} mesh(es) DTS-CW -> glTF-CCW".format(n))


def glb_signed_volume(js, data, bin_off):
    """Sum the signed volume of every indexed triangle mesh in the GLB.
    CCW-outward (spec-correct) is POSITIVE.  This is the same measurement the
    .dts winding validator uses, and it is the only way to catch a winding
    regression without eyes on the model."""
    bvs = js.get('bufferViews', [])
    accs = js.get('accessors', [])
    fmt_of = {5120: 'b', 5121: 'B', 5122: 'h', 5123: 'H', 5125: 'I', 5126: 'f'}
    ncomp_of = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}

    def read(ai):
        a = accs[ai]
        v = bvs[a['bufferView']]
        base = bin_off + v.get('byteOffset', 0) + a.get('byteOffset', 0)
        fmt = fmt_of[a['componentType']]
        nc = ncomp_of[a['type']]
        stride = v.get('byteStride') or struct.calcsize(fmt) * nc
        return [struct.unpack_from('<' + fmt * nc, data, base + i * stride)
                for i in range(a['count'])]

    total = 0.0
    pos_n = neg_n = 0
    for m in js.get('meshes', []):
        for pr in m.get('primitives', []):
            if 'POSITION' not in pr.get('attributes', {}) or 'indices' not in pr:
                continue
            P = read(pr['attributes']['POSITION'])
            I = [x[0] for x in read(pr['indices'])]
            vol = 0.0
            for k in range(0, len(I) - 2, 3):
                a, b, c = P[I[k]], P[I[k + 1]], P[I[k + 2]]
                vol += (a[0] * (b[1] * c[2] - b[2] * c[1])
                        - a[1] * (b[0] * c[2] - b[2] * c[0])
                        + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
            total += vol
            if vol < 0:
                neg_n += 1
            else:
                pos_n += 1
    return total, pos_n, neg_n


def glb_animation_names(path):
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 12 or data[0:4] != b'glTF':
        return None, None
    off = 12
    js = None
    bin_off = 0
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        chunk = data[off:off + clen]
        if ctype == b'JSON':
            js = json.loads(chunk.decode('utf-8'))
        elif ctype[0:3] == b'BIN':
            bin_off = off
        off += clen
    if js is None:
        return None, None, None, 0
    return [a.get('name') for a in js.get('animations', [])], js, data, bin_off


def main():
    src, dst = parse_args()
    log("=" * 70)
    log("IN : {}  ({} bytes)".format(src, os.path.getsize(src)))
    log("OUT: {}".format(dst))
    log("Blender {}".format(bpy.app.version_string))
    log("=" * 70)

    enable_addon()
    clear_scene()

    op = bpy.ops
    for part in IMPORT_OP_ID.split("."):
        op = getattr(op, part)
    res = op(filepath=src, import_scale=1.0, organize_by_lod=True)
    if 'FINISHED' not in res:
        log("!! IMPORT DID NOT FINISH")
        return 2

    meshes = [o for o in bpy.data.objects if o.type == 'MESH']
    tris = sum(max(0, len(p.vertices) - 2) for o in meshes for p in o.data.polygons)
    log("imported: {} objects ({} mesh, {} tris), {} actions".format(
        len(bpy.data.objects), len(meshes), tris, len(bpy.data.actions)))
    if not meshes:
        log("!! NO MESH OBJECTS IMPORTED - aborting")
        return 3

    # Cover every key in the export window: the importer keys along a running
    # frame counter and does not widen the scene range itself.
    last = bpy.context.scene.frame_end
    for a in bpy.data.actions:
        for fc in action_fcurves(a):
            for kp in fc.keyframe_points:
                last = max(last, int(kp.co[0]))
    if last > bpy.context.scene.frame_end:
        bpy.context.scene.frame_end = last

    ranges = sequence_ranges()
    log("DTS sequences from timeline markers: {}".format(
        [(n, s, e) for n, s, e in ranges]))
    if not ranges:
        log("!! no sequence markers found -- the GLB would carry node-named clips,")
        log("!! which the engine cannot bind (see this file's header). Aborting.")
        return 6

    build_nla_tracks(ranges)
    flip_winding_to_gltf_ccw()

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        bpy.ops.export_scene.gltf(
            filepath=dst,
            export_format='GLB',
            use_selection=False,
            export_apply=False,
            export_animations=True,
            export_animation_mode='NLA_TRACKS',
            export_yup=True,
            # Keep constant channels rather than let the size optimizer fold them.
            # A clip whose every channel is constant is exactly the kind this
            # pipeline must not lose: tr_talon's "looks" (seq 6) carries NO tracks
            # at all in the DTS, and a missing "looks" is what crashes the client
            # (player.cpp:388/700).  NB this flag did NOT recover the one clip that
            # still drops -- see the KNOWN GAP note in main() -- so it is here for
            # correctness of intent, not as a fix for that.
            export_optimize_animation_size=False,
        )
    except Exception:
        log("!! EXPORT RAISED:")
        traceback.print_exc()
        return 4

    if not os.path.exists(dst):
        log("!! export produced no file")
        return 5

    names, js, raw, bin_off = glb_animation_names(dst)
    log("---- GLB ----")
    log("  size       : {} bytes".format(os.path.getsize(dst)))
    log("  nodes      : {}".format(len(js.get('nodes', []))))
    log("  meshes     : {}".format(len(js.get('meshes', []))))
    log("  ANIMATIONS : {}  {}".format(len(names or []), names))

    vol, npos, nneg = glb_signed_volume(js, raw, bin_off)
    log("  winding    : {} CCW-outward / {} CW-outward, total signed volume {:.3f}".format(
        npos, nneg, vol))
    if vol < 0 or nneg > npos:
        log("!! FAIL: geometry is mostly CW-outward, which violates glTF spec 3.7.4.")
        log("!! ts_gltf.cpp:753 swaps 1/2 assuming CCW, so this renders INSIDE-OUT:")
        log("!! near faces culled, far faces visible through them.")
        return 8

    # The point of the whole script: sequence names must survive.
    want = set(n for n, _, _ in ranges)
    got = set(names or [])
    missing = sorted(want - got)
    extra = sorted(got - want)
    if missing:
        log("!! FAIL: {} sequence(s) lost in the round trip: {}".format(len(missing), missing))
        # KNOWN GAP (2026-07-25, tr_talon.dts): the LAST marked sequence,
        # 'Seq12_RLeg' (frames 175-177), does not survive, while the structurally
        # identical 'Seq11_LLeg' (172-174) does.  Cause NOT yet established --
        # disabling export_optimize_animation_size changed nothing (output was
        # byte-identical), so the constant-channel-optimizer theory is DISPROVED.
        # Harmless for tr_talon because the engine never selects that sequence
        # (it is not in the Player anim-name list), but do not assume that of the
        # next shape: this check failing is a hard stop, by design.
    if extra:
        log("   note: {} clip(s) not from a marker: {}".format(len(extra), extra))
    if missing:
        return 7

    log("PASS: all {} DTS sequence name(s) survived into the GLB".format(len(want)))
    return 0


rc = 0
try:
    rc = main()
except Exception:
    traceback.print_exc()
    rc = 1
log("EXIT {}".format(rc))
sys.exit(rc)
