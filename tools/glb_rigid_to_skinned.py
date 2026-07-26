#!/usr/bin/env python3
"""glb_rigid_to_skinned.py -- turn a RIGID .glb into an EXACTLY EQUIVALENT skinned one.

Usage:
  python tools/glb_rigid_to_skinned.py <in.glb> <out.glb> [--verify]

★Why this exists.★  The engine's new TS::SkinMesh path (native Phase 3) has never run
on real data, because nothing is rigged: a .dts binds each mesh to exactly ONE node
(ts_shape.h:227-249), so every Tribes character is a bag of rigid parts and Blender
exports it with no skin at all -- the loader takes the CelAnimMesh path and SkinMesh
is never constructed.

Authoring a brand-new rigged character to test it would change four things at once
(mesh, skeleton, node NAMES -- which animData binds by -- and animations) on top of
the skinning.  When it looked wrong you would be bisecting four suspects.

So instead: take a model that ALREADY WORKS and change exactly one thing.  Join its
rigid parts into one mesh and bind every vertex with weight 1.0 to the single joint
its part was already parented to.  Skeleton, node names, sequences and animations are
untouched.

★That makes the pass criterion EXACT rather than aesthetic.★  One influence at weight
1 is mathematically the same transform the rigid path applies, so:

    skinned_pos(t) = worldAnim_j(t) . invBind_j . scenePos
                   = worldAnim_j(t) . inverse(worldRest_j) . worldRest_j . localPos
                   = worldAnim_j(t) . localPos
                   = rigid_pos(t)

identically, at EVERY pose -- not just at rest.  So the skinned build must render the
same as the rigid one, and any visible difference is a bug in the SkinMesh code and
nothing else.  --verify asserts that numerically here, before it ever reaches the game.

Once this passes, softening the weights across one joint gives the first real bend, and
any problem THERE is the weighting rather than the code.

Deliberately not done through Blender: Blender only emits a glTF skin for a mesh with
an Armature modifier, and the DTS importer produces an OBJECT hierarchy rather than an
armature -- so that route would mean synthesising an armature and migrating the
animation onto bones, which is exactly the pile of new variables this is avoiding.
Operating on the GLB directly keeps the node tree and the animations byte-identical.
"""

import json
import math
import os
import struct
import sys

CFMT = {5120: 'b', 5121: 'B', 5122: 'h', 5123: 'H', 5125: 'I', 5126: 'f'}
NC = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}


def log(m):
    print("[R2S] {}".format(m), flush=True)


# ---------------------------------------------------------------- 4x4, column-major
# glTF stores column-major: m[col*4 + row].  Everything here follows that.

def m_ident():
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def m_mul(a, b):
    """Return a . b  (apply b first, then a) in column-major storage."""
    out = [0.0] * 16
    for c in range(4):
        for r in range(4):
            s = 0.0
            for k in range(4):
                s += a[k * 4 + r] * b[c * 4 + k]
            out[c * 4 + r] = s
    return out


