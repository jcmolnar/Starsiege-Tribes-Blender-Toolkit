r"""Add the Tribes player utility nodes a shape needs to be worn as an Armor.

Tribes' Player looks up a handful of nodes by name and uses them for weapon
mounting, the eye/camera, and the view-pitch override.  A shape imported from
another Darkstar game (a Starsiege Herc, say) has none of them.  Nothing
crashes without them -- every dereference site is guarded (see NOT_A_CRASH) --
but weapons will not mount, the eye sits at the object origin, and the torso
will not pitch with the view.  This tool appends the missing nodes.

What Player wants (program/code/player.cpp):
  "dummyalways root"      findNode, line 367  -> getTransform(rnode)
  "dummyalways chasecam"  findNode, line 487
  "dummy eye"             getNodeAtCurrentDetail, line 488
  "dummy hand"            \  getNodeAtCurrentDetail, line 493, one per
  "dummy unused"           \ MountPoint (PrimaryMount, SecondaryMount,
  "dummy midback"          / BackpackMount, JetExhaust)
  "dummy lowback"         /
  "lowerback"             insertOverride, line 369 -- view pitch

getNodeAtCurrentDetail (engine/Ts3/code/ts_shapeInst.cpp:2560) appends the
detail's SIZE to the name, so detail 0 of a shape whose top LOD is size 64
is asked for "dummy hand64" -- not "dummy hand0".  The per-detail nodes are
therefore named by size, whatever the shape's own LOD suffix convention is.

NOT_A_CRASH: a missing name yields -1, and both getTransform(int) and
getNode(int) are unbounded (ts_shapeInst.h:670 / :641).  But every place the
result is actually dereferenced is guarded by `if (node != -1)`
(player.cpp:1359, playerInventory.cpp:556 and :630), and the one unguarded
read -- getNode(mountNode[i]) at player.cpp:498 -- only dereferences `node`
inside `if (dn != -1)`, which cannot be true when the detail-0 lookup already
failed.  Stock Tribes proves this: larmor.dts has no "dummy unused" at all.

Usage:
    python tools/inject_player_nodes.py in.dts -o out.dts
    python tools/inject_player_nodes.py in.dts -o out.dts --pitch-node head
"""
import argparse
import os
import struct
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TOOLS_DIR))
sys.path.insert(0, _TOOLS_DIR)

from kaitaistruct import KaitaiStream, BytesIO   # noqa: E402
from dts import Dts                               # noqa: E402

NODE_SIZE = 20      # Nodev7:  name u4, parent s4, nSub u4, firstSub u4, dt u4
XFORM_SIZE = 32     # Transformv7: Quat16(8) + Point3F(12) + Point3F(12)
NAME_SIZE = 24

# per-detail nodes: (base name, parent-preference by name prefix, translate)
# translate is optional (default identity), in the PARENT's local space.
# The eye is nudged forward+up out of the cockpit shell so first person looks
# out over the mech's front instead of into interior geometry (cockpit/head
# nodes are identity-oriented, so +Y == world forward).
EYE_OFFSET = (0.0, 2.5, 1.0)
PER_DETAIL = [
    ('dummy hand',    ('right_pod', 'left_pod', 'head'), None),
    ('dummy unused',  ('left_pod', 'right_pod', 'head'), None),
    ('dummy midback', ('head',), None),
    ('dummy lowback', ('pelvis',), None),
    ('dummy eye',     ('cockpit', 'head'), EYE_OFFSET),
]


def clean(b):
    return b.split(b'\x00')[0].decode('latin-1', 'replace')


