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

★THE Int16 KEYFRAME CAP, AND HOW IT IS MET★  (was "KNOWN CEILING" -- it is not one)

  ts_gltf builds one TS::Shape keyframe AND one transform per sampled time, per
  (node, sequence).  Version 8 narrowed those indices to Int16 -- fFirstKeyframe and
  fnKeyframes are Int16 and fKeyValue is UInt16 in the LIVE in-memory struct
  (ts_shape.h:133,190-191), not merely on disk -- so the loader caps at 32000
  keyframes / 65000 transforms and DROPS the remainder (ts_gltf.cpp:1194-1201).
  Blender's NLA export always BAKES (export_force_sampling=False is ignored in
  NLA_TRACKS mode -- verified byte-identical), and a rate high enough to keep a
  0.033s sequence alive (240 fps) produced 678,946 keyframes on rpgmalehuman.dts.
  Sampling down instead wrecks duration fidelity (run 0.600s vs the authored
  0.667s at the ~10 fps the budget allowed) and looks choppy on a humanoid.

  So: bake HIGH, then remove the redundancy.  Three things get it under the cap.

    * --one-lod deletes all but the highest detail.  ★A .dts SHARES one subsequence
      range across its LOD copies★ (662 records serve 1730 node-sequence pairs)
      while ts_gltf gives every node its own -- 2.6x the keyframes for geometry a
      modern client never shows.  Joe's call, and the right one.
    * decimate_glb_animations() drops every key the interpolation already
      reproduces, HARD-FAILING above a 2e-3 reconstruction error.
    * ★The decimation metric IS the reconstruction error★ (see _decimate).  Three
      earlier attempts used a cheap PROXY for it and each one either broke poses or
      over-kept by 3.3x.  Measured on rpgmalehuman.dts, 88,401 baked sampler keys:

          metric                        keyframes   max rotation error
          plane only                      >cap      1.39      broken poses
          plane + monotonic angle         >cap      1.35      broken poses
          plane + uniform rate           83,929     1.24e-03  correct, 2.62x OVER
          objective (shipped)            25,303     3.05e-05  correct, UNDER CAP

      Smaller AND more accurate -- there was no trade.  ★Two explanations for the
      proxies' failure were recorded here and BOTH were refuted by measurement (a
      quaternion sign bug; an ill-conditioned plane basis).  The real cause was an
      UNNORMALISED dot product inside the rate test's angle helper.★  Full evidence
      is in _decimate's docstring -- read it before touching this, because both wrong
      guesses looked convincing.

  ★What the cap actually charges, since it is easy to measure the wrong thing.★
  ts_gltf.cpp:1167-1192 emits one keyframe per entry in the UNION of a node's
  translation and rotation key times for that sequence -- not one per sampler key.
  Measured union/sampler ratio on rpgmalehuman: 0.949, because Blender shares ONE
  input (time) accessor across every channel of an animation, so the two channels
  already agree on times.  Decimating them independently is therefore safe; it does
  not inflate the union.  Report the union, not the sampler total.
