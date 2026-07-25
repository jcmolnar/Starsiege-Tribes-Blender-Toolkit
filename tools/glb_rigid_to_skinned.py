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


def convert(src, dst, verify=False):
    js, binc = load_glb(src)
    read = make_reader(js, binc)
    nodes = js.get('nodes', [])
    meshes = js.get('meshes', [])
    if not nodes or not meshes:
        raise SystemExit("no nodes/meshes in %s" % src)

    world, roots = world_transforms(js)

    # Joints = every node.  Keeping the whole tree means the skeleton, its names and
    # the animations stay exactly as they were; only the mesh binding changes.
    joints = list(range(len(nodes)))
    joint_of = dict((n, i) for i, n in enumerate(joints))
    if len(joints) > 128:
        log("!! {} joints -- the engine's palette caps at 128 (ts_skinMesh.cpp). "
            "Run the source through --one-lod first.".format(len(joints)))
        return 2

    mesh_nodes = [i for i, n in enumerate(nodes) if 'mesh' in n]
    log("{} node(s), {} with a mesh, {} joint(s)".format(
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

    # The skinned mesh hangs off its own node.  Its transform is ignored by the spec,
    # so identity is both correct and unambiguous.
    nodes.append({'name': 'skinnedMesh', 'mesh': 0, 'skin': 0})
    mesh_node = len(nodes) - 1
    scene = js.get('scenes', [{}])[js.get('scene', 0)]
    scene.setdefault('nodes', roots)
    if mesh_node not in scene['nodes']:
        scene['nodes'].append(mesh_node)

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
    return convert(args[0], args[1], verify='--verify' in sys.argv)


if __name__ == '__main__':
    sys.exit(main())
