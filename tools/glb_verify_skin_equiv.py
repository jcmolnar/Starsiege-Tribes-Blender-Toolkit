#!/usr/bin/env python3
"""Prove the rigid/skinned equivalence UNDER ANIMATION, not just at rest.

glb_rigid_to_skinned.py claims the equality is "exact by construction". This checks
it, because a construction argument is exactly the sort of thing that is wrong in a
way you only find by evaluating it.

For a sampled animation time t:
    rigid_pos   = worldAnim_node(t)          . localPos
    skinned_pos = worldAnim_joint(t) . invBind_joint . scenePos
and those must agree for every vertex.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', '..', '..', '..'))
sys.path.insert(0, r"C:\Users\Joe\Tribes DTS Blender\tools")

from glb_rigid_to_skinned import (load_glb, make_reader, world_transforms,
                                  node_local, m_mul, m_ident, m_from_trs,
                                  xform_point, render_detail_subtree)


def sample_channel(times, vals, t, is_rot):
    """LINEAR sample; slerp for rotations (glTF 3.6.2.3)."""
    if not times:
        return None
    if t <= times[0]:
        return list(vals[0])
    if t >= times[-1]:
        return list(vals[-1])
    i = 0
    while i + 1 < len(times) and times[i + 1] < t:
        i += 1
    t0, t1 = times[i], times[i + 1]
    f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
    a, b = list(vals[i]), list(vals[i + 1])
    if is_rot:
        import math
        d = sum(a[k] * b[k] for k in range(4))
        if d < 0:
            b = [-x for x in b]; d = -d
        if d > 0.9995:
            r = [a[k] + (b[k] - a[k]) * f for k in range(4)]
        else:
            th = math.acos(max(-1.0, min(1.0, d)))
            s = math.sin(th)
            w1, w2 = math.sin((1 - f) * th) / s, math.sin(f * th) / s
            r = [a[k] * w1 + b[k] * w2 for k in range(4)]
        n = math.sqrt(sum(x * x for x in r)) or 1.0
        return [x / n for x in r]
    return [a[k] + (b[k] - a[k]) * f for k in range(len(a))]


def animated_world(js, read, anim_idx, t):
    """World matrix per node at time t under animation anim_idx."""
    nodes = js['nodes']
    trs = {}
    anim = js['animations'][anim_idx]
    for ch in anim.get('channels', []):
        tgt = ch.get('target', {})
        ni, path = tgt.get('node'), tgt.get('path')
        if ni is None or path not in ('translation', 'rotation', 'scale'):
            continue
        s = anim['samplers'][ch['sampler']]
        times = [x[0] for x in read(s['input'])]
        vals = read(s['output'])
        v = sample_channel(times, vals, t, path == 'rotation')
        if v is not None:
            trs.setdefault(ni, {})[path] = v

    def local(i):
        n = nodes[i]
        if i in trs:
            tt = trs[i].get('translation', n.get('translation', [0, 0, 0]))
            rr = trs[i].get('rotation', n.get('rotation', [0, 0, 0, 1]))
            ss = trs[i].get('scale', n.get('scale', [1, 1, 1]))
            return m_from_trs(tt, rr, ss)
        return node_local(n)

    world = [None] * len(nodes)
    child = set()
    for n in nodes:
        for c in n.get('children', []):
            child.add(c)
    roots = [i for i in range(len(nodes)) if i not in child]
    stack = [(r, m_ident()) for r in roots]
    while stack:
        i, par = stack.pop()
        w = m_mul(par, local(i))
        world[i] = w
        for c in nodes[i].get('children', []):
            stack.append((c, w))
    return [w if w else m_ident() for w in world]


def main(rigid_path, skinned_path):
    rjs, rbin = load_glb(rigid_path)
    sjs, sbin = load_glb(skinned_path)
    rread, sread = make_reader(rjs, rbin), make_reader(sjs, sbin)

    nanim = len(rjs.get('animations', []))
    print("animations: %d" % nanim)
    if not nanim:
        print("no animations to test"); return 1

    skin = sjs['skins'][0]
    joints = skin['joints']
    ibm = sread(skin['inverseBindMatrices'])

    overall = 0.0
    total = 0
    # a few clips, a few times each -- including a time strictly BETWEEN keys, which
    # is where an interpolation-order mistake would show and an endpoint test would not
    for ai in range(min(nanim, 4)):
        for frac in (0.0, 0.137, 0.5, 0.813, 1.0):
            # duration = max input time of this animation
            dur = 0.0
            for s in rjs['animations'][ai].get('samplers', []):
                ts = [x[0] for x in rread(s['input'])]
                if ts:
                    dur = max(dur, ts[-1])
            t = dur * frac
            rworld = animated_world(rjs, rread, ai, t)
            sworld = animated_world(sjs, sread, ai, t)

            # rigid: per mesh-node, local verts through that node's animated world
            rigid_pts = []
            _, subtree = render_detail_subtree(rjs)
            for ni, n in enumerate(rjs['nodes']):
                if 'mesh' not in n:
                    continue
                if subtree is not None and ni not in subtree:
                    continue          # collision hull / lower LODs are not merged
                for prim in rjs['meshes'][n['mesh']].get('primitives', []):
                    if prim.get('mode', 4) != 4 or 'POSITION' not in prim.get('attributes', {}):
                        continue
                    for p in rread(prim['attributes']['POSITION']):
                        rigid_pts.append(xform_point(rworld[ni], p))

            # skinned: scene-space verts through palette = worldAnim_j . invBind_j
            skin_pts = []
            for prim in sjs['meshes'][0]['primitives']:
                at = prim['attributes']
                pos = sread(at['POSITION'])
                jt = sread(at['JOINTS_0'])
                wt = sread(at['WEIGHTS_0'])
                # ★Sum ALL FOUR influences.★  An earlier version read only
                # JOINTS_0[k][0] and ignored the weights, which silently reduced every
                # comparison to the single-influence case -- so a genuinely BLENDED
                # asset measured as identical to rigid and looked like a no-op.  A
                # verifier that cannot see the thing under test reports success.
                for k in range(len(pos)):
                    acc = [0.0, 0.0, 0.0]
                    wsum = 0.0
                    for c in range(4):
                        w = wt[k][c]
                        if w <= 0.0:
                            continue
                        pal = m_mul(sworld[joints[jt[k][c]]], list(ibm[jt[k][c]]))
                        q = xform_point(pal, pos[k])
                        for e in range(3):
                            acc[e] += w * q[e]
                        wsum += w
                    if wsum > 1e-6 and abs(wsum - 1.0) > 1e-4:
                        acc = [a / wsum for a in acc]
                    skin_pts.append(tuple(acc))

            if len(rigid_pts) != len(skin_pts):
                print("  anim %d t=%.3f  COUNT MISMATCH %d vs %d"
                      % (ai, t, len(rigid_pts), len(skin_pts)))
                return 2
            worst = 0.0
            for a, b in zip(rigid_pts, skin_pts):
                worst = max(worst, max(abs(a[c] - b[c]) for c in range(3)))
            overall = max(overall, worst)
            total += len(rigid_pts)
            print("  anim %-2d t=%-7.3f verts=%-6d worst=%.3e" % (ai, t, len(rigid_pts), worst))

    print()
    print("checked %d vertex comparisons, worst deviation %.3e" % (total, overall))
    ok = overall < 1e-3
    print("PASS: rigid and skinned agree under animation" if ok
          else "FAIL: they diverge -- the construction argument is WRONG")
    return 0 if ok else 3


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
