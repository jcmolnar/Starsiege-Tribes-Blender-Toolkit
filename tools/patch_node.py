"""Patch a node's default transform in a DTS binary, in place.

Moves/rotates mount points ("dummy pilot", "dummy hand", muzzles...) without a
Blender round-trip -- node default transforms live in the shape header, which
the exporter's hybrid splice preserves from the ORIGINAL file, so Blender-side
empty moves don't survive a round-trip export. This edits the bytes directly.

Only works when the node's transform is not shared with other nodes (checked;
shared transforms would move every node using them).

Usage:
    python tools/patch_node.py model.dts "dummy pilot" --translate 0,4.5,3.2
    python tools/patch_node.py model.dts "dummy hand" --rotate 0,0,0,1 \
        -o patched.dts

Rotation is a raw DTS quaternion x,y,z,w (floats, quantized to quat16).
Without -o the file is patched in place with a .bak backup.
"""
import argparse
import os
import shutil
import struct
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(_TOOLS_DIR)
sys.path.insert(0, _ADDON_DIR)

from dts import Dts  # noqa: E402


def _name(n):
    if isinstance(n, bytes):
        n = n.split(b'\x00')[0].decode('ascii', 'ignore')
    return n.split('\x00')[0].strip()


def find_shape_data_offset(data):
    """Byte offset of the TS::Shape field data (num_nodes...) in the file."""
    p = data.find(b'PERS')
    while p != -1:
        name_len = struct.unpack('<H', data[p + 8:p + 10])[0]
        nm = data[p + 10:p + 10 + name_len].split(b'\x00')[0].decode('ascii', 'ignore')
        padded = (name_len + 1) & ~1
        if nm == 'TS::Shape':
            return p + 10 + padded + 4  # + version
        p = data.find(b'PERS', p + 4)
    raise ValueError('no TS::Shape PERS block found')


def transforms_offset(data, shape_off, version, counts):
    """Offset of the transforms array within the shape data (v7/v8 layouts)."""
    (num_nodes, num_seq, num_subseq, num_keyframes,
     num_transforms, num_names, num_objects, num_details,
     num_meshes, num_transitions, num_frametriggers) = counts
    off = shape_off + 11 * 4          # counts
    off += 4 + 12                     # radius + center
    if version >= 8:
        off += 24                     # bounds box
        off += num_nodes * 10         # nodev8
        off += num_seq * 32
        off += num_subseq * 6
        off += num_keyframes * 8
    else:
        off += num_nodes * 16         # nodev7 (u4 fields x4? -- v7 untested)
        off += num_seq * 32
        off += num_subseq * 12
        off += num_keyframes * 12
    return off


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('dts')
    ap.add_argument('node', help='node name (exact or prefix, e.g. "dummy pilot")')
    ap.add_argument('--translate', help='x,y,z (model space)')
    ap.add_argument('--rotate', help='quaternion x,y,z,w')
    ap.add_argument('-o', '--output', default=None,
                    help='write here instead of in-place (.bak) patch')
    args = ap.parse_args()
    if not args.translate and not args.rotate:
        ap.error('nothing to do: pass --translate and/or --rotate')

    d = Dts.from_file(args.dts)
    s = d.shape.data.obj_data
    version = d.shape.data.version if hasattr(d.shape.data, 'version') else 8
    names = [_name(n) for n in s.names]
    nodes = getattr(s, 'nodes', None) or getattr(s, 'nodes_v7', [])

    want = args.node.lower()
    idx = next((i for i, n in enumerate(nodes)
                if names[n.name].lower() == want), None)
    if idx is None:
        idx = next((i for i, n in enumerate(nodes)
                    if names[n.name].lower().startswith(want)), None)
    if idx is None:
        print(f"FAIL: node '{args.node}' not found. Nodes: "
              f"{[names[n.name] for n in nodes]}")
        sys.exit(1)
    node = nodes[idx]
    ti = node.default_transform
    sharers = [names[n.name] for n in nodes if n.default_transform == ti]
    if len(sharers) > 1:
        print(f"FAIL: transform {ti} is shared by nodes {sharers}; "
              f"patching would move all of them. Aborting.")
        sys.exit(1)

    with open(args.dts, 'rb') as f:
        data = bytearray(f.read())

    shape_off = find_shape_data_offset(data)
    counts = struct.unpack_from('<11I', data, shape_off)
    t_off = transforms_offset(data, shape_off, version, counts) + ti * 20

    old_q = struct.unpack_from('<4h', data, t_off)
    old_t = struct.unpack_from('<3f', data, t_off + 8)
    print(f"node '{names[node.name]}' (idx {idx}, transform {ti})")
    print(f"  old: q={tuple(round(c / 32767.0, 4) for c in old_q)} "
          f"t=({old_t[0]:.3f},{old_t[1]:.3f},{old_t[2]:.3f})")

    if args.rotate:
        q = [float(x) for x in args.rotate.split(',')]
        struct.pack_into('<4h', data, t_off,
                         *(max(-32767, min(32767, int(round(c * 32767))))
                           for c in q))
    if args.translate:
        t = [float(x) for x in args.translate.split(',')]
        struct.pack_into('<3f', data, t_off + 8, *t)

    out = args.output or args.dts
    if not args.output:
        bak = args.dts + '.bak'
        if not os.path.exists(bak):
            shutil.copy2(args.dts, bak)
            print(f"  backup: {bak}")
    with open(out, 'wb') as f:
        f.write(data)

    # verify by re-parsing
    d2 = Dts.from_file(out)
    s2 = d2.shape.data.obj_data
    tf2 = (getattr(s2, 'transforms', None) or getattr(s2, 'transforms_v7', []))[ti]
    print(f"  new: q=({tf2.rotate.x / 32767.0:.4f},{tf2.rotate.y / 32767.0:.4f},"
          f"{tf2.rotate.z / 32767.0:.4f},{tf2.rotate.w / 32767.0:.4f}) "
          f"t=({tf2.translate.x:.3f},{tf2.translate.y:.3f},{tf2.translate.z:.3f})")
    print(f"wrote {out}")


if __name__ == '__main__':
    main()