class ShapeV7:
    """Just enough structure to append nodes/transforms/names to a v7 shape."""

    def __init__(self, buf):
        self.buf = buf
        d = Dts(KaitaiStream(BytesIO(buf)))
        self.version = d.shape.data.version
        if self.version != 7:
            raise ValueError('expected a version-7 shape, got %d' % self.version)
        self.sh = d.shape.data.obj_data

        p = 8                                   # 'PERS' + size
        clen = struct.unpack_from('<H', buf, p)[0]
        p += 2 + ((clen + 1) & ~1) + 4          # classname + pad + version
        self.hdr = p                            # first count lives here
        s = self.sh
        self.off_nodes = p + 11 * 4 + 4 + 12    # counts + radius + center
        self.off_seq = self.off_nodes + s.num_nodes * NODE_SIZE
        self.off_subseq = self.off_seq + s.num_seq * 32
        self.off_kf = self.off_subseq + s.num_subseq * 12
        self.off_xf = self.off_kf + s.num_keyframes * 12
        self.off_names = self.off_xf + s.num_transforms * XFORM_SIZE
        self.off_objects = self.off_names + s.num_names * NAME_SIZE
        self.off_details = self.off_objects + s.num_objects * 72  # Objectv7 = 72

        # prove the layout arithmetic before we rewrite anything
        for i, parsed in enumerate(s.names):
            got = buf[self.off_names + i * NAME_SIZE:
                      self.off_names + (i + 1) * NAME_SIZE]
            if got != parsed:
                raise ValueError('layout check failed at name %d: %r != %r'
                                 % (i, clean(got), clean(parsed)))
        self.names = [clean(n) for n in s.names]
        self.nodes = s.nodes_v7

    # ---------------------------------------------------------------
    def node_name(self, i):
        return self.names[self.nodes[i].name]

    def subtree(self, root):
        """Node indices in the subtree rooted at `root` (inclusive)."""
        out, stack = [], [root]
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(j for j, nd in enumerate(self.nodes) if nd.parent == n)
        return out

    def find_in(self, indices, prefixes):
        """First node under `indices` whose name starts with any prefix."""
        for pref in prefixes:
            for i in indices:
                if self.node_name(i).lower().startswith(pref):
                    return i
        return None

    def strip_sequence_tracks(self, seq_name):
        """Remove a sequence's transform tracks from every NODE.

        Returns (new_bytes, tracks_removed). The sequence itself stays (the
        engine AssertFatal's if "looks" is missing, player.cpp:385) but no node
        subscribes to it. Needed because Tribes plays the "looks" aim sequence
        on a high-priority viewThread; `UpdateSequenceSubscriberLists`
        (ts_shapeInst.cpp:2726) registers each node under every sequence its
        subsequences name, and Ts3 then gives the node to the highest-priority
        subscribed thread. A Herc's full-skeleton "looks" subscribes all 48
        nodes, so viewThread clamps the whole body and the run cycle plays
        invisibly underneath. Removing "looks" from the node blocks hands every
        node back to the movement thread.

        Implemented as an array rebuild, because the subsequence array is SHARED
        with OBJECTS (mesh frame-track / visibility animation): objects index it
        via their own first_subsequence/num_subsequences. So we rebuild the
        array as (each node's filtered block) ++ (each object's full block) and
        repoint BOTH node and object records. Every surviving subsequence keeps
        its (sequence_index, num_keyframes, first_keyframe) intact -- keyframes
        and transforms are untouched -- so all indices stay valid, and
        sequence_index never goes out of [0, num_seq) (an earlier relabel-to-
        num_seq crashed in UpdateSequenceSubscriberLists' OOB list index).
        """
        buf = self.buf
        s = self.sh
        names = self.names
        seq_idx = next((i for i, q in enumerate(s.sequences)
                        if names[q.name] == seq_name), None)
        if seq_idx is None:
            return buf, 0

        SUBSEQ_SIZE = 12
        OBJ_SIZE = 72          # Objectv7: name/flags/mesh/node/tmat3f + nSub/firstSub
        OBJ_NSUB_OFF = 64      # num_subsequences within an object record
        subs = s.subsequences_v7
        objects = s.objects_v7 if getattr(s, 'objects_v7', None) else s.objects

        new_subs = bytearray()
        node_bytes = bytearray(buf[self.off_nodes:self.off_seq])
        removed = 0

        for i, n in enumerate(self.nodes):
            new_first = len(new_subs) // SUBSEQ_SIZE
            kept = 0
            for k in range(n.num_subsequences):
                ss = subs[n.first_subsequence + k]
                if ss.sequence_index == seq_idx:
                    removed += 1
                    continue
                new_subs += struct.pack('<III', ss.sequence_index,
                                        ss.num_keyframes, ss.first_keyframe)
                kept += 1
            struct.pack_into('<II', node_bytes, i * NODE_SIZE + 8, kept, new_first)

        # objects keep ALL their subsequences (they carry frame-track/visibility
        # tracks); just relocate them and repoint. Objects live in the tail,
        # after the keyframe section.
        tail = bytearray(buf[self.off_kf:])
        obj_rel = self.off_objects - self.off_kf
        for j, o in enumerate(objects):
            new_first = len(new_subs) // SUBSEQ_SIZE
            for k in range(o.num_subsequences):
                ss = subs[o.first_subsequence + k]
                new_subs += struct.pack('<III', ss.sequence_index,
                                        ss.num_keyframes, ss.first_keyframe)
            struct.pack_into('<II', tail, obj_rel + j * OBJ_SIZE + OBJ_NSUB_OFF,
                             o.num_subsequences, new_first)

        out = bytearray()
        out += buf[:self.off_nodes]                 # header
        out += node_bytes                           # nodes (repointed)
        out += buf[self.off_seq:self.off_subseq]    # sequences (unchanged)
        out += new_subs                             # rebuilt subsequences
        out += tail                                 # keyframes .. end (objs repointed)

        struct.pack_into('<I', out, self.hdr + 8, len(new_subs) // SUBSEQ_SIZE)
        struct.pack_into('<I', out, 4, len(out) - 8)   # PERS payload size
        return bytes(out), removed

    # ---------------------------------------------------------------
    def force_full_lod(self):
        """Make every render LOD draw the highest-detail mesh.

        Returns (new_bytes, details_repointed). Starsiege shapes ship 5 render
        LODs (sizes 64/32/16/8/4) whose lower levels drop meshes -- the Herc's
        LOD3/4 have no cockpit. Tribes picks a LOD by projected size, so at a
        distance the engine renders a cockpit-less level and the top appears to
        vanish. Modern GPUs don't need the LOD reduction, so point every
        positive-size detail's root node at detail 0's root: the engine renders
        `getDetail(dl) = fNodeInstanceList[fDetails[dl].fRootNodeIndex]` and
        recurses that subtree (ts_shapeInst.cpp:2600/669), so all LODs now draw
        the full detail-0 mesh.

        Detail SIZES are left unchanged so selectDetail still always picks a
        level (nothing renders blank at distance) and getNodeAtCurrentDetail --
        which builds mount-node names from int(size) -- still resolves; mount
        nodes stay correct because Player::initResources patches each lower
        detail's mount-node transform to detail 0's at load. Size<=0 details
        (cam / collision) are left alone.
        """
        buf = self.buf
        s = self.sh
        details = s.details
        if not details:
            return buf, 0
        DET_SIZE = 8               # Detail: root_node_index u4, size f4
        root0 = details[0].root_node_index
        out = bytearray(buf)
        repointed = 0
        for d, det in enumerate(details):
            if det.size > 0 and det.root_node_index != root0:
                struct.pack_into('<I', out, self.off_details + d * DET_SIZE, root0)
                repointed += 1
        return bytes(out), repointed

    # ---------------------------------------------------------------
    def build(self, new_nodes, renames=None):
        """new_nodes: [(name, parent_index)] or [(name, parent_index, translate)].

        Nodes with no translate (or None) share one appended identity
        transform; nodes with a translate (x, y, z) each get their own appended
        transform. New nodes carry no subsequences, so no animation data is
        rewritten.
        """
        buf = self.buf
        s = self.sh
        n_new = len(new_nodes)

        def xf_bytes(t):
            return (struct.pack('<4h', 0, 0, 0, 32767) +   # identity Quat16
                    struct.pack('<3f', t[0], t[1], t[2]) +  # translate
                    struct.pack('<3f', 1.0, 1.0, 1.0))      # unit scale

        # Build the appended transform pool: one shared identity, plus one per
        # node that asks for a specific translate.
        identity_index = s.num_transforms
        xform_blob = bytearray(xf_bytes((0.0, 0.0, 0.0)))
        next_index = identity_index + 1

        name_bytes = bytearray()
        node_bytes = bytearray()
        for k, entry in enumerate(new_nodes):
            nm, parent = entry[0], entry[1]
            translate = entry[2] if len(entry) > 2 else None
            if len(nm) > NAME_SIZE - 1:
                raise ValueError('name too long: %r' % nm)
            if translate is None:
                dt = identity_index
            else:
                dt = next_index
                xform_blob += xf_bytes(translate)
                next_index += 1
            name_bytes += nm.encode('latin-1').ljust(NAME_SIZE, b'\x00')
            node_bytes += struct.pack('<IiIII',
                                      s.num_names + k,   # name index
                                      parent,            # parent node
                                      0, 0,              # no subsequences
                                      dt)                # default transform
        n_new_xf = next_index - identity_index

        # Each array is contiguous, so grow them in place:
        #   header | nodes +NEW | seq..keyframes | transforms +NEW |
        #   names +NEW | objects..meshes
        out = bytearray()
        out += buf[:self.off_seq]          # header + existing nodes
        out += node_bytes                  # new nodes
        out += buf[self.off_seq:self.off_names]   # seq, subseq, keyframes, xforms
        out += xform_blob                  # appended transforms
        out += buf[self.off_names:self.off_objects]   # existing names
        out += name_bytes                  # new names
        out += buf[self.off_objects:]      # objects, details, ..., meshes, mats

        # fix the counts and the PERS payload size
        struct.pack_into('<I', out, self.hdr + 0, s.num_nodes + n_new)
        struct.pack_into('<I', out, self.hdr + 16, s.num_transforms + n_new_xf)
        struct.pack_into('<I', out, self.hdr + 20, s.num_names + n_new)
        struct.pack_into('<I', out, 4, len(out) - 8)     # PERS payload size

        if renames:
            base = self.off_names + len(node_bytes) + len(xform_blob)
            for idx, newname in renames:
                struct.pack_into('<24s', out, base + idx * NAME_SIZE,
                                 newname.encode('latin-1').ljust(NAME_SIZE, b'\x00'))
        return bytes(out)


def plan(shape, pitch_node=None, cam_height=None):
    """Work out which nodes to add (and any renames) for this shape.

    cam_height: world Z (metres) for the third-person chase camera pivot.
    Player::getCameraTransform (player.cpp:1336) reads the chasecam node's
    world transform, ZEROES x/y, and uses z as the orbit height, then pushes
    back by camDist. At z=0 (a plain injected node) the camera sits on the
    ground looking up; setting it to ~upper-body height frames the model.
    """
    s = shape.sh
    existing = {shape.node_name(i).lower() for i in range(len(shape.nodes))}
    new_nodes = []
    renames = []
    notes = []

    # one-off nodes, parented to node 0 (the shape root, "bounds").
    # chasecam gets a Z lift so the third-person camera isn't at the feet.
    for nm in ('dummyalways root', 'dummyalways chasecam'):
        if nm.lower() in existing:
            notes.append('%s already present' % nm)
            continue
        if nm == 'dummyalways chasecam' and cam_height:
            new_nodes.append((nm, 0, (0.0, 0.0, float(cam_height))))
            notes.append('chasecam pivot z = %.2f' % cam_height)
        else:
            new_nodes.append((nm, 0))

    # per-detail nodes, named by DETAIL SIZE (see module docstring)
    for di, det in enumerate(s.details):
        size = int(det.size)
        if size <= 0:
            continue                      # utility/collision details
        root = det.root_node_index
        sub = shape.subtree(root)
        for base, prefs, translate in PER_DETAIL:
            nm = '%s%d' % (base, size)
            if nm.lower() in existing:
                continue
            parent = shape.find_in(sub, prefs)
            if parent is None:
                parent = root
            if translate is None:
                new_nodes.append((nm, parent))
            else:
                new_nodes.append((nm, parent, translate))

        if pitch_node:
            # rename an existing node so insertOverride("lowerback",..) finds
            # it -- that is what pitches the upper body with the view.
            target = shape.find_in(sub, (pitch_node.lower(),))
            if target is not None:
                cur = shape.node_name(target)
                if not cur.lower().startswith('lowerback'):
                    renames.append((shape.nodes[target].name, 'lowerback%d' % size))
                    notes.append('pitch: %s -> lowerback%d' % (cur, size))
    return new_nodes, renames, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--pitch-node', default=None,
                    help='rename this node (by name prefix, e.g. "head") to '
                         'lowerback<size> so the view-pitch override applies '
                         'to the upper body')
    args = ap.parse_args()

    buf = open(args.input, 'rb').read()
    shape = ShapeV7(buf)
    print('%s: version %d, %d nodes, %d transforms, %d names'
          % (os.path.basename(args.input), shape.version,
             shape.sh.num_nodes, shape.sh.num_transforms, shape.sh.num_names))

    new_nodes, renames, notes = plan(shape, args.pitch_node)
    for n in notes:
        print('  note: %s' % n)
    print('  adding %d nodes:' % len(new_nodes))
    for nm, parent in new_nodes:
        print('    %-24s parent=%s' % (nm, shape.node_name(parent)))

    out = shape.build(new_nodes, renames)
    with open(args.output, 'wb') as f:
        f.write(out)
    print('  wrote %s (%d -> %d bytes)' % (args.output, len(buf), len(out)))

    # --- verify by re-parsing and resolving exactly what the engine asks for
    chk = ShapeV7(out)
    cs = chk.sh
    print('  verify: %d nodes, %d transforms, %d names, %d meshes parsed'
          % (cs.num_nodes, cs.num_transforms, cs.num_names,
             len(Dts(KaitaiStream(BytesIO(out))).meshes)))
    have = {chk.node_name(i).lower(): i for i in range(len(chk.nodes))}
    ok = True
    wanted = ['dummyalways root', 'dummyalways chasecam']
    for det in cs.details:
        if int(det.size) > 0:
            for entry in PER_DETAIL:
                wanted.append('%s%d' % (entry[0], int(det.size)))
    for nm in wanted:
        hit = have.get(nm.lower())
        if hit is None:
            print('    MISSING %s' % nm)
            ok = False
    print('    all %d engine-requested names resolve: %s' % (len(wanted), ok))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