def m_from_trs(t, r, s):
    x, y, z, w = r
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    rot = [
        1 - 2 * (yy + zz), 2 * (xy + wz), 2 * (xz - wy), 0.0,
        2 * (xy - wz), 1 - 2 * (xx + zz), 2 * (yz + wx), 0.0,
        2 * (xz + wy), 2 * (yz - wx), 1 - 2 * (xx + yy), 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    for c in range(3):
        for r_ in range(3):
            rot[c * 4 + r_] *= s[c]
    rot[12], rot[13], rot[14] = t[0], t[1], t[2]
    return rot


def m_inverse(m):
    """General 4x4 inverse (Gauss-Jordan).  Affine in practice, but a scaled node
    chain is not orthonormal, so a rigid transpose-inverse would be WRONG here."""
    a = [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    inv = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    for col in range(4):
        piv = max(range(col, 4), key=lambda rr: abs(a[rr][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("singular node transform -- cannot invert")
        a[col], a[piv] = a[piv], a[col]
        inv[col], inv[piv] = inv[piv], inv[col]
        d = a[col][col]
        a[col] = [v / d for v in a[col]]
        inv[col] = [v / d for v in inv[col]]
        for rr in range(4):
            if rr == col:
                continue
            f = a[rr][col]
            if f == 0.0:
                continue
            a[rr] = [av - f * bv for av, bv in zip(a[rr], a[col])]
            inv[rr] = [av - f * bv for av, bv in zip(inv[rr], inv[col])]
    return [inv[r][c] for c in range(4) for r in range(4)]


def xform_point(m, p):
    return (m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
            m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
            m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14])


def _dist_point_segment(p, a, b):
    """Distance from p to the segment ab.  Used for auto-weighting: a bone is a
    SEGMENT, and measuring to the joint pivot instead makes a long bone's midpoint
    equidistant from both ends, which smears weight along the whole limb."""
    abx, aby, abz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    apx, apy, apz = p[0] - a[0], p[1] - a[1], p[2] - a[2]
    denom = abx * abx + aby * aby + abz * abz
    if denom <= 1e-12:
        return math.sqrt(apx * apx + apy * apy + apz * apz)
    t = (apx * abx + apy * aby + apz * abz) / denom
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    dx = apx - abx * t
    dy = apy - aby * t
    dz = apz - abz * t
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def xform_dir(m, p):
    return (m[0] * p[0] + m[4] * p[1] + m[8] * p[2],
            m[1] * p[0] + m[5] * p[1] + m[9] * p[2],
            m[2] * p[0] + m[6] * p[1] + m[10] * p[2])


# ---------------------------------------------------------------- GLB container

def load_glb(path):
    with open(path, 'rb') as f:
        data = f.read()
    if data[0:4] != b'glTF':
        raise SystemExit("%s is not a GLB" % path)
    off, js, binc = 12, None, b''
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from('<I4s', data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == b'JSON':
            js = json.loads(chunk.decode('utf-8'))
        elif ctype[0:3] == b'BIN':
            binc = chunk
    return js, binc


def write_glb(path, js, binc):
    j = json.dumps(js, separators=(',', ':')).encode('utf-8')
    while len(j) % 4:
        j += b' '
    b = bytes(binc)
    while len(b) % 4:
        b += b'\x00'
    body = struct.pack('<I4s', len(j), b'JSON') + j + \
           struct.pack('<I4s', len(b), b'BIN\x00') + b
    with open(path, 'wb') as f:
        f.write(struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body)


def make_reader(js, binc):
    accs, bvs = js.get('accessors', []), js.get('bufferViews', [])

    def read(ai):
        a = accs[ai]
        if 'bufferView' not in a:
            return [tuple([0] * NC[a['type']])] * a['count']
        v = bvs[a['bufferView']]
        base = v.get('byteOffset', 0) + a.get('byteOffset', 0)
        n = NC[a['type']]
        f = CFMT[a['componentType']]
        stride = v.get('byteStride') or struct.calcsize(f) * n
        return [struct.unpack_from('<' + f * n, binc, base + i * stride)
                for i in range(a['count'])]
    return read


# ---------------------------------------------------------------- the conversion

def node_local(n):
    if 'matrix' in n:
        return list(n['matrix'])
    return m_from_trs(n.get('translation', [0, 0, 0]),
                      n.get('rotation', [0, 0, 0, 1]),
                      n.get('scale', [1, 1, 1]))


def world_transforms(js):
    """Rest-pose world matrix per node."""
    nodes = js.get('nodes', [])
    world = [None] * len(nodes)
    roots = []
    scene = js.get('scenes', [{}])[js.get('scene', 0)]
    if scene.get('nodes'):
        roots = list(scene['nodes'])
    else:
        child = set()
        for n in nodes:
            for c in n.get('children', []):
                child.add(c)
        roots = [i for i in range(len(nodes)) if i not in child]

    stack = [(r, m_ident()) for r in roots]
    while stack:
        idx, parent = stack.pop()
        w = m_mul(parent, node_local(nodes[idx]))
        world[idx] = w
        for c in nodes[idx].get('children', []):
            stack.append((c, w))
    for i in range(len(world)):
        if world[i] is None:
            world[i] = m_ident()          # unreachable node: treat as root-local
    return world, roots


def trailing_int(s):
    """Mirror gltfTrailingInt (ts_gltf.cpp) so details are identified the same way
    the loader will identify them: "head0" -> 0, "dummy_baseA64" -> 64."""
    s = (s or '').rstrip()
    i = len(s)
    while i > 0 and s[i - 1].isdigit():
        i -= 1
    if i == len(s) or i == 0:
        return None
    return int(s[i:])


def render_detail_subtree(js):
    """The set of nodes in the HIGHEST-SIZE detail, plus that detail's root.

    ★This has to mirror buildDetails + the render walk exactly, or the merged mesh
    ends up somewhere the engine never draws.★  buildDetails (ts_gltf.cpp) makes a
    detail out of every trailing-int node whose ancestors carry no trailing int, sorts
    them descending by that number, and the renderer walks the chosen detail's SUBTREE
    (ShapeInstance::animate / NodeInstance::render, ts_shapeInst.cpp:506-513).

    So two things follow, and getting either wrong is silent:
      * the skinned mesh must hang off a node INSIDE that subtree, or it is invisible;
      * only meshes from THAT subtree may be merged -- tr_talon still carries a
        `coll0` collision hull as detail 0, and merging it in would draw the
        collision geometry as solid triangles.
    """
    nodes = js.get('nodes', [])
    parent = {}
    for i, n in enumerate(nodes):
        for c in n.get('children', []):
            parent[c] = i

    details = []
    for i, n in enumerate(nodes):
        size = trailing_int(n.get('name'))
        if size is None:
            continue
        p = parent.get(i)
        ancestor_is_detail = False
        while p is not None:
            if trailing_int(nodes[p].get('name')) is not None:
                ancestor_is_detail = True
                break
            p = parent.get(p)
        if not ancestor_is_detail:
            details.append((size, i))

    if not details:
        return None, None          # no LOD naming: caller merges everything

    details.sort(key=lambda d: -d[0])
    best_size, root = details[0]
    subtree = set()
    stack = [root]
    while stack:
        i = stack.pop()
        subtree.add(i)
        stack.extend(nodes[i].get('children', []))
    log("details {} -> rendering detail {} rooted at '{}' ({} node(s))".format(
        sorted([d[0] for d in details], reverse=True), best_size,
        nodes[root].get('name', '?'), len(subtree)))
    return root, subtree


def convert(src, dst, verify=False, mode='rigid', radius=1.5, falloff=4.0):
    js, binc = load_glb(src)
    read = make_reader(js, binc)
    nodes = js.get('nodes', [])
    meshes = js.get('meshes', [])
    if not nodes or not meshes:
        raise SystemExit("no nodes/meshes in %s" % src)

    world, roots = world_transforms(js)
    detail_root, detail_subtree = render_detail_subtree(js)
    parent_of = {}
    for _i, _n in enumerate(nodes):
        for _c in _n.get('children', []):
            parent_of[_c] = _i

    # Joints = every node.  Keeping the whole tree means the skeleton, its names and
    # the animations stay exactly as they were; only the mesh binding changes.
    joints = list(range(len(nodes)))
    joint_of = dict((n, i) for i, n in enumerate(joints))
    if len(joints) > 128:
        log("!! {} joints -- the engine's palette caps at 128 (ts_skinMesh.cpp). "
            "Run the source through --one-lod first.".format(len(joints)))
        return 2

    mesh_nodes = [i for i, n in enumerate(nodes) if 'mesh' in n]
    if detail_subtree is not None:
        skipped = [i for i in mesh_nodes if i not in detail_subtree]
        mesh_nodes = [i for i in mesh_nodes if i in detail_subtree]
        if skipped:
            log("skipping {} mesh node(s) outside the render detail (collision hull / "
                "lower LODs): {}".format(
                    len(skipped),
                    ', '.join(nodes[i].get('name', '?') for i in skipped[:6]) +
                    (' ...' if len(skipped) > 6 else '')))
    if not mesh_nodes:
        raise SystemExit("no mesh nodes inside the render detail")
    log("{} node(s), {} mesh node(s) merged, {} joint(s)".format(
        len(nodes), len(mesh_nodes), len(joints)))

    new_bin = bytearray()
    new_views, new_accs = [], []

    def emit(rows, ctype, gtype, mins=None, maxs=None):
        while len(new_bin) % 4:
            new_bin.append(0)
        start = len(new_bin)
        f, n = CFMT[ctype], NC[gtype]
        for r in rows:
            new_bin.extend(struct.pack('<' + f * n, *r))
        new_views.append({'buffer': 0, 'byteOffset': start,
                          'byteLength': len(new_bin) - start})
        acc = {'bufferView': len(new_views) - 1, 'componentType': ctype,
               'count': len(rows), 'type': gtype}
        if mins is not None:
            acc['min'], acc['max'] = mins, maxs
        new_accs.append(acc)
        return len(new_accs) - 1

    # ---- bone segments, for --autoweight ----------------------------------
    # One segment per joint that has a parent: parent-position -> joint-position, in
    # scene space at REST.  A joint with no parent contributes a degenerate segment at
    # its own position so it can still attract nearby vertices.
    bones = []
    bone_joint = []
    for n in joints:
        w = world[n]
        pj = parent_of.get(n)
        here = (w[12], w[13], w[14])
        if pj is not None and pj in joint_of:
            pw = world[pj]
            bones.append((here, (pw[12], pw[13], pw[14])))
        else:
            bones.append((here, here))
        bone_joint.append(joint_of[n])
    # bone adjacency (parent + children), by BONE index, for locality-limited blending
    bone_of_node = dict((n, i) for i, n in enumerate(joints))
    bone_adj = {}
    for i, n in enumerate(joints):
        adj = set()
        pj = parent_of.get(n)
        if pj is not None and pj in bone_of_node:
            adj.add(bone_of_node[pj])
        for c in nodes[n].get('children', []):
            if c in bone_of_node:
                adj.add(bone_of_node[c])
        bone_adj[i] = adj
    if mode == 'autoweight':
        log("auto-weighting against {} bone segment(s), inverse-distance^{}, blending "
            "limited to hierarchy-adjacent bones".format(len(bones), falloff))

    prims_out = []
    checked = 0
    worst = 0.0

    for ni in mesh_nodes:
        w = world[ni]
        jidx = joint_of[ni]
        for prim in meshes[nodes[ni]['mesh']].get('primitives', []):
            if prim.get('mode', 4) != 4:
                continue
            at = prim.get('attributes', {})
            if 'POSITION' not in at:
                continue
            pos = read(at['POSITION'])
            nrm = read(at['NORMAL']) if 'NORMAL' in at else None
            uv = read(at['TEXCOORD_0']) if 'TEXCOORD_0' in at else None
            idx = read(prim['indices']) if 'indices' in prim else None

            # ★Bake the node's rest transform into the vertices.★  glTF requires a
            # skinned mesh node's own transform to be IGNORED (spec 3.7.3.3), so the
            # vertices must already be in scene space -- which is also exactly what
            # makes inverse(worldRest) the right inverse-bind matrix.
            spos = [xform_point(w, p) for p in pos]
            snrm = None
            if nrm:
                snrm = []
                for n3 in nrm:
                    d = xform_dir(w, n3)
                    L = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2) or 1.0
                    snrm.append((d[0] / L, d[1] / L, d[2] / L))

            if verify:
                for k in range(len(pos)):
                    r = xform_point(w, pos[k])
                    e = max(abs(r[c] - spos[k][c]) for c in range(3))
                    worst = max(worst, e)
                    checked += 1

            mins = [min(p[c] for p in spos) for c in range(3)]
            maxs = [max(p[c] for p in spos) for c in range(3)]
            newp = {'attributes': {'POSITION': emit(spos, 5126, 'VEC3', mins, maxs)}}
            if snrm:
                newp['attributes']['NORMAL'] = emit(snrm, 5126, 'VEC3')
            if uv:
                newp['attributes']['TEXCOORD_0'] = emit(
                    [(u[0], u[1]) for u in uv], 5126, 'VEC2')
            # ---- influences ---------------------------------------------------
            if mode == 'split':
                # ★Same joint listed FOUR times at 0.25 each.★  Exercises the engine's
                # multi-influence accumulation -- the loop over MaxInfluences, the
                # weight sum and the normalisation, none of which the weight-1.0 build
                # ever ran -- while the expected output stays EXACTLY the weight-1.0
                # result.  So the pass criterion is still identity rather than
                # judgement: 0.25+0.25+0.25+0.25 of the same transform is that
                # transform.  Any difference is the accumulation loop.
                jrow, wrow = (jidx, jidx, jidx, jidx), (0.25, 0.25, 0.25, 0.25)
                newp['attributes']['JOINTS_0'] = emit([jrow] * len(spos), 5123, 'VEC4')
                newp['attributes']['WEIGHTS_0'] = emit([wrow] * len(spos), 5126, 'VEC4')
            elif mode == 'autoweight':
                # ★POSITION-ONLY weighting, and that is the whole trick.★
                #
                # A .dts character is separate rigid parts, so the vertices either side
                # of a knee are DUPLICATED -- two coincident points belonging to two
                # meshes.  Weighting by "which part did this vertex come from" gives
                # those twins different weights, so they separate under animation and
                # the limb tears (which is exactly what --blend does).
                #
                # Weight purely by the vertex's POSITION relative to the bone segments
                # and the twins get IDENTICAL weights by construction: they are at the
                # same point, so they move together, the seam holds, and the limb
                # actually bends.  No shared topology required, which is what lets a
                # 1998 part-based character become a genuinely skinned one.
                #
                # Falloff is inverse-distance-to-BONE-SEGMENT (not to the joint pivot):
                # a pivot-distance falloff makes a long bone's midpoint equidistant
                # from both ends and smears weight across the whole limb.
                # ★Blend only with bones ADJACENT IN THE HIERARCHY to the nearest one.★
                # Inverse-distance over ALL bones does not work on a humanoid: the
                # spine, chest and both shoulders are all close together, so nearly
                # every vertex finds several "near" bones and the weights smear.
                # Measured on rpgmalehuman with all 36 bones in play: median dominant
                # weight 0.477 and 93% of vertices carrying a significant second
                # influence -- a soft, melted look.  Raising the falloff exponent does
                # not fix it (even ^24 left 59% blended); the candidate SET is wrong,
                # not the curve.
                #
                # Restricting to {nearest} + {its parent} + {its children} keeps
                # blending local to a joint -- an elbow vertex blends forearm and
                # upper arm, never forearm and spine -- and stays POSITION-DERIVED, so
                # coincident twins still agree and seams still hold.
                jr, wr = [], []
                for sp in spos:
                    dists = [_dist_point_segment(sp, b0, b1) for (b0, b1) in bones]
                    nearest = min(range(len(bones)), key=lambda i: dists[i])
                    cand = set([nearest])
                    cand.update(bone_adj.get(nearest, ()))
                    scored = sorted((dists[c], c) for c in cand)[:4]
                    ws = [1.0 / ((d + 1e-4) ** falloff) for (d, _b) in scored]
                    tot = sum(ws) or 1.0
                    ws = [w / tot for w in ws]
                    while len(scored) < 4:
                        scored.append((0.0, scored[0][1]))
                        ws.append(0.0)
                    jr.append(tuple(bone_joint[b] for (_d, b) in scored))
                    wr.append(tuple(ws))
                newp['attributes']['JOINTS_0'] = emit(jr, 5123, 'VEC4')
                newp['attributes']['WEIGHTS_0'] = emit(wr, 5126, 'VEC4')
            elif mode == 'blend':
                # Real two-joint blending: vertices near this joint's pivot share
                # influence with its PARENT, so the seam bends instead of cracking.
                pj = parent_of.get(ni)
                pidx = joint_of.get(pj, jidx) if pj is not None else jidx
                pivot = (world[ni][12], world[ni][13], world[ni][14])
                jr, wr = [], []
                for sp in spos:
                    d = math.sqrt(sum((sp[c] - pivot[c]) ** 2 for c in range(3)))
                    # linear falloff: at the pivot the parent takes half, fading to
                    # nothing at `radius`.  Half rather than all, so the part stays
                    # anchored to its own joint.
                    wp = 0.5 * max(0.0, 1.0 - d / radius) if radius > 0 else 0.0
                    jr.append((jidx, pidx, 0, 0))
                    wr.append((1.0 - wp, wp, 0.0, 0.0))
                newp['attributes']['JOINTS_0'] = emit(jr, 5123, 'VEC4')
                newp['attributes']['WEIGHTS_0'] = emit(wr, 5126, 'VEC4')
            else:
                # one influence, weight 1 -- the rigid binding, expressed as skinning
                newp['attributes']['JOINTS_0'] = emit(
                    [(jidx, 0, 0, 0)] * len(spos), 5123, 'VEC4')
                newp['attributes']['WEIGHTS_0'] = emit(
                    [(1.0, 0.0, 0.0, 0.0)] * len(spos), 5126, 'VEC4')
            if idx is not None:
                newp['indices'] = emit([(i[0],) for i in idx], 5125, 'SCALAR')
            if 'material' in prim:
                newp['material'] = prim['material']
            newp['mode'] = 4
            prims_out.append(newp)

    if not prims_out:
        raise SystemExit("no usable triangle primitives found")

    # inverse-bind: scene -> joint, i.e. the inverse of the joint's REST world matrix
    ibm = []
    for n in joints:
        ibm.append(tuple(m_inverse(world[n])))
    ibm_acc = emit(ibm, 5126, 'MAT4')

    # ---- rebuild the document ------------------------------------------------
    # Everything that is NOT geometry (nodes, animations, materials, textures,
    # samplers, images) is carried over untouched; only meshes/accessors/views are
    # replaced.  Animation accessors have to be copied across into the new binary.
    old_accs = js.get('accessors', [])
    remap = {}
    for anim in js.get('animations', []):
        for s in anim.get('samplers', []):
            for key in ('input', 'output'):
                ai = s[key]
                if ai in remap:
                    s[key] = remap[ai]
                    continue
                a = old_accs[ai]
                rows = read(ai)
                na = emit(rows, a['componentType'], a['type'],
                          a.get('min'), a.get('max'))
                remap[ai] = na
                s[key] = na

    skin_mesh = {'primitives': prims_out, 'name': 'skinned'}
    js['meshes'] = [skin_mesh]

    for n in nodes:
        n.pop('mesh', None)            # former mesh nodes are now pure joints
        n.pop('skin', None)

    # ★Attach to an EXISTING node inside the render detail -- do NOT append a new one.★
    #
    # The first version of this script appended a fresh "skinnedMesh" node and added it
    # to the scene roots.  It produced a GLB that verified perfectly and was INVISIBLE
    # in game, for two independent reasons, both silent:
    #
    #   1. The renderer only walks the chosen detail's subtree, so a mesh hanging off a
    #      node outside it is simply never drawn.
    #   2. It made the scene have TWO roots, and buildNodes synthesises a stub root
    #      when there is not exactly one -- which is the documented Phase 1 failure
    #      where ROOT MOTION is lost and every move clip resolves to idle.
    #
    # Reusing the detail root avoids both: it is inside the subtree by definition, and
    # the node count and scene-root count are unchanged.  The node's own transform is
    # ignored for skinning (spec 3.7.3.3), so it does not matter which node carries the
    # mesh geometrically -- only which subtree it lives in.
    mesh_node = detail_root if detail_root is not None else mesh_nodes[0]
    nodes[mesh_node]['mesh'] = 0
    nodes[mesh_node]['skin'] = 0
    log("skinned mesh attached to existing node '{}' (index {})".format(
        nodes[mesh_node].get('name', '?'), mesh_node))

    scene = js.get('scenes', [{}])[js.get('scene', 0)]
    scene.setdefault('nodes', roots)
    if len(scene['nodes']) != 1:
        log("!! scene has {} roots -- buildNodes will synthesise a stub root and ROOT "
            "MOTION WILL BE LOST".format(len(scene['nodes'])))

    js['skins'] = [{'joints': joints, 'inverseBindMatrices': ibm_acc,
                    'name': 'rigidEquivalent'}]
    js['accessors'] = new_accs
    js['bufferViews'] = new_views
    js['buffers'] = [{'byteLength': len(new_bin)}]

    write_glb(dst, js, new_bin)

    log("wrote {} ({:,} bytes): 1 mesh, {} primitive(s), {} joint(s)".format(
        os.path.basename(dst), os.path.getsize(dst), len(prims_out), len(joints)))
    if verify:
        log("verify: {} vertices, worst rest-pose deviation {:.3e}".format(checked, worst))
        if worst > 1e-4:
            log("!! rest-pose mismatch is too large -- refusing to call this equivalent")
            return 3
        log("PASS: skinned build is rigid-equivalent at rest.")
        log("      Under animation the equality is exact BY CONSTRUCTION:")
        log("        worldAnim_j . inverse(worldRest_j) . worldRest_j . local == worldAnim_j . local")
        log("      so any in-game difference from the rigid build is a SkinMesh bug.")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(args) != 2:
        raise SystemExit(__doc__)
    mode = 'rigid'
    if '--split' in sys.argv:
        mode = 'split'
    if '--blend' in sys.argv:
        mode = 'blend'
    if '--autoweight' in sys.argv:
        mode = 'autoweight'
    radius = 1.5
    falloff = 4.0
    for a in sys.argv[1:]:
        if a.startswith('--radius='):
            radius = float(a.split('=', 1)[1])
        if a.startswith('--falloff='):
            falloff = float(a.split('=', 1)[1])
    log("mode: {}{}".format(mode, "  radius={}".format(radius) if mode == 'blend' else ""))
    return convert(args[0], args[1], verify='--verify' in sys.argv,
                   mode=mode, radius=radius, falloff=falloff)


if __name__ == '__main__':
    sys.exit(main())
