"""Diagnose per-mesh winding on a v7/v8 DTS.

For each mesh reports:
  name  nfaces  Vsign(signed volume) Vmag   centroid(out/inn)  verdict
Signed volume is robust for CLOSED meshes regardless of convexity; the
centroid dot test is what the current fix uses. Disagreements or near-zero
signed volume flag the meshes the majority-vote fix gets wrong.
"""
import sys, os
from io import BytesIO
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kaitaistruct import KaitaiStream
import dts as dtsmod


def clean(b):
    if isinstance(b, str):
        b = b.encode('latin-1', 'replace')
    return b.split(b'\x00')[0].decode('latin-1', 'replace')


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def sub(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


def dot(a, b):
    return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]


def main(path):
    buf = open(path, 'rb').read()
    d = dtsmod.Dts(KaitaiStream(BytesIO(buf)))
    sh = d.shape.data.obj_data
    objs = getattr(sh, 'objects_v7', None) or getattr(sh, 'objects', [])
    mesh_name = {}
    for o in objs:
        mesh_name.setdefault(o.mesh_index,
                             clean(sh.names[o.name]) if o.name < len(sh.names) else '?')

    print('%-16s %6s %6s %10s %12s  %s' %
          ('mesh', 'faces', 'verts', 'Vsigned', 'cen out/inn', 'verdict'))
    vols = []
    for mi, m in enumerate(d.meshes):
        nm = mesh_name.get(mi, 'mesh%d' % mi)
        nf = m.num_faces
        nvpf = m.num_vertices_per_frame
        if not nf or not nvpf:
            print('%-16s %6d %6d %10s %12s  %s' % (nm, nf, nvpf, '-', '-', 'empty'))
            vols.append(None)
            continue
        fr = m.frames[0]
        sc = (fr.scale.x, fr.scale.y, fr.scale.z)
        og = (fr.origin.x, fr.origin.y, fr.origin.z)
        first = fr.first_vert

        def lv(i):
            v = m.vertices[first + i]
            return (v.x*sc[0]+og[0], v.y*sc[1]+og[1], v.z*sc[2]+og[2])

        pts = [lv(k) for k in range(nvpf)]
        cen = (sum(p[0] for p in pts)/nvpf,
               sum(p[1] for p in pts)/nvpf,
               sum(p[2] for p in pts)/nvpf)
        # signed volume about centroid (translation-invariant)
        V = 0.0
        out = inn = 0
        for f in m.faces:
            p0 = lv(f.vip[0].vertex_index)
            p1 = lv(f.vip[1].vertex_index)
            p2 = lv(f.vip[2].vertex_index)
            a = sub(p0, cen); b = sub(p1, cen); c = sub(p2, cen)
            V += dot(a, cross(b, c))    # 6x signed volume of tetra
            n = cross(sub(p1, p0), sub(p2, p0))
            fc = ((p0[0]+p1[0]+p2[0])/3, (p0[1]+p1[1]+p2[1])/3,
                  (p0[2]+p1[2]+p2[2])/3)
            if dot(n, sub(fc, cen)) >= 0:
                out += 1
            else:
                inn += 1
        vols.append(V)
        vsign = '+' if V > 0 else ('-' if V < 0 else '0')
        # relative magnitude: |V| vs bounding scale^3 to spot near-flat meshes
        ext = max(max(p[i] for p in pts)-min(p[i] for p in pts) for i in range(3)) or 1
        rel = V/(ext**3)
        print('%-16s %6d %6d  %+9.2e %5d/%-5d  Vsign=%s rel=%+.3f cen=%s' %
              (nm, nf, nvpf, V, out, inn, vsign,
               rel, 'out' if out > inn else 'inn'))

    pos = sum(1 for v in vols if v and v > 0)
    neg = sum(1 for v in vols if v and v < 0)
    print('\nsigned-volume: %d positive, %d negative meshes' % (pos, neg))
    print('majority sign = %s -> flip the %d minority meshes' %
          ('+' if pos >= neg else '-', min(pos, neg)))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'C:/Dynamix/Tribes/base/tr_talon.dts')