"""

import bpy
import sys
import os
import json
import math
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
        raise SystemExit("expected 2 args after --: <in.dts> <out.glb> [--one-lod]")
    one_lod = any(a == "--one-lod" for a in rest[2:])
    return os.path.abspath(rest[0]), os.path.abspath(rest[1]), one_lod


def check_addon_freshness():
    """★addon_enable() loads Blender's INSTALLED copy, not this repo.★

    That cost a full debugging cycle: a fix to the importer's keyframe placement
    sat in the repo while every conversion kept running the stale installed copy,
    so the measurement said the fix had not worked when it had simply never run.
    Same family as a stale object file -- verify, do not assume."""
    repo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "main.py")
    inst = os.path.join(bpy.utils.resource_path('USER'), "scripts", "addons",
                        ADDON_MODULE, "main.py")
    try:
        if not (os.path.exists(repo) and os.path.exists(inst)):
            return
        a = open(repo, 'rb').read()
        b = open(inst, 'rb').read()
        if a == b:
            log("addon: installed copy matches the repo")
            return
        log("!! ADDON IS STALE -- the installed copy differs from this repo:")
        log("!!   repo      {} bytes  {}".format(len(a), repo))
        log("!!   installed {} bytes  {}".format(len(b), inst))
        log("!! addon_enable() loads the INSTALLED one, so repo edits to main.py")
        log("!! (the DTS importer) are NOT in effect.  Copy it across and re-run.")
    except Exception as e:
        log("addon freshness check skipped: {}".format(e))


def enable_addon():
    check_addon_freshness()
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


def dts_sequence_owner_counts(path):
    """How many NODES each sequence actually drives, read from the .dts itself.

    ★This cannot be recovered from Blender.★ The DTS importer keys every node at
    every sequence boundary, so the fcurves say "all 48 nodes own all 12
    sequences" no matter what the file says.  The truth lives in the node ->
    subsequence -> sequence table.

    Returns a list indexed by SEQUENCE INDEX, which lines up with the timeline
    markers because the importer emits them in sequence order.  Returns None if
    the layout cannot be resolved, in which case the caller keeps every node
    (the old behaviour) rather than guessing.

    ★FULLY DETERMINISTIC.★  Every table offset is a running sum of the engine's own
    record sizes -- no stride solving, no scanning for the name table.  Both of those
    heuristics were WRONG, and measurably so: over an 823-shape corpus (all of
    Entities.vol + Editor.vol + every loose .dts in base\\ and rpg\\) the old code
    mis-parsed 228 shapes, handing 207 of them GARBAGE SEQUENCE NAMES.  On
    mortar_turret.DTS it reported ['?S7>?','?','e','ibility','n'] where the truth is
    ['fire','visibility','turn','elevate','power'] -- the scan landed 45 bytes early,
    so every 24-byte record straddled two real names.  That is the same failure class
    as the crash this whole tool exists to prevent: the engine binds sequences BY
    NAME, so a garbled name silently binds nothing.

    The stride "solve" was the worse bug conceptually: it searched 8..96 for a value
    that is a COMPILE-TIME CONSTANT.  Sequence is Int32 x 8 = 32 bytes for every
    version >= 5 (ts_shape.h:167-184) -- fName, fCyclic, fDuration, fPriority,
    fFirstFrameTrigger, fNumFrameTriggers, fNumIFLSubSequences, fFirstIFLSubSequence,
    all 4 bytes.  The search accepted the first stride whose subsequence table merely
    looked in-range, and returned something other than 32 on 196 of the 505 shapes
    that have sequences.

    Read order (ts_shape.cpp:1122-1142, no per-vector count prefix):
        nodes, sequences, subsequences, keyframes, transforms, NAMES, objects,
        details, transitions, frameTriggers
    Record sizes, all under #pragma pack(push,4), each verified in the header:
        Node       v<=7 Int32 x5 = 20 | v8 Int16 x5 = 10        ts_shape.h:214-221
        Sequence   v>=5 Int32 x8 = 32                           ts_shape.h:167-184
        SubSeq     v<=7 Int32 x3 = 12 | v8 Int16 x3 = 6         ts_shape.h:186-194
        Keyframe   v<=7 V7Keyframe RealF+UInt32 x2 = 12         ts_shape.cpp:985-989
                   v8   Keyframe   RealF+UInt16 x2 = 8          ts_shape.h:127-141
        Transform  v<7  V6Transform QuatF+Point3F x2 = 40       ts_transform.h:31-37
                   v==7 V7Transform Quat16+Point3F x2 = 32      ts_transform.h:170-176
                   v8   Transform   Quat16+Point3F     = 20     ts_transform.h:91-95
                        (TRANS_USE_SCALE is 0 -- ts_transform.h:20)
        Name       char[24]                                      ts_types.h MaxNameSize

    Still self-verifying, just on the RESULT instead of on the offsets: the name table
    must fit the file, every node's and sequence's name index must be in range, the
    resolved names must look like names, and every node->subsequence->sequence index
    must be in range.  On the 823-shape corpus that passes 823/823 (the scan passed
    615/823).  Returns None on any failure, so the caller falls back to keeping every
    node rather than acting on a bad parse.

    Versions below 5 are refused rather than guessed -- v4 and v3 read
    oldSequencesV4/V3 with different record layouts (ts_shape.cpp:1125-1129)."""
    try:
        data = open(path, 'rb').read()
        i = data.find(b"TS::Shape")
        if i < 0:
            return None
        o = i + 10
        (version, nNodes, nSeq, nSubSeq, nKeyframes, nTransforms, nNames,
         nObjects, nDetails, nMeshes) = struct.unpack_from("<10i", data, o)
        o += 40
        if version >= 2:
            o += 4                              # nTransitions
        if version >= 4:
            o += 4                              # nFrameTriggers
        o += 4 + 12                             # fRadius + fCenter
        if version > 7:
            o += 24                             # bounds box (v8+)

        if version < 5:
            log("  !! DTS version {} predates the v5 record layout -- refusing to guess"
                .format(version))
            return None

        if version <= 7:
            node_rec, sub_rec, node_fmt, sub_fmt = 20, 12, "<5i", "<3i"
        else:
            node_rec, sub_rec, node_fmt, sub_fmt = 10, 6, "<5h", "<3h"
        kf_rec = 12 if version <= 7 else 8
        xf_rec = 40 if version < 7 else (32 if version == 7 else 20)
        SEQ_REC = 32                    # Int32 x 8, every version >= 5

        node_off = o
        seq_off = node_off + nNodes * node_rec
        ss_off = seq_off + nSeq * SEQ_REC
        kf_off = ss_off + nSubSeq * sub_rec
        xf_off = kf_off + nKeyframes * kf_rec
        name_off = xf_off + nTransforms * xf_rec

        if name_off + nNames * 24 > len(data):
            log("  !! computed name table runs past EOF ({} > {}) -- layout not "
                "understood, keeping every node".format(name_off + nNames * 24, len(data)))
            return None

        nodes = [struct.unpack_from(node_fmt, data, node_off + k * node_rec)
                 for k in range(nNodes)]
        subs = [struct.unpack_from(sub_fmt, data, ss_off + k * sub_rec)
                for k in range(nSubSeq)]
        names = [data[name_off + 24 * k:name_off + 24 * k + 24].split(b"\x00")[0]
                 .decode("latin1") for k in range(nNames)]

        # ---- verify the RESULT (the offsets are not in question; the file might be)
        def _namelike(s):
            return len(s) >= 1 and any(c.isalnum() for c in s) and \
                all(32 <= ord(c) <= 126 for c in s)

        for (nmIdx, _p, _ns, _fs, _dt) in nodes:
            if not (0 <= nmIdx < nNames) or not _namelike(names[nmIdx]):
                log("  !! node name index {} does not resolve to a name -- keeping "
                    "every node".format(nmIdx))
                return None
        for k in range(nSubSeq):
            si, nk, fk = subs[k]
            if not (0 <= si < nSeq and 0 <= nk <= nKeyframes
                    and 0 <= fk <= nKeyframes and fk + nk <= nKeyframes):
                log("  !! subsequence {} out of range (seq {}, keys {}+{} of {}) -- "
                    "keeping every node".format(k, si, fk, nk, nKeyframes))
                return None

        counts = [0] * nSeq
        for (_nm, _par, nsub, firstsub, _dt) in nodes:
            for k in range(firstsub, firstsub + nsub):
                if 0 <= k < nSubSeq:
                    counts[subs[k][0]] += 1

        # Sequence is fName, fCyclic, fDuration, fPriority, ... (ts_shape.h:167-184),
        # so read all four: duration at +8, and cyclic/priority which ts_gltf currently
        # HARDCODES to 1 and 0.  ★75.3% of authored sequences across an 823-shape corpus
        # are NON-cyclic (2,579 of 3,424, in 356 shapes)★, and fCyclic is not cosmetic:
        # ts_shape.cpp:166-188 makes a position past the last key WRAP toward the first
        # one.  So forcing cyclic makes a weapon's 'fire'/'reload'/'activation' snap back
        # to its start pose at the end, and makes the player's 'looks' -- the view-pitch
        # sequence, authored fCyclic=0 -- wrap from full-up toward full-down at extreme
        # pitch.  Carry the real values so the loader can stop guessing.
        durations = [0.0] * nSeq
        cyclics = [0] * nSeq
        priorities = [0] * nSeq
        for s in range(nSeq):
            _nm, cyc, dur, prio = struct.unpack_from("<iifi", data, seq_off + s * SEQ_REC)
            durations[s] = dur
            cyclics[s] = int(cyc)
            priorities[s] = int(prio)

        # The name table is what makes EXACT per-(node, sequence) gating possible;
        # counting alone forced "this sequence drives 47 of 48 nodes, keep them all",
        # and that one extra node was 'bounds', which owns neither 'root' nor 'looks'.
        # Giving it a 'root' track pinned it to -29.821 during idle and made entering
        # the walk snap it 29 units -- a visible shift at exactly the idle<->walk
        # transitions.
        seq_names = None
        owned = None
        seq_ok = True
        for s in range(nSeq):
            ni = struct.unpack_from("<i", data, seq_off + s * SEQ_REC)[0]
            if not (0 <= ni < nNames) or not _namelike(names[ni]):
                log("  !! sequence {} name index {} does not resolve -- falling back "
                    "to per-sequence counts".format(s, ni))
                seq_ok = False
                break
        if seq_ok:
            # Sequence::fName is the record's first field, so ownership and durations
            # can be keyed by NAME.  That matters: keying by marker INDEX assumes every
            # sequence has a timeline marker, and rpgmalehuman has 44 sequences but only
            # 43 markers -- one unmarked sequence shifts every index after it.
            seq_names = []
            for s in range(nSeq):
                ni = struct.unpack_from("<i", data, seq_off + s * SEQ_REC)[0]
                seq_names.append(names[ni] if 0 <= ni < nNames else "seq%d" % s)
            owned = set()
            for (nmIdx, _par, nsub, firstsub, _dt) in nodes:
                for k in range(firstsub, firstsub + nsub):
                    if 0 <= k < nSubSeq:
                        si = subs[k][0]
                        if 0 <= si < nSeq:
                            owned.add((names[nmIdx], seq_names[si]))
            log("DTS name table at 0x{:X} (computed) -- exact per-node ownership, keyed by name"
                .format(name_off))
            # Duplicate sequence names COLLAPSE: markers are keyed by name and glTF
            # merges same-named tracks, so N sequences become N-distinct animations.
            # rpgmalehuman.dts has two called 'wave' (44 sequences, 43 names).
            # Harmless for a player -- GetSequenceIndex takes the first match and the
            # wire carries a SLOT -- but for a non-player shape sequence indices
            # 0..15 ARE a wire contract, so collapsing shifts them.
            dupes = sorted(set(n for n in seq_names if seq_names.count(n) > 1))
            if dupes:
                log("  note: duplicate sequence name(s) in the .dts {} -- {} sequences "
                    "collapse to {} distinct clips. Fine for a player (slot-based wire); "
                    "NOT fine for a station/vehicle, where index 0..15 is contracted."
                    .format(dupes, nSeq, len(set(seq_names))))
        else:
            log("  !! sequence names did not resolve -- falling back to per-sequence "
                "counts, so a partially-owned sequence keeps every node")

        log("DTS v{}: {} node(s), {} sequence(s), {} subsequence(s), {} keyframe(s), "
            "{} transform(s), {} name(s) -- all offsets computed, all indices in range"
            .format(version, nNodes, nSeq, nSubSeq, nKeyframes, nTransforms, nNames))
        nonc = sum(1 for c in cyclics if not c)
        if nonc:
            log("  {} of {} sequence(s) are authored NON-cyclic -- carried into the GLB "
                "as extras so the loader does not force them cyclic".format(nonc, nSeq))
        return counts, durations, owned, seq_names, cyclics, priorities
    except Exception as e:
        log("  !! could not read sequence ownership from the .dts: {}".format(e))
        return None


def has_keys_in(act, fs, fe):
    """Does this action key anything inside [fs, fe]?"""
    for fc in action_fcurves(act):
        for kp in fc.keyframe_points:
            if fs - 0.5 <= kp.co[0] <= fe + 0.5:
                return True
    return False


def sequence_closes(fs, fe, eps=1e-3):
    """Does the pose at the sequence's LAST frame equal the pose at its FIRST?

    A cyclic DTS sequence ends where it began, so the loop is seamless -- measured
    on tr_talon's 'run', the root node runs -0.4901 (f1) ... -10.7187 (f43) and
    back to -0.4901 (f44).  That closing keyframe is NOT a transition into the next
    sequence and must be kept, or the cycle wraps with a 10-unit snap: Joe saw the
    mech "shift to the right a bit" near the end of every walk cycle.

    For a STATIC sequence the last frame does not match ('root' holds -29.821 then
    ends at -0.490), and there it really is a stray frame -- keeping it made the
    engine flicker between two poses 15 times a second.  So decide from the data
    instead of applying one rule to both.

    Returns the FRACTION of channels that close, because an all-or-nothing test
    cannot distinguish "one node is off by a float epsilon" from "this is not a
    cycle".  Measured on tr_talon the answer is sharply bimodal, so the 0.90
    threshold sits in empty space:

        run         100.0%   <- closes
        Seq12_RLeg  100.0%   <- closes
        everything else 56.0 - 72.6%

    ★That also corrected an assumption of mine: I expected 'fastrun' and 'sprint'
    to be closed walk cycles too, and they are NOT (56-59%).  Only 'run' is a
    properly closed cycle in this shape -- which is precisely the one Joe saw
    snap.★"""
    same = 0
    total = 0
    for obj in bpy.data.objects:
        ad = obj.animation_data
        if not ad or not ad.action:
            continue
        for fc in action_fcurves(ad.action):
            try:
                total += 1
                if abs(fc.evaluate(fs) - fc.evaluate(fe)) <= eps:
                    same += 1
            except Exception:
                pass
    return (float(same) / total) if total else 0.0


def retime_strip(strip, fs, fe, dts_dur, fps, closes):
    """Place the strip at scene frame 0 and stretch it to the DTS's AUTHORED
    duration.

    Two separate corrections, both measured:
      * 0-basing -- the exporter bakes over the strip's SCENE range, and
        ts_gltf.cpp takes fDuration from the sampler's MAX time (not its span),
        normalising keys as time/duration.  A strip left where it was imported
        exported 'sprint' as 4.917..5.708s, so 86% of the cycle was a frozen
        first pose followed by a blip.
      * retiming -- the DTS importer lays sequences out by KEYFRAME COUNT, which
        throws the authored duration away.  Measured against tr_talon.dts:
        run 2.667s -> 1.792s (49% too fast), root 0.067s -> 0.167s (2.5x too
        slow).  The ratios are not a constant, so this is not an fps mismatch and
        cannot be fixed by changing the scene rate."""
    # Whether the "End of <seq>" frame belongs to the sequence is decided per
    # sequence by sequence_closes(): it is the cycle-closing keyframe when the pose
    # returns to the start, and a stray transition frame when it does not.  Keeping
    # a stray one flickers the pose 15x a second; dropping a closing one snaps the
    # root 10 units on every wrap.  Both were observed in game.
    last = fe if closes else (fe - 1 if fe - 1 > fs else fe)
    action_len = float(last - fs)
    strip.action_frame_start = float(fs)
    strip.action_frame_end = float(last)
    strip.frame_start = 0.0
    strip.frame_end = action_len
    if dts_dur and dts_dur > 0.0 and action_len > 0.0:
        target = dts_dur * float(fps)
        if target >= 1.0:
            strip.scale = target / action_len
            strip.frame_end = target
    return strip


def build_nla_tracks(ranges, count_of, dur_of, fps, owned):
    """One NLA track per sequence, per animated object, each strip referencing
    that sequence's sub-range of the object's single imported action.

    ★A strip is created ONLY where the object actually has keys in that range.★
    Node ownership per sequence is load-bearing, not cosmetic: an earlier build
    gave every sequence a strip on every animated object, so `looks` -- which owns
    ZERO nodes in tr_talon.dts -- came out owning all 48.  `looks` is the view
    thread, positioned from camera pitch every frame
    (player.cpp:702, pos = 0.5 + (-viewPitch/MaxPitch)*0.5), so it pinned the legs
    to a pitch-derived pose and the walk animation never showed.  Joe saw exactly
    that: "walking is not animated", and "move the camera fully up and the legs
    move forward/backwards while idle".

    Ground truth to diff against, from the DTS itself (tools v7_tracks.py):
      run 48, fastrun 48, root 47, crouch root 48, Seq05 48, Seq06 48,
      looks 0, sprint 48, fall 48, landing 48, Seq11 48, Seq12 48."""
    animated = 0
    strips = 0
    per_seq = dict((n, 0) for n, _, _ in ranges)

    # Which sequences drive nothing, straight from the .dts.  Marker order is
    # sequence order, so ranges[i] pairs with owner_counts[i].
    skip = set()
    if count_of:
        for (name, _, _) in ranges:
            if name in count_of:
                if count_of[name] == 0:
                    skip.add(name)
                elif 0 < count_of[name] < len([o for o in bpy.data.objects
                                               if o.animation_data and o.animation_data.action]):
                    log("  '{}' drives {} of the animated nodes -- gating {}".format(
                        name, count_of[name],
                        "EXACTLY, by node name" if owned else
                        "loosely (name table unavailable), so an unowned node keeps a track "
                        "it should not have"))
    if skip:
        log("sequences that drive NO nodes in the .dts: {}".format(sorted(skip)))

    closes = {}
    frac = {}
    for name, fs, fe in ranges:
        frac[name] = sequence_closes(fs, fe)
        closes[name] = frac[name] >= 0.90
    log("cycle closure (fraction of channels whose last frame == first frame):")
    for name, _, _ in ranges:
        log("    {:<14} {:5.1f}%  {}".format(
            name, frac[name] * 100.0,
            "CLOSES - keep last frame" if closes[name] else "stray - drop last frame"))

    for obj in bpy.data.objects:
        ad = obj.animation_data
        if not ad or not ad.action:
            continue
        act = ad.action
        animated += 1

        for si, (name, fs, fe) in enumerate(ranges):
            if name in skip:
                continue          # faithful: this sequence owns no nodes
            # ★EXACT ownership: the .dts says which NODES each sequence drives, and a
            # node given a track it does not own jumps when the sequence is entered.
            # 'bounds' owns 10 of 12 sequences -- not 'root', not 'looks' -- so a
            # fabricated 'root' track pinned it 29 units away from where the walk
            # starts, and the mech visibly shifted at every idle<->walk transition.
            if owned is not None and (obj.name, name) not in owned:
                continue
            if not has_keys_in(act, fs, fe):
                continue          # this sequence does not drive this node
            track = ad.nla_tracks.new()
            track.name = name
            try:
                strip = track.strips.new(name, int(fs), act)
            except Exception as e:
                log("  !! strip '{}' on '{}' failed: {}".format(name, obj.name, e))
                continue
            per_seq[name] += 1
            retime_strip(strip, fs, fe, dur_of.get(name, 0.0), fps, closes[name])
            strip.extrapolation = 'HOLD'
            strip.blend_type = 'REPLACE'
            strip.use_animated_influence = False
            strips += 1

        # Drop the active action so only the tracks describe motion.  The strips
        # keep their own reference, so the action itself stays alive.
        ad.action = None

    # A sequence that owns no nodes cannot be expressed in glTF -- an animation
    # must carry at least one channel (spec 5.1: animation.channels minItems 1) --
    # so it would silently vanish and take its NAME with it.  For "looks" that is
    # fatal: the name is how Player::initResources:388 finds the view sequence, and
    # a missing one is what crashed the client (player.cpp:700 -> SetSequence(-1)).
    # Keep the name alive by parking it on ONE inert leaf node, so the view thread
    # owns something harmless rather than the legs.
    orphans = [n for n, _, _ in ranges if per_seq[n] == 0]
    if orphans:
        parents = set(o.parent.name for o in bpy.data.objects if o.parent)

        def inert_rank(o):
            return (o.name.lower() != 'bounds',      # prefer the bbox node
                    o.type == 'MESH',                # then any non-mesh
                    o.name.lower() not in ('cam', 'coll0'))

        hosts = [o for o in bpy.data.objects
                 if o.name not in parents            # leaf only: moving a parent moves its subtree
                 and o.animation_data and o.animation_data.nla_tracks is not None]
        hosts.sort(key=inert_rank)
        for name in orphans:
            si = [i for i, (n, _, _) in enumerate(ranges) if n == name][0]
            fs, fe = ranges[si][1], ranges[si][2]
            placed = False
            for host in hosts:
                act = next((s.action for t in host.animation_data.nla_tracks
                            for s in t.strips if s.action), None)
                if not act:
                    continue
                track = host.animation_data.nla_tracks.new()
                track.name = name
                strip = track.strips.new(name, 0, act)
                # Keep the real 2-frame range.  Collapsing it to a single instant
                # was tried and BACKFIRED: a zero-length action range yields no
                # sampled channels, Blender drops the animation, and the sequence
                # NAME disappears -- which is the condition that crashed the client.
                # The pose is constant anyway: the importer keys this node's held
                # transform at both ends of a range the .dts gives it no track in.
                retime_strip(strip, fs, fe, dur_of.get(name, 0.0), fps,
                             closes.get(name, False))
                per_seq[name] = 1
                placed = True
                log("  '{}' owns no nodes in the DTS -- parked on inert leaf '{}' "
                    "to keep the sequence NAME (glTF needs >=1 channel)".format(name, host.name))
                break
            if not placed:
                log("  !! '{}' owns no nodes and no inert host was found -- the name "
                    "will be LOST".format(name))

    log("NLA: {} animated object(s), {} strip(s)".format(animated, strips))
    log("node ownership per sequence (diff this against tools/v7_tracks.py on the .dts):")
    for n, _, _ in ranges:
        log("    {:<14} {}".format(n, per_seq[n]))
    return animated, strips


def trailing_int(name):
    """Trailing integer of a node name: "submesh_lfoot 10" -> 10, "lfoot10" -> 10.
    Mirrors gltfTrailingInt in ts_gltf.cpp so detail roots are identified the same
    way the loader will identify them."""
    s = (name or "").rstrip()
    i = len(s)
    while i > 0 and s[i - 1].isdigit():
        i -= 1
    if i == len(s) or i == 0:
        return None
    return int(s[i:])


def collapse_to_one_lod():
    """Delete every detail level except the highest.

    Joe's call, and it is the right one for a modern client: "it's not 1998, we
    don't need LOD cull".  It also removes the single biggest source of keyframe
    bloat.  ★A .dts SHARES one subsequence range across its LOD copies★ -- 662
    records serve 1730 node-sequence pairs in rpgmalehuman -- but ts_gltf gives
    every node its own subsequence, so the LOD duplicates cost 2.6x the keyframes
    for geometry the player will now never see.

    Detail roots are found the way ts_gltf does it: a node whose name ends in an
    integer and which has no integer-suffixed ancestor.  Nodes with no suffix
    (bounds, cam, coll0, dummyalways root) belong to no detail and are KEPT --
    collision details live at negative sizes and must survive.
    """
    roots = {}                      # size -> [objects]
    for obj in bpy.data.objects:
        size = trailing_int(obj.name)
        if size is None or size <= 0:
            continue
        anc = obj.parent
        suffixed_ancestor = False
        while anc is not None:
            if trailing_int(anc.name) is not None:
                suffixed_ancestor = True
                break
            anc = anc.parent
        if not suffixed_ancestor:
            roots.setdefault(size, []).append(obj)
    if len(roots) <= 1:
        log("LOD collapse: only one detail level present, nothing to do")
        return

    keep = max(roots)
    log("LOD collapse: details {} -> keeping {} only".format(
        sorted(roots, reverse=True), keep))

    doomed = []

    def mark(o):
        doomed.append(o)
        for c in o.children:
            mark(c)

    for size, objs in roots.items():
        if size == keep:
            continue
        for o in objs:
            mark(o)

    names = set(o.name for o in doomed)
    for o in doomed:
        try:
            bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            pass
    log("LOD collapse: removed {} object(s); {} remain".format(
        len(names), len(bpy.data.objects)))


def set_linear_interpolation():
    """★DTS INTERPOLATES between keyframes -- LERP for translation, SLERP for
    rotation★ (docs/darkstar_dts_master_reference.md).  The importer leaves
    Blender's keys on CONSTANT interpolation, which holds each value and jumps at
    the next key.  Baking a sparse track then produces stair-steps:
    dummy_baseA64's X had only 12 distinct values across 321 samples, and since it
    is the detail-0 root the whole mech jumped between them instead of swaying.
    Joe: "it moves left and right now ... but it's not smooth, it seems jumpy".

    Note the GLB's sampler already declared LINEAR -- it was faithfully
    reproducing steps that were baked in, so the format was not the problem.

    Done here rather than in main.py so the shared importer is left alone; this
    converter is the only consumer that bakes."""
    total = 0
    for act in bpy.data.actions:
        for fc in action_fcurves(act):
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'
                total += 1
            try:
                fc.update()
            except Exception:
                pass
    log("interpolation: {} keyframe(s) set to LINEAR (DTS lerps; Blender defaulted "
        "to CONSTANT)".format(total))


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


def _fit_error_lin(times, vals, lo, hi):
    """Worst deviation if [lo..hi] collapses to ONE linearly interpolated segment."""
    if hi <= lo + 1:
        return 0.0
    span = times[hi] - times[lo]
    ncomp = len(vals[0])
    a, b = vals[lo], vals[hi]
    worst = 0.0
    for m in range(lo + 1, hi):
        f = 0.0 if span <= 0 else (times[m] - times[lo]) / span
        row = vals[m]
        for c in range(ncomp):
            e = abs(a[c] + (b[c] - a[c]) * f - row[c])
            if e > worst:
                worst = e
    return worst


def _fit_error_rot(times, vals, lo, hi):
    """Worst deviation if [lo..hi] collapses to ONE slerp segment."""
    if hi <= lo + 1:
        return 0.0
    span = times[hi] - times[lo]
    a, b = vals[lo], vals[hi]
    worst = 0.0
    for m in range(lo + 1, hi):
        f = 0.0 if span <= 0 else (times[m] - times[lo]) / span
        q = _slerp(a, b, f)
        row = vals[m]
        e1 = max(abs(q[c] - row[c]) for c in range(4))
        e2 = max(abs(-q[c] - row[c]) for c in range(4))
        e = e1 if e1 < e2 else e2
        if e > worst:
            worst = e
    return worst


def _decimate(times, vals, tol, rotation):
    """Indices to KEEP, by greedy longest-segment fit.

    ★THE ACCEPTANCE TEST IS THE OBJECTIVE ITSELF.★  That is the whole point, and it
    is what three earlier attempts got wrong.  Every previous metric was a PROXY for
    the reconstruction error we are actually graded on -- "does the next sample
    continue the line through the first two points" for translation, "does every
    sample stay in the 2-plane of the first two, sweeping at a uniform rate" for
    rotation -- and a proxy can always diverge from its objective.  Measured on
    rpgmalehuman.dts (43 sequences, 36 nodes, 88,401 baked sampler keys):

        metric                        keyframes   max rotation error
        plane only                      >cap      1.39      <- visibly broken poses
        plane + monotonic angle         >cap      1.35      <- visibly broken poses
        plane + uniform rate           83,929     1.24e-03  <- correct, 2.62x OVER CAP
        THIS (objective)               25,303     3.05e-05  <- correct AND under cap

    So the objective test is simultaneously 3.3x smaller and 40x more accurate than
    the best proxy.  Nothing was traded away.

    ★Why the proxies failed -- MEASURED, after two wrong guesses.  Do not re-derive.★

    The over-keying had a single numerical cause: the old rate test's helper
    _quat_angle computed acos of the RAW dot of two float32 quaternions, without
    normalising them.  Blender's exported quaternions carry |1-|q|| up to 1.9e-07, and
    since d = |a||b|cos(phi) the angle error is amplified by 1/sin(phi) -- at a 240 fps
    bake phi is about 1.4e-03, so 1e-07 of magnitude error becomes ~1e-04 rad, which
    IS ANG_TOL.  Measured on the raw baked human: the raw |dot| EXCEEDS 1.0 on 8.9% of
    adjacent pairs (20,252 of 227,264), where the clamp fires and the angle is reported
    as EXACTLY ZERO; against an exact chord reference the shipped angle was wrong by
    more than the whole tolerance on 23.4% of pairs, versus 0.0000% once normalised.
    The rate test caused 92.3% of all cuts, so the metric was very largely cutting on
    float noise rather than geometry.  Just normalising takes it to 29,193 keyframes /
    8.08e-04 -- which passes; the objective metric below is simply better still
    (24,571 / 1.00e-04) and cannot regress this way, because its accept test is the
    error itself and it normalises up front.

    ★TWO HYPOTHESES THIS FILE PREVIOUSLY RECORDED, BOTH REFUTED BY MEASUREMENT:★
      1. "quaternion SIGN handling, because the error lands near sqrt(2)".  No: sign is
         handled in all three places that matter -- QuatF::interpolate negates q2 when
         cosOmega < 0 (m_quat.cpp:259-265), _slerp below does the same, and the error
         metric compares both +q and -q.
      2. "the plane's Gram-Schmidt basis is ill-conditioned because it is built from two
         nearly identical adjacent samples, so the plane is float noise".  No.  The
         residual nr has median 1.4e-03, an order of magnitude above the supposed noise
         scale, and only 3.8% of evaluated pairs fall in the (1e-9, 1e-4) band.  An SVD
         of the accepted segments puts their TRUE out-of-plane extent at median 2.7e-08
         (max 1.0e-05, tol 3.05e-05) -- the planes accepted were the arc's real planes.
         Rebuilding the basis from a WELL-SEPARATED pair rejects no more segments
         (5/592 vs 3/592).  The mechanism was self-defeating anyway: an arbitrary bn
         makes the residual LARGER, so it would cut earlier and keep MORE keys, while
         plane-only measurably keeps FEWER.
      What the ~1.35 error actually is: the correct great circle traversed to the WRONG
      EXTENT.  The samples are coplanar, but the short-arc slerp between the segment's
      endpoints cannot reach them because the true sweep is large (rate deviation up to
      3.13 rad).  In-plane is NECESSARY but NOT SUFFICIENT -- that part was always right.

    Schedule: from each kept key, exponentially probe outward while the segment still
    fits, then binary-search the boundary between the last good end and the first bad
    one.  A segment of length L costs O(L log L) instead of the O(L^2) of testing
    every candidate end.

    ★Monotonicity caveat, deliberately accepted.★  "[lo..hi] fits" is not strictly
    monotone in hi, so the binary search can stop at a shorter segment than the true
    maximum.  That direction is safe -- it keeps an EXTRA key and never exceeds tol --
    and the caller re-verifies the whole result with _reconstruct_error against every
    original sample, so correctness never rests on this assumption.
    """
    n = len(times)
    if n <= 2:
        return list(range(n))
    fit = _fit_error_rot if rotation else _fit_error_lin
    keep = [0]
    lo = 0
    while lo < n - 1:
        step = 1
        good = lo + 1
        while True:
            cand = lo + step * 2
            if cand >= n - 1:
                if fit(times, vals, lo, n - 1) <= tol:
                    good = n - 1
                break
            if fit(times, vals, lo, cand) <= tol:
                good = cand
                step *= 2
            else:
                break
        if good < n - 1:
            a, b = good, min(lo + step * 2, n - 1)
            while b - a > 1:
                mid = (a + b) // 2
                if fit(times, vals, lo, mid) <= tol:
                    a = mid
                else:
                    b = mid
            good = a
        if good <= lo:
            good = lo + 1
        keep.append(good)
        lo = good
    if keep[-1] != n - 1:
        keep.append(n - 1)
    return keep


def _qnorm(q):
    n = math.sqrt(sum(x * x for x in q)) or 1.0
    return [x / n for x in q]


def _slerp(a, b, t):
    # ★Shortest arc.★  Negating q2 on a negative dot is not an optimisation, it is
    # required for parity: QuatF::interpolate does exactly this (m_quat.cpp:259-265),
    # so a metric that did NOT flip would be measuring a different curve than the one
    # the engine will play back.
    d = sum(a[c] * b[c] for c in range(4))
    if d < 0.0:
        b = [-x for x in b]
        d = -d
    if d > 0.9995:
        r = [a[c] + (b[c] - a[c]) * t for c in range(4)]
    else:
        th = math.acos(max(-1.0, min(1.0, d)))
        s = math.sin(th)
        w1 = math.sin((1.0 - t) * th) / s
        w2 = math.sin(t * th) / s
        r = [a[c] * w1 + b[c] * w2 for c in range(4)]
    n = math.sqrt(sum(x * x for x in r)) or 1.0
    return [x / n for x in r]


def _reconstruct_error(times, vals, keep, rotation=False):
    """Max abs deviation when the kept keys are linearly re-sampled at every
    original time.  This is the number that justifies calling it lossless."""
    worst = 0.0
    ncomp = len(vals[0])
    ki = 0
    for idx in range(len(times)):
        while ki + 1 < len(keep) - 1 and keep[ki + 1] < idx:
            ki += 1
        a, b = keep[ki], keep[min(ki + 1, len(keep) - 1)]
        span = times[b] - times[a]
        f = 0.0 if span == 0 else (times[idx] - times[a]) / span
        if rotation:
            q = _slerp(vals[a], vals[b], f)
            # q and -q are the same rotation, so compare the closer representation
            e1 = max(abs(q[c] - vals[idx][c]) for c in range(4))
            e2 = max(abs(-q[c] - vals[idx][c]) for c in range(4))
            worst = max(worst, min(e1, e2))
        else:
            for c in range(ncomp):
                pred = vals[a][c] + (vals[b][c] - vals[a][c]) * f
                worst = max(worst, abs(pred - vals[idx][c]))
    return worst


def reorder_animations(path, seq_names):
    """Put the GLB's animations back into the .dts SEQUENCE TABLE order.

    ★This is a WIRE CONTRACT, not tidiness.★  A script thread transmits a 4-bit
    SEQUENCE INDEX (ThreadSequenceBits = 4, MaxSequenceIndex = 16, shapebase.h:21-29,
    packed at shapeBase.cpp:1337-1382), and ts_gltf assigns sequence indices in glTF
    animation order (buildAnimations pushes one Sequence per animation, in order).  So
    for anything SCRIPT drives -- stations, turrets, sensors, mines, static shapes --
    glTF animation index IS the number the server sends.

    The exported order is NOT the .dts order.  Track creation follows the importer's
    TIMELINE MARKERS, and the marker layout does not match the sequence table, so
    animations came out permuted.  Measured before this fix:

        mortar_turret.DTS  .dts: fire, visibility, turn, elevate, power
                           .glb: turn, fire, elevate, power, visibility   (all 5 wrong)
        gunturet.DTS       .dts: turn, elevate, fire
                           .glb: turn, fire, elevate                      (1 and 2 swapped)

    A turret built from that plays the WRONG animation for a correct wire value, and
    nothing at runtime can notice: the index is valid, it just means something else.
    Players were unaffected -- their wire value is a SLOT bound by NAME through animData
    (playerUpdate.cpp:1269) -- which is exactly why this survived the player testing.

    Sequences the .dts has but the GLB does not (one that drives no nodes and was
    dropped) cannot be repaired by reordering: every later index still shifts.  Those
    are reported loudly rather than silently tolerated.
    """
    if not seq_names:
        return
    with open(path, 'rb') as f:
        data = f.read()
    if data[0:4] != b'glTF':
        return
    off = 12
    chunks, js = [], None
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == b'JSON':
            js = json.loads(chunk.decode('utf-8'))
            chunks.append([ctype, None])
        else:
            chunks.append([ctype, chunk])
    if js is None or not js.get('animations'):
        return

    anims = js['animations']
    by_name = {}
    for a in anims:
        by_name.setdefault(a.get('name'), []).append(a)

    ordered, missing = [], []
    for nm in seq_names:
        bucket = by_name.get(nm)
        if bucket:
            ordered.append(bucket.pop(0))
        else:
            missing.append(nm)
    # anything the GLB has that the .dts table did not name, kept at the end
    extra = [a for bucket in by_name.values() for a in bucket]

    before = [a.get('name') for a in anims]
    after = [a.get('name') for a in ordered + extra]
    if before == after:
        log("sequence order already matches the .dts table")
        return

    js['animations'] = ordered + extra
    new_json = json.dumps(js, separators=(',', ':')).encode('utf-8')
    while len(new_json) % 4:
        new_json += b' '
    out = bytearray()
    for ctype, payload in chunks:
        body = new_json if ctype == b'JSON' else payload
        if ctype != b'JSON':
            while len(body) % 4:
                body = body + b'\x00'
        out += struct.pack('<I4s', len(body), ctype) + body
    with open(path, 'wb') as f:
        f.write(struct.pack('<4sII', b'glTF', 2, 12 + len(out)) + bytes(out))

    log("REORDERED animations into .dts sequence order (glTF index == wire index):")
    log("   was: {}".format(before[:8]))
    log("   now: {}".format(after[:8]))
    if missing:
        log("  !! {} sequence(s) in the .dts have NO animation in the GLB: {}"
            .format(len(missing), missing))
        log("  !! every index after the first of those is still shifted -- for a "
            "SCRIPT-DRIVEN shape (station/turret/sensor/mine) that breaks the 4-bit "
            "sequence index on the wire.")
    if extra:
        log("  note: {} animation(s) not named in the .dts sequence table, appended "
            "after the contracted range: {}".format(len(extra), [a.get('name') for a in extra]))


def dts_material_maps(path):
    """Each .dts material's fMapFile, indexed by DTS material index.

    ★The texture NAME is lost between the .dts and the .glb.★  The importer names
    Blender materials "<file>.<index>" (main.py:833) and wires no image, so the exported
    glTF material carries no base_color_texture -- and ts_gltf then falls back to a flat
    colour (MatRGB, ts_gltf.cpp:757-761).  ★Every converted shape has therefore rendered
    UNTEXTURED★; it is only obvious on a character, because a mech's panels are mostly
    flat-coloured in the .dts anyway (tr_talon: 3 of 21 materials textured).

    ★The fix must carry the NAME, never an embedded image.★  `base.larmor.BMP` is the
    Tribes skin convention <skinbase>.<armour>.BMP and the engine rewrites that prefix
    per player at runtime, so baking the texture into the .glb would freeze one armour
    appearance and break skin swapping outright.

    Format, from the engine's own serialiser:
      MaterialList::read  (ts_material.cpp:578)  Int32 fnDetails, Int32 fnMaterials,
                          then fnDetails*fnMaterials records
      Material::write     (ts_material.cpp:299)  sizeof(Params) minus fUseDefaultProps
                          = 60 bytes
      Params              (ts_material.h:172-189) fMapFile[32] at offset 16

    Located by SIGNATURE rather than by summing the preceding tables: objects, details,
    transitions and frameTriggers all sit in between, each version- and
    alignment-dependent, so an offset chain would have four chances to be silently
    wrong.  Every candidate is validated (each fMapFile must be NUL-padded ASCII or all
    zeros) and the one that actually names textures wins.
    """
    REC, MAP_OFF, MAP_LEN = 60, 16, 32
    NULB = b"\x00"

    def plausible(b):
        z = b.find(NULB)
        if z < 0:
            return False
        if z == 0:
            return all(c == 0 for c in b)
        return all(32 <= c <= 126 for c in b[:z]) and all(c == 0 for c in b[z:])

    try:
        raw = open(path, 'rb').read()
    except Exception:
        return None
    start = raw.find(b"TS::Shape")
    if start < 0:
        start = 0
    best = None
    for off in range(start, len(raw) - 8):
        nDet, nMat = struct.unpack_from("<ii", raw, off)
        if not (1 <= nDet <= 32 and 1 <= nMat <= 512):
            continue
        total = nDet * nMat
        if off + 8 + total * REC > len(raw):
            continue
        maps, ok = [], True
        for m in range(total):
            base = off + 8 + m * REC
            fld = raw[base + MAP_OFF:base + MAP_OFF + MAP_LEN]
            if not plausible(fld):
                ok = False
                break
            maps.append(fld.split(NULB)[0].decode('latin1'))
        if not ok:
            continue
        named = sum(1 for m in maps if m)
        if best is None or (named, total) > best[0]:
            best = ((named, total), maps)
    return best[1] if best else None


def inject_material_extras(path, map_of):
    """Carry each material's .dts fMapFile into the GLB as material extras.

    The glTF material NAME is "<file>.<dtsIndex>", so its trailing integer recovers
    which .dts material it came from.  That matters: tr_talon exports 16 glTF materials
    against 21 in the .dts, so the correspondence is NOT positional."""
    if not map_of:
        return
    with open(path, 'rb') as f:
        data = f.read()
    if data[0:4] != b'glTF':
        return
    off, chunks, js = 12, [], None
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == b'JSON':
            js = json.loads(chunk.decode('utf-8'))
            chunks.append([ctype, None])
        else:
            chunks.append([ctype, chunk])
    if js is None or not js.get('materials'):
        return

    tagged, unmatched = 0, []
    for m in js['materials']:
        idx = trailing_int(m.get('name'))
        if idx is None or not (0 <= idx < len(map_of)):
            unmatched.append(m.get('name'))
            continue
        if not map_of[idx]:
            continue                    # genuinely flat-coloured in the .dts
        m.setdefault('extras', {})['dtsMapFile'] = map_of[idx]
        tagged += 1

    if not tagged:
        log("  !! no material matched a .dts map file -- textures stay flat")
        return

    new_json = json.dumps(js, separators=(',', ':')).encode('utf-8')
    while len(new_json) % 4:
        new_json += b' '
    out = bytearray()
    for ctype, payload in chunks:
        body = new_json if ctype == b'JSON' else payload
        if ctype != b'JSON':
            while len(body) % 4:
                body = body + b'\x00'
        out += struct.pack('<I4s', len(body), ctype) + body
    with open(path, 'wb') as f:
        f.write(struct.pack('<4sII', b'glTF', 2, 12 + len(out)) + bytes(out))

    named = sorted(set(x for x in map_of if x))
    log("material extras: {} of {} material(s) carry a .dts map file {}"
        .format(tagged, len(js['materials']), named[:6]))
    if unmatched:
        log("  note: {} material name(s) had no trailing .dts index: {}"
            .format(len(unmatched), unmatched[:4]))


def inject_sequence_extras(path, cyclic_of, prio_of):
    """Carry the .dts's authored fCyclic / fPriority into the GLB as animation extras.

    ★Why this is needed.★  ts_gltf.cpp:1113,1115 hardcodes fCyclic = 1 and
    fPriority = 0 with the comment "cyclic-ness is not in glTF ... the slot->clip
    binding that actually drives a character comes from animData".  The second half is
    true; the first half is a gap, not a fact, and the consequence is not cosmetic.

    fCyclic decides whether a position past the LAST key wraps toward the FIRST
    (ts_shape.cpp:166-188).  Measured over an 823-shape corpus, 75.3% of authored
    sequences are NON-cyclic (2,579 of 3,424, across 356 shapes) -- including every
    weapon's 'activation' / 'fire' / 'reload' (which would snap back to the start pose
    at the end of the swing) and the player's 'looks', the VIEW-PITCH sequence, whose
    position is driven by where the player is aiming: forcing it cyclic wraps the pose
    from full-up toward full-down at extreme pitch.

    glTF has no cyclic field, but `extras` is arbitrary application JSON on any object
    (spec 3.2 / 5.-- "extras": any) and cgltf already parses it into
    cgltf_animation::extras.data (third_party/cgltf.h:3024 allocates and NUL-terminates
    the raw substring).  So write it there and let the loader read it, defaulting to the
    old hardcoded values when the key is absent -- which keeps every GLB produced before
    this change loading exactly as it does today.

    Done as a JSON post-pass rather than through Blender: the glTF exporter derives
    extras from custom properties on nodes/meshes/materials, not on animations, and
    this file already rewrites the GLB wholesale in decimate_glb_animations."""
    if not cyclic_of and not prio_of:
        return
    with open(path, 'rb') as f:
        data = f.read()
    if data[0:4] != b'glTF':
        log("  !! not a GLB, skipping sequence extras")
        return
    off = 12
    chunks = []
    js = None
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == b'JSON':
            js = json.loads(chunk.decode('utf-8'))
            chunks.append([ctype, None])          # placeholder, refilled below
        else:
            chunks.append([ctype, chunk])
    if js is None or not js.get('animations'):
        return

    tagged = 0
    noncyclic = 0
    for a in js['animations']:
        nm = a.get('name')
        if nm is None or (nm not in cyclic_of and nm not in prio_of):
            continue
        ex = a.setdefault('extras', {})
        if nm in cyclic_of:
            ex['dtsCyclic'] = int(cyclic_of[nm])
            if not cyclic_of[nm]:
                noncyclic += 1
        if nm in prio_of:
            ex['dtsPriority'] = int(prio_of[nm])
        tagged += 1

    if not tagged:
        log("  !! no GLB animation name matched a .dts sequence -- extras not written")
        return

    new_json = json.dumps(js, separators=(',', ':')).encode('utf-8')
    while len(new_json) % 4:
        new_json += b' '                          # spec: JSON chunk padded with spaces
    out = bytearray()
    for ctype, payload in chunks:
        body = new_json if ctype == b'JSON' else payload
        if ctype != b'JSON':
            while len(body) % 4:
                body = body + b'\x00'             # spec: BIN chunk padded with zeros
        out += struct.pack('<I4s', len(body), ctype) + body
    header = struct.pack('<4sII', b'glTF', 2, 12 + len(out))
    with open(path, 'wb') as f:
        f.write(header + bytes(out))
    log("sequence extras: tagged {} animation(s) with authored fCyclic/fPriority "
        "({} non-cyclic)".format(tagged, noncyclic))


def decimate_glb_animations(path):
    """Drop every animation keyframe that linear interpolation already reproduces.

    ★Why this exists.★ ts_gltf builds one TS::Shape keyframe AND one transform per
    sampled time, per (node, sequence), against Int16 caps (MaxKeyframes 32000).
    Blender's NLA export always BAKES -- export_force_sampling=False is ignored in
    NLA_TRACKS mode -- so a rate high enough to keep a 0.033s clip alive produced
    678,946 keyframes on rpgmalehuman, 21x the cap, and the loader would have
    silently dropped most of the animation.  Sampling down instead wrecks duration
    fidelity (run 0.600s vs the authored 0.667s at the 10 fps the budget allowed).

    So: bake HIGH, then remove the redundancy.  Because the source is piecewise
    linear this is lossless to float noise, and the reported max error proves it.

    Rotation tolerance is one Quat16 LSB (1/32767): the engine stores node
    rotations quantised to 16 bits per component (docs: Quat16), so anything below
    that cannot survive into the shape anyway.
    """
    EPS_ROT = 1.0 / 32767.0
    EPS_LIN = 1e-4

    with open(path, 'rb') as f:
        data = f.read()
    if data[0:4] != b'glTF':
        log("  !! not a GLB, skipping decimation")
        return
    off = 12
    js = None
    bin_bytes = b''
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == b'JSON':
            js = json.loads(chunk.decode('utf-8'))
        elif ctype[0:3] == b'BIN':
            bin_bytes = chunk
    if js is None or not js.get('animations'):
        return

    accs = js.get('accessors', [])
    bvs = js.get('bufferViews', [])
    CFMT = {5120: 'b', 5121: 'B', 5122: 'h', 5123: 'H', 5125: 'I', 5126: 'f'}
    NC = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}

    # Bail out rather than corrupt anything if the file uses a feature this pass
    # does not model.  (Verified absent on both test shapes: no sparse accessors,
    # no byteStride, no matrix types, one accessor per bufferView.)
    for a in accs:
        if 'sparse' in a or a['type'] not in NC or a['componentType'] not in CFMT:
            log("  !! decimation skipped: unsupported accessor layout")
            return

    def read_acc(ai):
        a = accs[ai]
        v = bvs[a['bufferView']]
        base = v.get('byteOffset', 0) + a.get('byteOffset', 0)
        nc = NC[a['type']]
        f = CFMT[a['componentType']]
        stride = v.get('byteStride') or struct.calcsize(f) * nc
        return [struct.unpack_from('<' + f * nc, bin_bytes, base + i * stride)
                for i in range(a['count'])]

    # Decimate each sampler INDEPENDENTLY, and give it its own time accessor.
    # ★Blender shares one input accessor across every channel of an animation★, so
    # patching accessors in place is impossible: each channel thins differently.
    # Emitting fresh accessors per sampler is what makes this work at all -- the
    # first attempt guarded against shared accessors and therefore skipped every
    # single sampler ("decimation: nothing to remove").
    # A sampler only knows it drives rotation via the channel that references it.
    rot_sampler = {}
    for a in js['animations']:
        for ch in a.get('channels', []):
            if ch.get('target', {}).get('path') == 'rotation':
                rot_sampler[id(a['samplers'][ch['sampler']])] = True

    new_for = {}           # id(sampler) -> (times list, values list)
    before = after = 0
    worst = 0.0
    for a in js['animations']:
        for s in a.get('samplers', []):
            if s.get('interpolation', 'LINEAR') != 'LINEAR':
                continue
            ai, ao = s['input'], s['output']
            if 'bufferView' not in accs[ai] or 'bufferView' not in accs[ao]:
                continue
            ts = read_acc(ai)
            vs = read_acc(ao)
            if len(ts) != len(vs) or len(ts) < 3:
                continue
            times = [t[0] for t in ts]
            is_rot = (accs[ao]['type'] == 'VEC4' and rot_sampler.get(id(s), False))
            if is_rot:
                # ★Normalise before fitting -- this line is LOAD-BEARING, not hygiene.★
                # Blender's exported quaternions carry |1-|q|| up to 1.9e-07, and the
                # raw dot of two adjacent ones EXCEEDS 1.0 on 8.9% of pairs (measured:
                # 20,252 of 227,264 on the baked human).  Any acos/slerp fed those
                # clamps, and the previous metric's angle helper reported such pairs as
                # EXACTLY ZERO degrees apart -- which is what made it cut on noise.
                # Normalising here is what keeps that entire failure class out.
                # _slerp also renormalises its result, so comparing it against a
                # non-unit sample would otherwise charge the fit for the LENGTH
                # difference rather than the pose difference.  glTF requires unit
                # rotation quaternions anyway (spec 3.7.3.2 / 5.24) and ts_gltf hands
                # them straight to QuatF::interpolate, so writing the normalised values
                # back is both safe and more correct.
                vs = [tuple(_qnorm(v)) for v in vs]
            keep = _decimate(times, vs, EPS_ROT if is_rot else EPS_LIN, is_rot)
            before += len(times)
            after += len(keep)
            if len(keep) < len(times):
                worst = max(worst, _reconstruct_error(times, vs, keep, is_rot))
            new_for[id(s)] = ([times[k] for k in keep], [vs[k] for k in keep])

    if not new_for:
        log("decimation: nothing to remove")
        return

    # Rebuild accessors + views + binary from scratch.  Every accessor gets its own
    # tightly packed view, which also drops the now-unreferenced original animation
    # data instead of leaving 10 MB of it stranded in the file.
    new_bin = bytearray()
    new_accs = []
    new_views = []

    def emit(rows, ctype, gtype, target=None):
        nonlocal new_bin
        while len(new_bin) % 4:
            new_bin.append(0)
        start = len(new_bin)
        f = CFMT[ctype]
        nc = NC[gtype]
        for r in rows:
            new_bin += struct.pack('<' + f * nc, *r)
        nv = {'buffer': 0, 'byteOffset': start, 'byteLength': len(new_bin) - start}
        if target is not None:
            nv['target'] = target
        new_views.append(nv)
        acc = {'bufferView': len(new_views) - 1, 'componentType': ctype,
               'count': len(rows), 'type': gtype}
        if ctype == 5126 and rows:
            acc['min'] = [min(r[c] for r in rows) for c in range(nc)]
            acc['max'] = [max(r[c] for r in rows) for c in range(nc)]
        new_accs.append(acc)
        return len(new_accs) - 1

    # non-animation accessors first, keeping an index map for meshes
    anim_accs = set()
    for a in js['animations']:
        for s in a.get('samplers', []):
            anim_accs.add(s['input'])
            anim_accs.add(s['output'])
    remap = {}
    for ai, a in enumerate(accs):
        if ai in anim_accs:
            continue
        v = bvs[a['bufferView']]
        remap[ai] = emit(read_acc(ai), a['componentType'], a['type'], v.get('target'))
    for m in js.get('meshes', []):
        for p in m.get('primitives', []):
            for k, val in list(p.get('attributes', {}).items()):
                p['attributes'][k] = remap[val]
            if 'indices' in p:
                p['indices'] = remap[p['indices']]

    for a in js['animations']:
        for s in a.get('samplers', []):
            if id(s) not in new_for:
                s['input'] = remap.get(s['input'], s['input'])
                s['output'] = remap.get(s['output'], s['output'])
                continue
            times, vals = new_for[id(s)]
            out_type = accs[s['output']]['type']
            s['input'] = emit([(t,) for t in times], 5126, 'SCALAR')
            s['output'] = emit(vals, 5126, out_type)

    js['accessors'] = new_accs
    js['bufferViews'] = new_views
    js['buffers'] = [{'byteLength': len(new_bin)}]

    jb = json.dumps(js, separators=(',', ':')).encode('utf-8')
    while len(jb) % 4:
        jb += b' '
    while len(new_bin) % 4:
        new_bin.append(0)
    out = bytearray()
    out += struct.pack('<4sII', b'glTF', 2, 12 + 8 + len(jb) + 8 + len(new_bin))
    out += struct.pack('<I4s', len(jb), b'JSON') + jb
    out += struct.pack('<I4s', len(new_bin), b'BIN\x00') + bytes(new_bin)
    with open(path, 'wb') as f:
        f.write(out)

    log("decimation: {} -> {} animation keys ({:.1f}x smaller), max reconstruction "
        "error {:.2e}".format(before, after,
                              (float(before) / after) if after else 0.0, worst))
    # ★The error is the whole justification for calling this lossless -- so it has to
    # be able to FAIL.★ A plane-only rotation test once produced 1.39 (on components
    # bounded to [-1,1]) and still sailed through the keyframe-cap check, i.e. a
    # visibly broken shape that looked like a pass.
    BUDGET = 2.0e-3
    if worst > BUDGET:
        log("!! decimation error {:.2e} exceeds {:.1e} -- keys were removed that linear/"
            "slerp interpolation does NOT reproduce. Refusing to ship this.".format(
                worst, BUDGET))
        raise SystemExit(11)


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
    src, dst, one_lod = parse_args()
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

    if one_lod:
        collapse_to_one_lod()

    set_linear_interpolation()

    ranges = sequence_ranges()
    log("DTS sequences from timeline markers: {}".format(
        [(n, s, e) for n, s, e in ranges]))
    if not ranges:
        log("!! no sequence markers found -- the GLB would carry node-named clips,")
        log("!! which the engine cannot bind (see this file's header). Aborting.")
        return 6

    parsed = dts_sequence_owner_counts(src)
    if parsed is None:
        log("  !! sequence table unreadable -- every node will own every sequence,")
        log("  !! which lets the view thread ('looks') pin nodes the walk should drive,")
        log("  !! and clip durations will come from frame counts rather than the .dts.")
        count_of, dur_of, owned = {}, {}, None
        durations = None
        cyclic_of, prio_of = {}, {}
    else:
        counts, durations, owned, seq_names, cyclics, priorities = parsed
        # Key everything by sequence NAME.  Marker INDEX is not a safe key: this
        # shape has 44 sequences and 43 timeline markers, so one unmarked sequence
        # shifts every index after it.
        if seq_names:
            count_of = dict(zip(seq_names, counts))
            dur_of = dict(zip(seq_names, durations))
            cyclic_of = dict(zip(seq_names, cyclics))
            prio_of = dict(zip(seq_names, priorities))
        else:
            count_of, dur_of, cyclic_of, prio_of = {}, {}, {}, {}
    # ★Pick the sampling rate against BOTH ends of the squeeze.★
    #
    # Too low and a short sequence collapses: retiming at Blender's default 24 fps
    # reduced tr_talon's 0.067s sequences to 1.6 frames and Blender exported nothing
    # for them, so the clip and its NAME vanished.
    #
    # Too high and the SHAPE overflows.  TS::Shape narrowed its keyframe and
    # transform indices to Int16 at version 8, so ts_gltf.cpp caps them
    # (MaxKeyframes 32000, MaxTransforms 65000) and DROPS the remaining animation
    # once past it.  ts_gltf builds one subsequence per (node, sequence) with one
    # keyframe -- and one transform -- per sampled time, so cost is
    # owners x duration x fps summed over sequences.  rpgmalehuman.dts has 44
    # sequences over ~1730 node-sequence pairs: at 240 fps that is 678,946
    # keyframes, 21x the cap, and almost every animation would have been silently
    # truncated.  The .dts itself holds only 8,635.
    #
    # So: take the HIGHEST rate whose estimated cost fits the budget, and say what
    # was traded if the shortest sequence still cannot get 2 frames.
    # Choose the rate for FIDELITY, not for size.  Enough frames that even the
    # shortest sequence survives (a clip that samples to nothing is dropped by
    # Blender and loses its NAME) and that retiming to the authored duration
    # quantises cleanly.  Size is handled afterwards by decimate_glb_animations(),
    # which removes the redundancy this introduces -- so under-sampling would trade
    # fidelity for nothing.  Sampling down was tried first and wrecked durations:
    # at the 10 fps a 28000-keyframe budget allowed, rpgmalehuman's 'run' came out
    # 0.600s against an authored 0.667s and a 0.033s clip stretched to 0.300s.
    real = [d for d in dur_of.values() if d and d > 0.0]
    shortest = min(real) if real else 0.0
    fps = 24.0
    if real:
        fps = min(240.0, max(24.0, math.ceil(8.0 / shortest)))
    bpy.context.scene.render.fps = int(fps)
    bpy.context.scene.render.fps_base = 1.0
    log("sampling at {} fps (shortest sequence {:.3f}s -> {:.0f} frames); "
        "decimation removes the redundancy afterwards".format(
            int(fps), shortest, shortest * fps))
    build_nla_tracks(ranges, count_of, dur_of, fps, owned)
    flip_winding_to_gltf_ccw()
    # Strips now start at frame 0, and the exporter bakes each track over its own
    # strip range -- so the scene range has to include 0 or the first key is cut.
    bpy.context.scene.frame_start = 0

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
            # ★Blender DISCARDS an animation whose every channel is constant.★ That
            # is what silently ate Seq12_RLeg all along, and then took root / crouch
            # root / fall / Seq11_LLeg the moment the trailing transition frame was
            # removed and they became genuinely static.  A static sequence is
            # perfectly legal in DTS -- 'root' IS the idle pose, and 'looks' carries
            # no tracks at all -- and losing one loses its NAME, which is how the
            # engine binds sequences.  A missing "looks" crashes the client outright
            # (player.cpp:388 -> :700).  So keep constant channels AND constant
            # object animations.
            export_optimize_animation_size=False,
            export_optimize_animation_keep_anim_object=True,
            # Asking for sparse keys rather than baked frames.  ★MEASURED: this has
            # NO EFFECT in NLA_TRACKS mode -- output was byte-identical.★ NLA
            # evaluation requires baking, so Blender samples regardless.  Left in
            # because it is the correct intent and costs nothing, but it does NOT
            # solve the keyframe-cap problem: see the KF_BUDGET note above and the
            # SEQUENCE-COUNT CEILING in this file's header.
            export_force_sampling=False,
        )
    except Exception:
        log("!! EXPORT RAISED:")
        traceback.print_exc()
        return 4

    if not os.path.exists(dst):
        log("!! export produced no file")
        return 5

    decimate_glb_animations(dst)
    # Both post-passes run AFTER decimation, which rewrites the whole container --
    # anything written earlier would be dropped when the JSON chunk is rebuilt.
    # Reorder BEFORE the extras so the extras land on the final animation list.
    reorder_animations(dst, seq_names if parsed else None)
    inject_sequence_extras(dst, cyclic_of, prio_of)
    inject_material_extras(dst, dts_material_maps(src))

    names, js, raw, bin_off = glb_animation_names(dst)
    log("---- GLB ----")
    log("  size       : {} bytes".format(os.path.getsize(dst)))
    log("  nodes      : {}".format(len(js.get('nodes', []))))
    log("  meshes     : {}".format(len(js.get('meshes', []))))
    log("  ANIMATIONS : {}  {}".format(len(names or []), names))

    # Every clip must START at ~0.  ts_gltf.cpp takes fDuration from the sampler's
    # MAX time and normalises keys as time/duration, so a late-starting clip spends
    # most of its cycle frozen on the first pose and then blips.
    acc = js.get('accessors', [])
    late = []
    for a in js.get('animations', []):
        lo = None
        for s in a.get('samplers', []):
            mn = acc[s['input']].get('min', [None])[0]
            if mn is not None:
                lo = mn if lo is None else min(lo, mn)
        hi = None
        for s in a.get('samplers', []):
            mx = acc[s['input']].get('max', [None])[0]
            if mx is not None:
                hi = mx if hi is None else max(hi, mx)
        if lo is not None:
            want = dur_of.get(a.get('name'), 0.0)
            log("    clip {:<14} {:.3f}..{:.3f}s   .dts says {:.3f}s  {}".format(
                a.get('name'), lo, hi or 0.0, want,
                "OK" if want <= 0 or abs((hi or 0.0) - want) < 0.05 else "SPEED MISMATCH"))
            if lo > 0.05:
                late.append((a.get('name'), lo))
    if late:
        log("!! FAIL: {} clip(s) do not start at 0 -- they will freeze then blip "
            "in game: {}".format(len(late), late))
        return 9

    # ★Hard cap check on the FINAL file.★ ts_gltf builds one TS::Shape keyframe and
    # one transform per sampled time, per (node, sequence); version 8 narrowed those
    # indices to Int16 so the loader stops at 32000 and DROPS the rest -- silently,
    # as far as the player is concerned.  Count what it will actually build.
    kf_total = 0
    for a in js.get('animations', []):
        per_node = {}
        for ch in a.get('channels', []):
            nd = ch.get('target', {}).get('node')
            cnt = acc[a['samplers'][ch['sampler']]['input']]['count']
            per_node[nd] = max(per_node.get(nd, 0), cnt)
        kf_total += sum(per_node.values())
    log("  keyframes   : {} that ts_gltf will build (MaxKeyframes 32000, "
        "MaxTransforms 65000)".format(kf_total))
    if kf_total > 32000:
        log("!! FAIL: over the Int16 keyframe cap -- ts_gltf would log \"animation data "
            "exceeds the Int16 keyframe/transform indices\" and drop the remainder.")
        return 10

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
