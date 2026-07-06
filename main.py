try:
    from .dts import Dts
except ImportError:
    from dts import Dts
import sys
import os
import os.path
import pprint
import re
import math
import bpy
import bmesh
import mathutils
from bpy import ops
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, FloatProperty, BoolProperty


'''
Keyframe matIndex flags:
if( visible )
    fMatIndex |= 0x8000;
if( visMatters )        // Uses visibility track
    fMatIndex |= 0x4000;
if( matMatters )        // Uses material track
    fMatIndex |= 0x2000;
if( frameMatters )      // Uses frame track
    fMatIndex |= 0x1000;

Material frame index: fMatIndex & 0x0fff
pa->useTextures( &fTextureVerts[matFrameIndex*fnTextureVertsPerFrame] );


v7 Keyframe matIndex is u4 instead of u2
fMatIndex & 0x80000000
fMatIndex & 0x40000000
fMatIndex & 0x20000000
fMatIndex & 0x10000000
mat index = fMatIndex & 0x0fffffff
'''
FLAG_FRAME_TRACK = 0x1000
FLAG_MATERIAL_TRACK = 0x2000
FLAG_VISIBILITY_TRACK = 0x4000
FLAG_VISIBILITY_VISIBLE = 0x8000  # per-keyframe: object visible from this key on
FLAG_IS_VISIBLE = 0x8000

FLAG_MATTYPE_NULL = 0x0
FLAG_MATTYPE_FLAGS = 0xF
FLAG_MATTYPE_PALETTE = 0x1
FLAG_MATTYPE_RGB = 0x2
FLAG_MATTYPE_TEXTURE = 0x3

FLAG_SHADING_FLAGS = 0xF00
FLAG_SHADING_NONE = 0x100
FLAG_SHADING_FLAT = 0x200
FLAG_SHADING_SMOOTH = 0x400

FLAG_TEXTURE_FLAGS = 0xF000
FLAG_TEXTURE_TRANSPARENT = 0x1000
FLAG_TEXTURE_TRANSLUCENT = 0x2000

frame_id = 0


def _action_fcurves(anim_data):
    """All fcurves of an action, handling both legacy and Blender 5 layered actions."""
    if not (anim_data and anim_data.action):
        return []
    try:
        return list(anim_data.action.fcurves)
    except AttributeError:
        fcs = []
        for layer in anim_data.action.layers:
            for strip in layer.strips:
                cb = strip.channelbag(anim_data.action_slot)
                if cb:
                    fcs.extend(cb.fcurves)
        return fcs


def _parse_palette_file(path):
    """Parse a Tribes palette into either:
    - a dict {palette_id: 256-entry RGB list} for Darkstar PL98 multi-palettes
      (each PBMP picks its table via its PiDX chunk -- flames/translucents use
      different palettes than hull textures), or
    - a plain 256-entry RGB list for single-table formats (RIFF .pal etc)."""
    import struct
    try:
        with open(path, 'rb') as f:
            buf = f.read()
    except OSError:
        return None

    def entries_from(block):
        pal = []
        for e in range(256):
            o = e * 4
            if o + 3 > len(block):
                return None
            pal.append((block[o], block[o + 1], block[o + 2]))
        return pal

    # Darkstar 98 multi-palette (found in *World.vol and extracted .ppl files):
    # 'PL98' u32 ver, u32 count, 52-byte header, then 2064-byte records with
    # the id header at PL98+1076+2064k. IMPORTANT: each id's 256*4 RGBA color
    # table is the 1024 bytes immediately BEFORE its id header (verified: the
    # table before id 1135 is byte-identical to pal2MS.pal; the +12 offset
    # used previously picked up a different table and garbled shape textures).
    p = buf.find(b'PL98')
    if p != -1 and len(buf) >= p + 12:
        count = struct.unpack('<I', buf[p + 8:p + 12])[0]
        if 0 < count <= 64:
            pals = {}
            for k in range(count):
                idpos = p + 1076 + 2064 * k
                if idpos + 4 > len(buf) or idpos - 1024 < p:
                    break
                pid = struct.unpack('<I', buf[idpos:idpos + 4])[0]
                if pid == 0xFFFFFFFF:  # index-remap record, not colors
                    continue
                tab = entries_from(buf[idpos - 1024:idpos])
                if tab:
                    pals[pid] = tab
            if pals:
                return pals

    # RIFF PAL: "RIFF" ... "data" chunk = u16 version, u16 count, then RGBFlags entries
    if buf[:4] == b'RIFF':
        pos = buf.find(b'data')
        if pos != -1 and len(buf) >= pos + 8 + 4 + 256 * 4:
            return entries_from(buf[pos + 12:pos + 12 + 256 * 4])
        return None

    # Raw fallbacks: 768 = packed RGB, 1024+ = RGBX
    if len(buf) == 768:
        return [(buf[i * 3], buf[i * 3 + 1], buf[i * 3 + 2]) for i in range(256)]
    if len(buf) >= 1024:
        return entries_from(buf[:1024])
    return None


def _find_palette(dts_dir):
    """Find the palette for a model with no guesswork. Preference order:
    1. a .ppl next to the .dts (explicit user choice),
    2. the game's PL98 multi-palette auto-located by walking up from the
       .dts folder to the Tribes install and reading *World.vol directly
       (lushWorld preferred -- the classic look; ids are the same in every
       world, only the lighting tint differs),
    3. a single-table .pal next to the .dts (pal2MS preferred; it matches
       the hull palette but can't color flames/translucents)."""
    import glob

    candidates = sorted(glob.glob(os.path.join(dts_dir, '*.ppl')))

    # Auto-locate *World.vol: the dts usually lives in <Tribes>/<something>/,
    # with the vols in <Tribes>/base/. Check each ancestor dir and its base/.
    vols = []
    d = os.path.abspath(dts_dir)
    for _ in range(6):
        for vol_dir in (d, os.path.join(d, 'base')):
            if os.path.isdir(vol_dir):
                found = sorted(glob.glob(os.path.join(vol_dir, '*World.vol')),
                               key=lambda fn: (os.path.basename(fn).lower() != 'lushworld.vol',))
                for fn in found:
                    if fn not in vols:
                        vols.append(fn)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    candidates += vols

    pals = sorted(glob.glob(os.path.join(dts_dir, '*.pal')))
    candidates += sorted(pals, key=lambda fn: (os.path.basename(fn).lower() != 'pal2ms.pal',))

    for fn in candidates:
        pal = _parse_palette_file(fn)
        if pal:
            print("Using palette: {}".format(fn))
            return pal
    return None


_vol_index_cache = {}


def _vol_file_index(volpath):
    """Filename -> (data offset, size) index of a PVOL archive (cached)."""
    import struct
    if volpath in _vol_index_cache:
        return _vol_index_cache[volpath]
    idx = {}
    try:
        with open(volpath, 'rb') as f:
            b = f.read()
        if b[:4] == b'PVOL':
            dirofs = struct.unpack('<I', b[4:8])[0]
            if b[dirofs:dirofs + 4] == b'vols':
                nsize = struct.unpack('<I', b[dirofs + 4:dirofs + 8])[0]
                names_blk = b[dirofs + 8:dirofs + 8 + nsize]
                ip = b.find(b'voli', dirofs + 8 + nsize - 4)
                if ip != -1:
                    isize = struct.unpack('<I', b[ip + 4:ip + 8])[0]
                    ib = b[ip + 8:ip + 8 + isize]
                    for off in range(0, len(ib) - 16, 17):
                        _z, name_ofs, data_ofs, size = struct.unpack('<4I', ib[off:off + 16])
                        end = names_blk.find(b'\0', name_ofs)
                        nm = names_blk[name_ofs:end].decode('latin-1').lower()
                        idx[nm] = (data_ofs, size)
    except Exception as e:
        print("Could not index {}: {}".format(volpath, e))
    _vol_index_cache[volpath] = idx
    return idx


def _load_from_vols(filename, dts_dir):
    """Fetch a file's raw bytes from any game .vol reachable from dts_dir
    (the model's folder, its ancestors, and their base/ subfolders)."""
    import struct, glob
    target = filename.lower()
    vol_dirs = []
    d = os.path.abspath(dts_dir)
    for _ in range(6):
        for vd in (d, os.path.join(d, 'base')):
            if os.path.isdir(vd) and vd not in vol_dirs:
                vol_dirs.append(vd)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for vd in vol_dirs:
        for vol in sorted(glob.glob(os.path.join(vd, '*.vol'))):
            idx = _vol_file_index(vol)
            if target in idx:
                data_ofs, size = idx[target]
                with open(vol, 'rb') as f:
                    f.seek(data_ofs)
                    head = f.read(8)
                    if head[:4] != b'VBLK':
                        f.seek(data_ofs)
                    payload = f.read(size)
                print("Texture {} extracted from {}".format(filename, vol))
                return payload
    return None


def _decode_pbmp(path, palette):
    """Decode a Dynamix PBMP file into (width, height, RGBA float list) or None."""
    try:
        with open(path, 'rb') as f:
            buf = f.read()
    except OSError:
        return None
    return _decode_pbmp_bytes(buf, palette)


def _decode_pbmp_bytes(buf, palette):
    """Decode Dynamix PBMP bytes into (width, height, RGBA float list) or None."""
    import struct
    if buf[:4] != b'PBMP':
        return None

    w = h = None
    data = None
    pidx = None
    pos = 8
    # Chunk sizes in these files are unreliable (head/data can overrun the
    # declared PBMP size), so walk tags defensively over the whole file.
    while pos + 8 <= len(buf):
        tag = buf[pos:pos + 4]
        size = struct.unpack('<I', buf[pos + 4:pos + 8])[0]
        body = buf[pos + 8:pos + 8 + size]
        if tag == b'head' and size >= 16:
            _ver, w, h, _depth = struct.unpack('<4I', body[:16])
        elif tag == b'data':
            data = body
        elif tag == b'PiDX' and size >= 4:
            pidx = struct.unpack('<I', body[:4])[0]
        pos += 8 + size

    if not (w and h and data) or len(data) < w * h:
        return None

    # Multi-palette (PL98): the PBMP's PiDX chunk picks its table -- hull
    # textures, flames, and translucents each use a different palette id.
    if isinstance(palette, dict):
        table = palette.get(pidx)
        if table is None and palette:
            table = next(iter(palette.values()))
            print("  palette id {} not in palette file; using first table".format(pidx))
    else:
        table = palette

    pixels = [0.0] * (w * h * 4)
    for y in range(h):
        src = y * w
        dst = (h - 1 - y) * w * 4  # Blender images are bottom-up
        for x in range(w):
            idx = data[src + x]
            r, g, b = table[idx] if table else (idx, idx, idx)
            o = dst + x * 4
            pixels[o] = r / 255.0
            pixels[o + 1] = g / 255.0
            pixels[o + 2] = b / 255.0
            # pure magenta is the Darkstar transparency key
            pixels[o + 3] = 0.0 if (r, g, b) == (255, 0, 255) else 1.0
    return w, h, pixels


def load_dts_image(image_path, palette=None):
    """Load a texture for a DTS material with no manual extraction needed.
    Tries, in order: a .png next to the .dts, a .bmp on disk (standard BMP via
    Blender, Dynamix PBMP via our decoder + palette), and finally the game's
    .vol archives (auto-located from the .dts path). Returns a bpy image or None."""
    base = image_path.rsplit('.', 1)[0]
    name = os.path.basename(base) + '.bmp'

    png_path = base + '.png'
    if os.path.exists(png_path):
        return bpy.data.images.load(png_path, check_existing=False)

    def _image_from_decoded(decoded, img_name):
        w, h, pixels = decoded
        image = bpy.data.images.new(img_name, width=w, height=h, alpha=True)
        image.colorspace_settings.name = 'sRGB'
        image.pixels = pixels
        image.pack()
        if palette is None:
            print("PBMP decoded WITHOUT palette (grayscale): {} -- no game "
                  "*World.vol or .ppl/.pal palette was found".format(img_name))
        return image

    bmp_path = None
    for ext in ('.bmp', '.BMP'):
        if os.path.exists(base + ext):
            bmp_path = base + ext
            break
    if bmp_path is not None:
        with open(bmp_path, 'rb') as f:
            magic = f.read(4)
        if magic[:2] == b'BM':  # standard BMP, Blender handles it
            return bpy.data.images.load(bmp_path, check_existing=False)
        decoded = _decode_pbmp(bmp_path, palette)
        if decoded is None:
            print("Could not decode texture: {}".format(bmp_path))
            return None
        return _image_from_decoded(decoded, os.path.basename(bmp_path))

    # Not on disk: pull it straight out of the game's .vol archives
    payload = _load_from_vols(name, os.path.dirname(image_path))
    if payload is None:
        return None
    if payload[:2] == b'BM':
        # standard BMP: Blender can only load from a file, so bounce it
        # through a temp file and pack the result into the .blend
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), name)
        with open(tmp, 'wb') as f:
            f.write(payload)
        image = bpy.data.images.load(tmp, check_existing=False)
        image.pack()
        try:
            os.remove(tmp)
        except OSError:
            pass
        return image
    decoded = _decode_pbmp_bytes(payload, palette)
    if decoded is None:
        print("Could not decode vol texture: {}".format(name))
        return None
    return _image_from_decoded(decoded, name)


class ImportDTS(bpy.types.Operator, ImportHelper):
    bl_idname = "dynamix.dts"
    bl_label = "Import Starsiege: Tribes .dts"
    bl_description = 'Imports Starsiege: Tribes .dts file.'

    filter_glob : StringProperty(default="*.dts", options={'HIDDEN'})
    filename_ext = ".dts"
    
    import_scale: FloatProperty(
        name="Import Scale",
        description="[BOTH] Scale multiplier for imported geometry. Use 1.0 for correct round-trip export",
        default=1.0,
        min=0.01,
        max=1000.0
    )
    
    organize_by_lod: BoolProperty(
        name="Organize by LOD",
        description="[BOTH] Create collections for each LOD level (36/10/2) for easier editing. Edit only LOD36 if using 'High LOD for All' on export",
        default=True,
    )
    

    def execute(self, context):
        global frame_id
        # Module-level counter survives across imports in one Blender session;
        # without a reset, a re-import keys its animation starting where the
        # previous import ended (thousands of frames out on the timeline).
        frame_id = 0
        import re

        # Force CONSTANT interpolation to prevent drift and file bloat
        context.preferences.edit.keyframe_new_interpolation_type = 'CONSTANT'

        filename = self.filepath.split(os.path.sep)[-1].split('.')[0]
        path = self.filepath

        # Palette for decoding Dynamix PBMP-format .bmp textures (any .ppl/.pal
        # in the model's folder); None -> PBMPs decode as grayscale
        dts_palette = _find_palette(os.path.dirname(self.filepath))
        
        # Collection created, but not used yet
        obj_collection = bpy.data.collections.new(filename)
        context.scene.collection.children.link(obj_collection)
        
        # Store original DTS path for round-trip export (header preservation)
        obj_collection["dts_source_file"] = path

        with open(path, 'r') as f:
            def store(str=''):
                pass
#                print(str)
#                with open(out_path, 'a') as f:
#                    f.write(str + "\n")
                
            def short2float(short):
                if short == 0:
                    return 0
                return float(short) / float(0x7FFF)

            # Blender uses linear rgb
            def srgb_to_linear_rgb(srgb: int) -> float:
                srgb = srgb / 255
                if srgb <= 0.04045:
                    linear = srgb / 12.92
                else:
                    linear = math.pow((srgb + 0.055) / 1.055, 2.4)

                return linear
            
            def create_nodes(node: Dts.Node, nodes, transforms, node_tree):
                store('var node_{} = new THREE.Group();'.format(node.id))

                def_trans = transforms[nodes[node.id].default_transform]
                if nodes[node.id].parent == -1:
                    store('node_{}.position.set({}, {}, {});'.format(node.id, def_trans.translate.x, def_trans.translate.y, def_trans.translate.z))
                    store('node_{}.applyQuaternion(new THREE.Quaternion({}, {}, {}, {}));'.format(
                        node.id, short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z), short2float(def_trans.rotate.w)
                    ))
                    store('group.add(node_{});'.format(node.id))
                else:
                    #store('console.log(node_{}.quaternion);'.format(nodes[node.id].parent))
                    store('node_{}.translateX({});'.format(node.id, def_trans.translate.x))
                    store('node_{}.translateY({});'.format(node.id, def_trans.translate.y))
                    store('node_{}.translateZ({});'.format(node.id, def_trans.translate.z))
                    store('node_{}.applyQuaternion(node_{}.quaternion);'.format(node.id, nodes[node.id].parent))
                    store('node_{}.applyQuaternion(new THREE.Quaternion({}, {}, {}, {}).invert());'.format(
                        node.id, short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z), short2float(def_trans.rotate.w)
                    ))
                    store('node_{}.add(node_{});'.format(nodes[node.id].parent, node.id))

                for child in node_tree[node.id]:
                    create_nodes(nodes[child], nodes, transforms, node_tree)


            def animate_meshes(mesh, obj, names, keyframes, sequences, subsequences, scene, blender_name):
                global frame_id

                if not obj.num_subsequences:
                    print("Object has no subsequences to animate")
                    return

                if hasattr(mesh, 'frames'):
                    frames = mesh.frames
                elif hasattr(mesh, 'frames_v2'):
                    frames = mesh.frames_v2

                # Objects only have sequences if they have more than one frame on the mesh
                if len(frames) == 1:
                    print('{} has no frames'.format(names[obj.name]))
                    return

                # Assume meshes can only have one subsequence
                subseq = subsequences[obj.first_subsequence]
                seq = subseq.sequence_index
                seq_name = names[sequences[seq].name]
                print('Seq:', seq_name, 'Subseq id:', obj.first_subsequence)
                scene.timeline_markers.new(seq_name, frame=frame_id)

                first_keyframe = subseq.first_keyframe
                isFrameTrackKeyframe = keyframes[first_keyframe].mat_index & FLAG_FRAME_TRACK
                isMaterialTrackKeyframe = keyframes[first_keyframe].mat_index & FLAG_MATERIAL_TRACK
                isVisibilityTrack = keyframes[first_keyframe].mat_index & FLAG_VISIBILITY_TRACK

                # Look up by the ACTUAL Blender name (may have a .00x suffix on
                # re-import) -- a plain DTS-name lookup can hit a stale object
                # from a previous import and bind the animation to it.
                object = bpy.context.scene.objects[blender_name]
                if isFrameTrackKeyframe:
                    print('Frame track!!!')
                    # Morph frames were already imported as correctly-decoded
                    # shape keys (named frame_NNN, per-frame scale/origin
                    # applied) right after mesh creation. Here we only ANIMATE
                    # their values along the timeline. (This code previously
                    # ADDED its own keys with raw PACKED 0-255 coords -- garbage
                    # geometry that also inflated export packing bounds.)
                    sk_data = object.data.shape_keys
                    if not sk_data:
                        print('  no shape keys on object; frame track skipped')
                        return
                    sk_data.use_relative = True

                    sks = []
                    for key in range(first_keyframe, first_keyframe + subseq.num_keyframes):
                        frametrack_key = keyframes[key].key_value
                        sk = sk_data.key_blocks.get('frame_{:03d}'.format(frametrack_key))
                        if sk is None:
                            # frame 0 == basis: showing it means all keys at 0,
                            # which the sequencing loop below handles via prev_sk
                            continue
                        sk.interpolation = 'KEY_LINEAR'
                        sk.value = 0
                        sk.keyframe_insert(data_path="value", index=-1)
                        sks.append(sk)
                    
                    prev_sk = None
                    for sk_idx in range(len(sks)):
                        scene.frame_set(frame_id)

                        # Set prev frame back to zero
                        if prev_sk is not None:
                            prev_sk.value = 0
                            prev_sk.keyframe_insert(data_path="value", index=-1)

                        # Set current frame to 1
                        sks[sk_idx].value = 1
                        sks[sk_idx].keyframe_insert(data_path="value", index=-1)

                        # Queue up next frame, setting it to zero, so there's no automatic transition to 1
                        if sk_idx != len(sks) - 1:
                            sks[sk_idx + 1].value = 0
                            sks[sk_idx + 1].keyframe_insert(data_path="value", index=-1)

                        prev_sk = sks[sk_idx]
                        frame_id += 1

                if isMaterialTrackKeyframe:
                    print('Material track!!!')
                if isVisibilityTrack:
                    print('Visibility track!!!')
                if not isFrameTrackKeyframe and not isMaterialTrackKeyframe and not isVisibilityTrack:
                    print('Transform track!!!')

            def generate_ifl_materials(sequences, keyframes):
                ifl_materials = {}
                for seq_id in range(len(sequences)):
                    sequence: Dts.VectorSequence = sequences[seq_id]
                    seq_name = names[sequence.name]

                    # IFL subsequences
                    if sequence.num_ifl_subsequences > 0:
                        # A sequence may have multiple IFL subsequences, for different materials
                        ifl_frame_id = 0
                        for subseq_count in range(sequence.num_ifl_subsequences):
                            subseq = subsequences[sequence.first_ifl_subsequence + subseq_count]
                            first_keyframe = subseq.first_keyframe

                            ifl_mat = bpy.data.materials.new(name='ifl_{}_{}'.format(seq_name, subseq_count))
                            ifl_mat.use_nodes = True
                            shader_nodes = ifl_mat.node_tree.nodes
                            shader_links = ifl_mat.node_tree.links

                            if keyframes[first_keyframe].mat_index not in ifl_materials:
                                ifl_materials[keyframes[first_keyframe].mat_index] = 'ifl_{}_{}'.format(seq_name, subseq_count)

                            # Texture nodes. One entry per KEYFRAME (repeats
                            # share a node) -- the mix chain below switches
                            # between entries by frame index.
                            texture_nodes = []
                            ifl_sequence = []
                            node_by_path = {}
                            node_num = 0
                            for key in range(first_keyframe, first_keyframe + subseq.num_keyframes):
                                # The material index represents the default material, while the key_value represents the new material to replace it with
                                old_map = d.materials.params[keyframes[key].mat_index].map_file
                                old_map = old_map[:old_map.find(b'\0')].decode('ascii')
                                new_map = d.materials.params[keyframes[key].key_value].map_file
                                new_map = new_map[:new_map.find(b'\0')].decode('ascii')
                                print("Key:", key, "Material Idx:", keyframes[key].mat_index, "Map name:", old_map, "->", new_map)

                                image_path = os.path.dirname(self.filepath) + os.path.sep + new_map
                                image_path = image_path.rsplit('.', 1)[0] + ".png"
                                ifl_sequence.append(image_path)

                                # Re-use the texture node if this frame repeats an image
                                if image_path in node_by_path:
                                    texture_nodes.append(node_by_path[image_path])
                                    continue

                                # Make a new texture shader
                                image = load_dts_image(image_path, dts_palette)
                                if image is None:
                                    print("Missing image: {}".format(image_path))

                                shader_node = shader_nodes.new("ShaderNodeTexImage")

                                if image:
                                    image.name = new_map  # "sequence_{}_{}".format(seq_name, ifl_frame_id)
                                    image.use_fake_user = True
                                    shader_node.image = image
                                    if image.filepath:  # packed PBMP images have no file source
                                        shader_node.image.source = "FILE"

                                shader_node.location = node_num * 250, 100
                                node_by_path[image_path] = shader_node
                                texture_nodes.append(shader_node)
                                node_num += 1

                            # Create mix and math nodes
                            prev_mix_node_color = None
                            prev_mix_node_alpha = None
                            greater_nodes = []
                            for mix_idx in range(len(texture_nodes) - 1):
                                mix_node_color = shader_nodes.new("ShaderNodeMixRGB")
                                mix_node_color.location = 300 + mix_idx * 250, 550

                                mix_node_alpha = shader_nodes.new("ShaderNodeMixRGB")
                                mix_node_alpha.location = 300 + mix_idx * 250, 350

                                # Math node to toggle between the two textures
                                math_node = shader_nodes.new("ShaderNodeMath")
                                math_node.operation = "GREATER_THAN"
                                math_node.inputs[1].default_value = mix_idx + 1  # Threshold
                                math_node.location = 50 + mix_idx * 250, 750
                                shader_links.new(math_node.outputs["Value"], mix_node_color.inputs["Fac"])
                                shader_links.new(math_node.outputs["Value"], mix_node_alpha.inputs["Fac"])
                                greater_nodes.append(math_node)

                                # For the first mix node, use the first two textures
                                if prev_mix_node_color is None:
                                    shader_links.new(texture_nodes[mix_idx].outputs["Color"], mix_node_color.inputs["Color1"])
                                    shader_links.new(texture_nodes[mix_idx + 1].outputs["Color"], mix_node_color.inputs["Color2"])

                                    shader_links.new(texture_nodes[mix_idx].outputs["Alpha"], mix_node_alpha.inputs["Color1"])
                                    shader_links.new(texture_nodes[mix_idx + 1].outputs["Alpha"], mix_node_alpha.inputs["Color2"])
                                else:
                                    # Otherwise, use the previous mix node and the next texture
                                    shader_links.new(prev_mix_node_color.outputs["Color"], mix_node_color.inputs["Color1"])
                                    shader_links.new(texture_nodes[mix_idx + 1].outputs["Color"], mix_node_color.inputs["Color2"])

                                    shader_links.new(prev_mix_node_alpha.outputs["Color"], mix_node_alpha.inputs["Color1"])
                                    shader_links.new(texture_nodes[mix_idx + 1].outputs["Alpha"], mix_node_alpha.inputs["Color2"])

                                prev_mix_node_color = mix_node_color
                                prev_mix_node_alpha = mix_node_alpha

                            # Create an Add node that inputs into all of the "greater than" nodes
                            add_node = shader_nodes.new("ShaderNodeMath")
                            add_node.operation = "ADD"
                            add_node.inputs[1].default_value = 0.01
                            add_node.location = -150, 850
                            for g_node in greater_nodes:
                                shader_links.new(add_node.outputs["Value"], g_node.inputs["Value"])

                            # Create an input value node for keyframing and attach it to the "Add" node
                            input_node = shader_nodes.new("ShaderNodeValue")
                            input_node.name = "IFL Input Value"
                            input_node.location = -350, 850
                            shader_links.new(input_node.outputs["Value"], add_node.inputs[0])

                            # Link the final mix output to the BSDF
                            bsdf_node = shader_nodes.get("Principled BSDF")
                            bsdf_node.location = (len(texture_nodes) + 1) * 250, 100
                            shader_links.new(prev_mix_node_color.outputs["Color"], bsdf_node.inputs["Base Color"])

                            # Update the material's blend mode if there are alpha channels
                            mat_flags = d.materials.params[keyframes[first_keyframe].key_value].flags
                            if mat_flags & FLAG_TEXTURE_TRANSPARENT == FLAG_TEXTURE_TRANSPARENT or mat_flags & FLAG_TEXTURE_TRANSLUCENT == FLAG_TEXTURE_TRANSLUCENT:
                                ifl_mat.blend_method = "BLEND"
                                if mat_flags & FLAG_TEXTURE_TRANSLUCENT == FLAG_TEXTURE_TRANSLUCENT:
                                    # additive in-engine: luminance-as-alpha + emissive
                                    lum = shader_nodes.new("ShaderNodeRGBToBW")
                                    lum.location = bsdf_node.location[0] - 200, -150
                                    shader_links.new(prev_mix_node_color.outputs["Color"], lum.inputs["Color"])
                                    amul = shader_nodes.new("ShaderNodeMath")
                                    amul.operation = 'MULTIPLY'
                                    amul.location = bsdf_node.location[0] - 50, -150
                                    shader_links.new(lum.outputs["Val"], amul.inputs[0])
                                    shader_links.new(prev_mix_node_alpha.outputs["Color"], amul.inputs[1])
                                    shader_links.new(amul.outputs["Value"], bsdf_node.inputs["Alpha"])
                                    if "Emission Color" in bsdf_node.inputs:
                                        shader_links.new(prev_mix_node_color.outputs["Color"], bsdf_node.inputs["Emission Color"])
                                        bsdf_node.inputs["Emission Strength"].default_value = 1.0
                                else:
                                    shader_links.new(prev_mix_node_alpha.outputs["Color"], bsdf_node.inputs["Alpha"])

                            mat_out_node = shader_nodes.get("Material Output")
                            mat_out_node.location = bsdf_node.location[0] + 300, bsdf_node.location[1]

                return ifl_materials


            
            d = Dts.from_file(path)

            MAX_VAL = float(0x7FFF)
            names = []
            nodes = []
            objects = []
            textures = []
            mapFiles = []
            transforms = []
            pngORbmp = ""
            node_tree = {}
            obj_dts_to_blender_map = {}


            if b'TS::Shape' in d.shape.data.classname:
                shape_data: Dts.TsShape = d.shape.data.obj_data

                for name in shape_data.names:
                    names.append(name[:name.find(b'\0')].decode('ascii'))

                if hasattr(shape_data, 'objects'):
                    objects = shape_data.objects
                elif hasattr(shape_data, 'objects_v7'):
                    objects = shape_data.objects_v7

                if hasattr(shape_data, 'transforms'):
                    transforms = shape_data.transforms
                elif hasattr(shape_data, 'transforms_v7'):
                    transforms = shape_data.transforms_v7

                if hasattr(shape_data, 'nodes'):
                    nodes = shape_data.nodes
                elif hasattr(shape_data, 'nodes_v7'):
                    nodes = shape_data.nodes_v7

                if hasattr(shape_data, 'keyframes'):
                    keyframes = shape_data.keyframes
                elif hasattr(shape_data, 'keyframes_v7'):
                    keyframes = shape_data.keyframes_v7

                sequences = shape_data.sequences
                if hasattr(shape_data, 'subsequences'):
                    subsequences = shape_data.subsequences
                elif hasattr(shape_data, 'subsequences_v7'):
                    subsequences = shape_data.subsequences_v7
            else:
                print("Shape was not of TS::Shape")
                sys.exit(1)



            # Load textures
            if d.has_materials and d.materials:
                material_count = 0
                i = 0

                iflTextures = generate_ifl_materials(sequences, keyframes)
                    
                # Make a list of mapFiles, needed for IFL sequences
                for param in d.materials.params:
                    bitmap_name: bytes = param.map_file[:param.map_file.find(b'\0')]
                    mapFiles.append(bitmap_name.decode('ascii'))
                        
                for param in d.materials.params:
                    if i not in iflTextures:
                        bitmap_name: bytes = param.map_file[:param.map_file.find(b'\0')]
                        #texture = None
                        #if len(bitmap_name):
                        #    bitmap_name = bitmap_name.replace(b'.bmp', b'.png')
                        #    bitmap_name = bitmap_name.replace(b'.BMP', b'.png')
                        #   texture = 'const texture_' + str(i) + " = textureLoader.load('textures/{}')".format(
                        #       bitmap_name.decode('ascii'))

                        # Blender - Create a new material based on the model name and material id
                        mat = bpy.data.materials.new(filename.split(os.path.sep)[-1] + '.' + str(i))
                        mat.use_nodes = True

                        if param.flags & FLAG_TEXTURE_TRANSPARENT == FLAG_TEXTURE_TRANSPARENT or param.flags & FLAG_TEXTURE_TRANSLUCENT == FLAG_TEXTURE_TRANSLUCENT:
                            mat.blend_method = "BLEND"

                        mat_nodes = mat.node_tree.nodes
                        # check alpha paramater, may need to flip 0 to 1, and 1 to 0
                        mat_nodes["Principled BSDF"].inputs[0].default_value = (
                        srgb_to_linear_rgb(param.rgb.red), srgb_to_linear_rgb(param.rgb.green), srgb_to_linear_rgb(param.rgb.blue), param.alpha)

                        if len(bitmap_name):
                            store('map: {},'.format('texture_' + str(i)))
                            store('transparent: true,')

                            bitmap_name = bitmap_name.replace(b'.bmp', b'.png')
                            bitmap_name = bitmap_name.replace(b'.BMP', b'.png')

                            # Blender - Create the image texture node
                            shader_node = mat_nodes.new("ShaderNodeTexImage")
                            shader_node.location = -400, 200
                            shader_node.select = True
                            # Create the path to the image based on the model path.
                            # abspath: Blender's image loader resolves relative
                            # paths against its own CWD, not the process CWD, so
                            # a relative filepath passes os.path.exists below but
                            # then fails to load.
                            image_path = os.path.abspath(
                                os.path.dirname(self.filepath) + os.path.sep + bitmap_name.decode('ascii'))

                            # Try .png, standard .bmp, or Dynamix PBMP .bmp
                            loaded_image = load_dts_image(image_path, dts_palette)
                            if loaded_image:
                                shader_node.image = loaded_image
                            else:
                                print("Missing image: {}".format(image_path))
                            # Link the image texture node to the color slot on the BSDF node
                            links = mat.node_tree.links
                            bsdf = mat_nodes["Principled BSDF"]
                            link = links.new(shader_node.outputs["Color"], bsdf.inputs[0])

                            if param.flags & FLAG_TEXTURE_TRANSLUCENT == FLAG_TEXTURE_TRANSLUCENT:
                                # The engine draws translucent materials
                                # (muzzle flashes, engine flames) additively:
                                # black contributes nothing. Approximate that
                                # with luminance-as-alpha and an emissive
                                # color so it glows like in game.
                                lum = mat_nodes.new("ShaderNodeRGBToBW")
                                lum.location = -400, -100
                                links.new(shader_node.outputs["Color"], lum.inputs["Color"])
                                amul = mat_nodes.new("ShaderNodeMath")
                                amul.operation = 'MULTIPLY'
                                amul.location = -200, -100
                                links.new(lum.outputs["Val"], amul.inputs[0])
                                links.new(shader_node.outputs["Alpha"], amul.inputs[1])
                                links.new(amul.outputs["Value"], bsdf.inputs["Alpha"])
                                if "Emission Color" in bsdf.inputs:
                                    links.new(shader_node.outputs["Color"], bsdf.inputs["Emission Color"])
                                    bsdf.inputs["Emission Strength"].default_value = 1.0
                                mat.show_transparent_back = False
                            else:
                                # Link the alpha input/output
                                links.new(shader_node.outputs["Alpha"], bsdf.inputs[21])

                        textures.append(mat.name)
                    else:
                        textures.append(iflTextures[i])

                    # Blender - count materials, used for face maps
                    material_count += 1
                    i += 1

            # Make nodes
            # Create a dictionary of the nodes' children
            for j in range(0, len(nodes)):
                node_tree[j] = []
                nodes[j].id = j  # Set an ID on the nodes so we can reference it later
                for node2 in range(0, len(nodes)):
                    if nodes[node2].parent == j:
                        node_tree[j].append(node2)
            #pprint.pprint(node_tree)


            # Start with the roots, which come from the LODs
            # Find any needed parents for the roots. Should only be the bounds box (0)
            needed_parents = set()
            for lod_idx in range(0, len(shape_data.details)):
                if nodes[lod_idx].parent != -1:
                    needed_parents.add(nodes[lod_idx].parent)
            #print(needed_parents)

            # Root node (bounds)
            store('var node_0 = new THREE.Group();')
            store('node_0.position.set({}, {}, {});'.format(transforms[nodes[0].default_transform].translate.x, transforms[nodes[0].default_transform].translate.y, transforms[nodes[0].default_transform].translate.z))
            store('node_0.applyQuaternion(new THREE.Quaternion({}, {}, {}, {}));'.format(
                short2float(transforms[nodes[0].default_transform].rotate.x), short2float(transforms[nodes[0].default_transform].rotate.y), short2float(transforms[nodes[0].default_transform].rotate.z), short2float(transforms[nodes[0].default_transform].rotate.w)
            ))
            store('group.add(node_0);')

            # Set up the node hierarchy
            for child in node_tree[0]:
                create_nodes(nodes[child], nodes, transforms, node_tree)



            # Set up the node hierarchy
            # for j in range(0, len(nodes)):
            #     store('var node_{} = new THREE.Group();'.format(j))

            #     def_trans = transforms[nodes[j].default_transform]
            #     parent_trans = transforms[nodes[j].parent]
            #     store('node_{}.position.set({}, {}, {});'.format(j, def_trans.translate.x, def_trans.translate.y, def_trans.translate.z))
            #     #store('node_{}.setRotationFromQuaternion(new THREE.Quaternion({}, {}, {}, {}));'.format(
            #     #store('node_{}.applyQuaternion(new THREE.Quaternion({}, {}, {}, {}));'.format(
            #     #    j, short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z), short2float(def_trans.rotate.w)
            #     #))

            #     if nodes[j].parent == -1: # or nodes[j].parent == 0xFFFFFFFF:
            #         store('node_{}.applyQuaternion(new THREE.Quaternion({}, {}, {}, {}));'.format(
            #             j, short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z), short2float(def_trans.rotate.w)
            #         ))
            #         store('group.add(node_{});'.format(j))
            #     else:
            #         #store('console.log(node_{}.quaternion);'.format(nodes[j].parent))
            #         store('node_{}.applyQuaternion(new THREE.Quaternion({}, {}, {}, {}));'.format(
            #             j, short2float(parent_trans.rotate.x), short2float(parent_trans.rotate.y), short2float(parent_trans.rotate.z), short2float(parent_trans.rotate.w)
            #         ))
            #         store('node_{}.applyQuaternion(new THREE.Quaternion({}, {}, {}, {}));'.format(
            #             j, short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z), short2float(def_trans.rotate.w)
            #         ))
            #         store('node_{}.add(node_{});'.format(nodes[j].parent, j))


            # Set up LoDs
            for lod in shape_data.details:
                store("""
                lods.push({{
                    node: node_{},
                    level: {}
                }});""".format(lod.root_node_index, lod.size))
                store('node_{}.visible = false;'.format(lod.root_node_index))
            store('lods[0].node.visible = true;')


            # Add meshes
            print('meshes')
            print()
            obj_id = 0
            mesh_data: Dts.TsAnimmesh
            for mesh_data in d.meshes:
                obj = objects[obj_id]
                obj_name = names[objects[obj_id].name]
                print('Object ID: {}, Name: {}'.format(obj_id, obj_name))
                parent_node = objects[obj_id].node_index
                array_verts_all = []  # Blender
                array_faces = []  # Blender
                array_faces_material = []  # Blender
                array_texvert = []  # Blender
                array_uvs = []  # Blender

                if hasattr(mesh_data, 'frames'):
                    frames = mesh_data.frames
                elif hasattr(mesh_data, 'frames_v2'):
                    frames = mesh_data.frames_v2

                # Face-less meshes are the degenerate (usually 2-vert) bounding/placeholder
                # meshes that multi-part weapons use for detail/LOD slots. Dropping them
                # collapsed the mesh count and broke multi-mesh round-trips (the exporter's
                # header splice then bailed with "using generated header"). Import them as
                # verts-only placeholder objects so the structure is preserved on export.
                # Only skip a mesh that is genuinely empty (no faces AND no vertices).
                if len(mesh_data.faces) == 0 and mesh_data.num_vertices_per_frame == 0:
                    obj_id += 1
                    continue

                # Vertices
                # Add only vertices from Frame 0, this will be our Basis
                start_vertex = frames[0].first_vert
                for vrt_idx in range(mesh_data.num_vertices_per_frame):
                    # Blender - Unpack vertices using scale and origin from frame data
                    # packed_val * scale + origin = world coordinate (then apply import scale)
                    vert = mesh_data.vertices[start_vertex + vrt_idx]
                    frame = frames[0]
                    x = (vert.x * frame.scale.x + frame.origin.x) * self.import_scale
                    y = (vert.y * frame.scale.y + frame.origin.y) * self.import_scale
                    z = (vert.z * frame.scale.z + frame.origin.z) * self.import_scale
                    array_val = [x, y, z]
                    array_verts_all.append(array_val)

                # Faces
                for face in mesh_data.faces:
                    store('geometry.faces.push( new THREE.Face3( {}, {}, {}, null, null, {} ) );'.format(
                        face.vip[0].vertex_index,
                        face.vip[1].vertex_index,
                        face.vip[2].vertex_index,
                        face.material
                    ))
                    # Blender - Put all the faces in an array of [v1, v2, v3]
                    array_val = [face.vip[0].vertex_index, face.vip[1].vertex_index, face.vip[2].vertex_index]
                    array_faces.append(array_val)
                    array_faces_material.append(face.material)

                # Invert the normals
                store("""
                for ( var i = 0; i < geometry.faces.length; i ++ ) {

                    var face = geometry.faces[ i ];
                    var temp = face.a;
                    face.a = face.c;
                    face.c = temp;

                }

                geometry.computeFaceNormals();
                geometry.computeVertexNormals();
                            """)

                # Texture vertices
                store('textureVerts = [')
                for vert in mesh_data.texture_vertices:
                    # store('new THREE.Vector2({}, {}),'.format(
                    #     vert.x, vert.y
                    # ))
                    # Blender - Put all the texture vertices in an array of [x, 1-y]. Blender uses a different texture space coordinate.
                    array_val = [vert.x, 1 - vert.y]
                    array_texvert.append(array_val)
                store('];')

                # Set up UVs
                # store('geometry.faceVertexUvs = [[')
                for face in mesh_data.faces:
                    # store(' [ textureVerts[{}], textureVerts[{}], textureVerts[{}] ],'.format(
                    #     face.vip[0].texture_index, face.vip[1].texture_index, face.vip[2].texture_index
                    # ))
                    # Blender - Look up texture vertices from face indices
                    array_val = array_texvert[face.vip[0].texture_index]
                    array_uvs.append(array_val)
                    array_val = array_texvert[face.vip[1].texture_index]
                    array_uvs.append(array_val)
                    array_val = array_texvert[face.vip[2].texture_index]
                    array_uvs.append(array_val)
                # store(']];')

                # Flip UV normals
                store("""
                var faceVertexUvs = geometry.faceVertexUvs[ 0 ];
                for ( var i = 0; i < faceVertexUvs.length; i ++ )
                {
                    var temp = faceVertexUvs[ i ][ 0 ];
                    faceVertexUvs[ i ][ 0 ] = faceVertexUvs[ i ][ 2 ];
                    faceVertexUvs[ i ][ 2 ] = temp;
                }
                            """)

                # Scale it
                store('geometry.scale({}, {}, {});'.format(
                    mesh_data.frames[0].scale.x,
                    mesh_data.frames[0].scale.y,
                    mesh_data.frames[0].scale.z
                ))

                # Create the mesh
                #store('mesh = new THREE.Mesh( geometry, [{}] );'.format(', '.join(textures)))

                # Position the mesh
                store('mesh.position.set({}, {}, {});'.format(
                    mesh_data.frames[0].origin.x,
                    mesh_data.frames[0].origin.y,  # + (lod if is_lod_shape else 0),
                    mesh_data.frames[0].origin.z  # - (15 if (is_debris or is_hulk) else 0)
                ))

                # Add the mesh to the node's group
                store('node_{}.add(mesh);'.format(parent_node))

                # Blender - Create an object with the node's id
                mesh = bpy.data.meshes.new(obj_name)
                object = bpy.data.objects.new(obj_name, mesh)
                # Use node_index, NOT obj_id, to unify sort with skeleton nodes
                object["dts_object_index"] = objects[obj_id].node_index
                
                # Store original DTS frame scale/origin for correct export bounds
                # The exporter will use these instead of recalculating from geometry
                frame = frames[0]
                object["dts_frame_scale_x"] = frame.scale.x
                object["dts_frame_scale_y"] = frame.scale.y
                object["dts_frame_scale_z"] = frame.scale.z
                object["dts_frame_origin_x"] = frame.origin.x
                object["dts_frame_origin_y"] = frame.origin.y
                object["dts_frame_origin_z"] = frame.origin.z
                object["dts_import_scale"] = self.import_scale
                object["dts_mesh_radius"] = mesh_data.radius
                object["dts_vertex_count"] = mesh_data.num_vertices_per_frame
                # Also stamped per-object: the LOD organizer MOVES objects out
                # of the source collection into LOD_* collections, which made
                # the exporter's collection-prop lookup fail (no header splice).
                object["dts_source_file"] = path

                # Per-frame layout for vertex-morph (frame track) meshes: the
                # exporter's faithful path uses these to reproduce the original
                # vertex array layout and per-frame scale/origin exactly
                # (frames may overlap via first_vert, and each frame has its
                # own packing box).
                object["dts_total_vertices"] = mesh_data.num_vertices
                object["dts_frame_first_verts"] = [f.first_vert for f in frames]
                _scales = []
                _origins = []
                for f in frames:
                    _sc = getattr(f, 'scale', None) or getattr(mesh_data, 'scale_v2', None)
                    _og = getattr(f, 'origin', None) or getattr(mesh_data, 'origin_v2', None)
                    if _sc and _og:
                        _scales.extend([_sc.x, _sc.y, _sc.z])
                        _origins.extend([_og.x, _og.y, _og.z])
                if len(_scales) == 3 * len(frames):
                    object["dts_frame_scales"] = _scales
                    object["dts_frame_origins"] = _origins
                
                # The bounds/collision helper meshes aren't part of the visible
                # model -- draw them as wireframe so they don't render as
                # untextured boxes around the shape (they still export).
                if obj_name.lower() in ('bounds', 'collision'):
                    object.display_type = 'WIRE'
                    object.hide_render = True

                # Object flag 0x1 = hidden by default (e.g. "hide muzzle"
                # flash meshes, shown only by a visibility track during
                # "fire"). Stored here; the actual hiding happens after all
                # mesh editing (edit-mode ops can't run on hidden objects).
                object["dts_object_flags"] = int(objects[obj_id].flags)

                actual_object_name = object.name # Blender may append a .00x
                # Link via the collection REFERENCE, not by name: on re-import the
                # new collection may be named "file.001" and a name lookup would
                # put objects into the leftover old collection.
                obj_collection.objects.link(object)
                object = bpy.context.scene.objects[actual_object_name]
                obj_dts_to_blender_map[obj_id] = actual_object_name
                object.data = mesh
                # Move Blender 3d cursor to object's pivot point, then set object pivot to 3d cursor
                bpy.context.scene.cursor.location = (0, 0, 0) #(transforms[nodes[0].default_transform].translate.x, transforms[nodes[0].default_transform].translate.y, transforms[nodes[0].default_transform].translate.z)
                bpy.context.scene.cursor.rotation_quaternion = (1, 0, 0, 0)
                bpy.context.scene.cursor.rotation_euler = (0, 0, 0)
                bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
                
                # Vertices now contain world coordinates, don't apply additional scale/origin
                object.scale = (1.0, 1.0, 1.0)
                object.location = (0.0, 0.0, 0.0)  # Origin baked into vertex coords
                object.rotation_mode = 'QUATERNION'
                
                # Create the mesh of the object
                mesh.from_pydata(array_verts_all, [], array_faces)

                # Vertex-morph (frame track) animation: import frames 1..N-1 as
                # shape keys so morph animation survives editing and re-export
                # (e.g. the Sensor Jammer, and morph-animated custom weapons).
                # The exporter emits key_blocks[1:] as CelAnimMesh frames in
                # order, so key order here must match frame order.
                if mesh_data.num_frames > 1 and mesh_data.num_vertices_per_frame > 0:
                    object.shape_key_add(name='Basis', from_mix=False)
                    for f_idx in range(1, mesh_data.num_frames):
                        fr = frames[f_idx]
                        f_sc = getattr(fr, 'scale', None) or getattr(mesh_data, 'scale_v2', None)
                        f_og = getattr(fr, 'origin', None) or getattr(mesh_data, 'origin_v2', None)
                        key = object.shape_key_add(name='frame_{:03d}'.format(f_idx), from_mix=False)
                        # Blender 5.x adds shape keys with value 1.0 -- with 35
                        # relative keys all active the mesh becomes a stacked
                        # spike. They must start at 0 (the sequence loop
                        # animates them).
                        key.value = 0.0
                        if not (f_sc and f_og):
                            continue
                        f_start = fr.first_vert
                        for vrt_idx in range(mesh_data.num_vertices_per_frame):
                            vert = mesh_data.vertices[f_start + vrt_idx]
                            key.data[vrt_idx].co = (
                                (vert.x * f_sc.x + f_og.x) * self.import_scale,
                                (vert.y * f_sc.y + f_og.y) * self.import_scale,
                                (vert.z * f_sc.z + f_og.z) * self.import_scale)
                    print(f"  Imported {mesh_data.num_frames - 1} morph frame(s) as shape keys for '{obj_name}'")

                # NOTE: object-level animation (vertex-morph frame tracks) is
                # handled in the main sequence loop below so the keys share the
                # sequence's timeline range. The old per-mesh animate_meshes()
                # call only ever processed the object's FIRST subsequence (so a
                # leading visibility track hid the deploy morph entirely) and
                # laid keys on its own drifting frame counter.
                # Select object by name
                ob = bpy.context.scene.objects[actual_object_name]  # Get the object
                bpy.ops.object.select_all(action='DESELECT')  # Deselect all objects
                bpy.context.view_layer.objects.active = ob  # Make the desired object the active object
                ob.select_set(True)  # Select the object
                # Create the face maps (materials for faces)
                # Note: face_maps were removed in Blender 4.0+, using direct material assignment
                for tex in textures:
                    mat = bpy.data.materials.get(tex)
                    object.data.materials.append(mat)

                # Switch object modes to access polygon data
                ob = bpy.context.active_object
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_mode(type="FACE")
                bpy.ops.mesh.select_all(action='DESELECT')
                bpy.ops.object.mode_set(mode='OBJECT')
                
                # Loop through all faces and assign material directly to polygons
                for face_idx, poly in enumerate(ob.data.polygons):
                    if face_idx < len(array_faces_material):
                        poly.material_index = int(array_faces_material[face_idx])

                # Create the UV map
                new_uv = ob.data.uv_layers.new(name='UV Map')
                for loop in ob.data.loops:
                    new_uv.data[loop.index].uv = array_uvs[loop.index]

                obj_id += 1

            # Blender - Create Objects for all nodes, with dummy meshes, find parents
            # Map each DTS node index to the ACTUAL Blender object name created by
            # THIS import. All later lookups go through this map instead of raw
            # scene name lookups, which could resolve to stale same-named objects
            # from a previous import (breaking parenting and animation).
            created_mesh_by_dts_name = {}
            for _oid, _bname in obj_dts_to_blender_map.items():
                created_mesh_by_dts_name.setdefault(names[objects[_oid].name], _bname)

            node_blender_map = {}
            for node in nodes:
                dts_name = names[node.name]
                blender_name = created_mesh_by_dts_name.get(dts_name)
                if blender_name is None:
                    object = bpy.data.objects.new(dts_name, None)
                    object["dts_object_index"] = node.id # Use Node Index for sorting
                    object.rotation_mode = 'QUATERNION'
                    obj_collection.objects.link(object)
                    blender_name = object.name # Blender may append a .00x
                node_blender_map[node.id] = blender_name

            array_parents = []
            for node in nodes:
                if node.parent != -1:
                    array_parents.append([node_blender_map[node.id], node_blender_map[node.parent]])

            # Blender - Find parents for all objects
            pprint.pp(obj_dts_to_blender_map)
            for obj_id in range(len(objects)):
                obj = objects[obj_id]
                # Some objects don't get created in Blender (e.g. bounds)
                if obj_id not in obj_dts_to_blender_map:
                    continue

                array_val = [obj_dts_to_blender_map[obj_id], node_blender_map[obj.node_index]]
                if array_val[0] != array_val[1]: # mesh may BE its own node
                    array_parents.append(array_val)
                print(obj_id, obj_dts_to_blender_map[obj_id], obj.node_index, names[nodes[obj.node_index].name])
                        
            # Blender - Parent all the objects
            x = 0
            for obj in array_parents:
                #print(str(array_parents[x][0]), '->', str(array_parents[x][1]))
                bpy.ops.object.select_all(action='DESELECT')
                bpy.context.view_layer.objects.active = bpy.context.scene.objects[array_parents[x][1]] # Parent
                bpy.context.scene.objects[array_parents[x][1]].select_set(True) # Parent
                bpy.context.scene.objects[array_parents[x][0]].select_set(True) # First child
                bpy.ops.object.parent_set(type='OBJECT')
                x += 1
                    
            # Blender - Move the nodes
            for node in nodes:
                def_trans = transforms[nodes[node.id].default_transform]
                object = bpy.context.scene.objects[node_blender_map[node.id]]
                object.location = [def_trans.translate.x, def_trans.translate.y, def_trans.translate.z]
                #object.rotation_quaternion = [short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z), short2float(def_trans.rotate.w)]
                object.rotation_quaternion = [short2float(def_trans.rotate.w) * -1, short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z)]
            
            shape_data: Dts.TsShape = d.shape.data.obj_data
            # Create a panel to hold sequences
            store("""
            const panel = new GUI( { width: 310 } );
            const folder_lod = panel.addFolder('Level of Detail');
            const folder_seq = panel.addFolder('Sequences');

            var _sequences = {};
            """)

            store("""
            controller_settings = {{
                lod: {defLOD}
            }};
            """.format(defLOD=int(shape_data.details[0].size)))
            store('folder_lod.add( controller_settings, "lod", [ {} ] ).name( "Level" ).onChange( updateLod );'.format(
                ', '.join(str(int(x.size)) for x in shape_data.details)
            ))
            store('folder_lod.open();')

            for sequence in shape_data.sequences:
                # store(str(names[sequence.name]))
                seq_name_ascii = names[sequence.name]
                store('_sequences["{}"] = true;'.format(seq_name_ascii))
                store('folder_seq.add(_sequences, "{}");'.format(seq_name_ascii))

            store('folder_seq.open();')



            # Create the sequences
            scene = bpy.data.scenes['Scene']

            # Apply default-hidden object flags (bit 0x1) now that all mesh
            # editing is done: key the hidden state at frame 0 so visibility
            # tracks animate from the right base state.
            scene.frame_set(0)
            for _bname in obj_dts_to_blender_map.values():
                _ob = bpy.context.scene.objects.get(_bname)
                if _ob is None or not (_ob.get("dts_object_flags", 0) & 1):
                    continue
                _ob.hide_viewport = True
                _ob.hide_render = True
                _ob.keyframe_insert(data_path="hide_viewport")
                _ob.keyframe_insert(data_path="hide_render")

            # Iterate through all sequences and generate key frames for each object participating in that sequence
            for seq_id in range(len(shape_data.sequences)):
                # Before starting a sequence, reset nodes to their default
                # transform -- but ONLY nodes that this sequence animates.
                # Sequences without a node track (e.g. the jammer's "power",
                # which plays on the DEPLOYED shape) must not yank nodes back
                # to the rest pose at their boundary.
                scene.frame_set(frame_id)
                for node in nodes:
                    participates = False
                    for _sc in range(node.num_subsequences):
                        if subsequences[node.first_subsequence + _sc].sequence_index == seq_id:
                            participates = True
                            break
                    if not participates:
                        continue
                    def_trans = transforms[nodes[node.id].default_transform]
                    object = bpy.context.scene.objects[node_blender_map[node.id]]
                    object.location = [def_trans.translate.x, def_trans.translate.y, def_trans.translate.z]
                    object.rotation_quaternion = [short2float(def_trans.rotate.w) * -1, short2float(def_trans.rotate.x), short2float(def_trans.rotate.y), short2float(def_trans.rotate.z)]
                    object.keyframe_insert(data_path="rotation_quaternion", index=-1)
                    object.keyframe_insert(data_path="location", index=-1)
                frame_id += 1

                sequence: Dts.VectorSequence = shape_data.sequences[seq_id]
                seq_name = names[sequence.name]
                print(seq_name)
                scene.timeline_markers.new(seq_name, frame=frame_id)

                if sequence.num_ifl_subsequences > 0:
                    # IFL sequence
                    print("IFL sequence")

                    # A sequence may have multiple IFL subsequences, for different materials
                    for subseq_count in range(sequence.num_ifl_subsequences):
                        subseq = subsequences[sequence.first_ifl_subsequence + subseq_count]
                        #print('num key frames:', subseq.num_keyframes)

                        first_keyframe = subseq.first_keyframe
                        ifl_mat = bpy.data.materials.get('ifl_{}_{}'.format(seq_name, subseq_count))
                        value_node = ifl_mat.node_tree.nodes.get("IFL Input Value")

                        val = 0
                        for key in range(first_keyframe, first_keyframe + subseq.num_keyframes):
                            # Set the frame BEFORE inserting: keyframe_insert keys
                            # at the CURRENT frame, so the old order put every key
                            # one step behind (and the first key wherever the
                            # playhead happened to be).
                            scene.frame_set(frame_id)
                            value_node.outputs["Value"].default_value = val
                            value_node.outputs["Value"].keyframe_insert(data_path="default_value", index=-1)
                            val += 1
                            frame_id += 1

                else:
                    # Node sequence
                    # Find the node with a sequence that corresponds to it
                    node_id = 0
                    last_subseq_len = 0
                    for node in nodes:
                        if node.num_subsequences:

                            # A node may have multiple subsequences, go through all of them
                            for subseq_count in range(node.num_subsequences):
                                subseq = subsequences[node.first_subsequence + subseq_count]
                                if subseq.sequence_index == seq_id:
                                    #print('num key frames:', subseq.num_keyframes)
                                    first_keyframe = subseq.first_keyframe

                                    #Blender
                                    blender_frame = frame_id
                                    object = bpy.context.scene.objects[node_blender_map[node_id]]
                                    # Actions will be created for each object animated. Bones will need to be created to be used with armors.
                                    #object.animation_data_create() #
                                    #object.animation_data.action = bpy.data.actions.new(name=seq_name) #
                                    for key in range(first_keyframe, first_keyframe + subseq.num_keyframes):
                                        trans = transforms[keyframes[key].key_value]
                                        scene.frame_set(blender_frame) #Blender
                                        object.location = [trans.translate.x, trans.translate.y, trans.translate.z]
                                        object.rotation_quaternion = [short2float(trans.rotate.w) * -1, short2float(trans.rotate.x), short2float(trans.rotate.y), short2float(trans.rotate.z)] #Blender
                                        object.keyframe_insert(data_path="rotation_quaternion", index=-1)
                                        object.keyframe_insert(data_path="location", index=-1)
                                        blender_frame += 1 #Blender
                                    last_subseq_len = subseq.num_keyframes

                        node_id += 1

                    # Object tracks: vertex-morph (frame track) subsequences.
                    # Keys go at this sequence's frame range, one morph frame
                    # per timeline frame, exactly one shape key active at a time.
                    for obj_i in range(len(objects)):
                        obj_rec = objects[obj_i]
                        if not obj_rec.num_subsequences or obj_i not in obj_dts_to_blender_map:
                            continue
                        for ss in range(obj_rec.num_subsequences):
                            subseq = subsequences[obj_rec.first_subsequence + ss]
                            if subseq.sequence_index != seq_id:
                                continue
                            first_keyframe = subseq.first_keyframe

                            # Visibility track: key hide_viewport/hide_render so
                            # e.g. muzzle flashes only show during "fire",
                            # returning to the object's default state after.
                            # The visible state is mat_index bit 0x8000 (NOT
                            # key_value), and each key has a fractional
                            # position within the sequence -- honor it so keys
                            # stay inside this sequence's frame range instead
                            # of spilling into the next one.
                            if keyframes[first_keyframe].mat_index & FLAG_VISIBILITY_TRACK:
                                ob = bpy.context.scene.objects[obj_dts_to_blender_map[obj_i]]
                                seq_len = max(last_subseq_len, subseq.num_keyframes + 1)
                                for key in range(first_keyframe, first_keyframe + subseq.num_keyframes):
                                    kf = keyframes[key]
                                    hidden = not (kf.mat_index & FLAG_VISIBILITY_VISIBLE)
                                    pos = getattr(kf, 'position', 0.0) or 0.0
                                    scene.frame_set(frame_id + int(round(pos * seq_len)))
                                    ob.hide_viewport = hidden
                                    ob.hide_render = hidden
                                    ob.keyframe_insert(data_path="hide_viewport")
                                    ob.keyframe_insert(data_path="hide_render")
                                default_hidden = bool(ob.get("dts_object_flags", 0) & 1)
                                scene.frame_set(frame_id + seq_len)
                                ob.hide_viewport = default_hidden
                                ob.hide_render = default_hidden
                                ob.keyframe_insert(data_path="hide_viewport")
                                ob.keyframe_insert(data_path="hide_render")
                                for fc in _action_fcurves(ob.animation_data):
                                    if fc.data_path.startswith('hide'):
                                        for kp in fc.keyframe_points:
                                            kp.interpolation = 'CONSTANT'
                                last_subseq_len = max(last_subseq_len, seq_len)
                                continue

                            if not (keyframes[first_keyframe].mat_index & FLAG_FRAME_TRACK):
                                continue
                            ob = bpy.context.scene.objects[obj_dts_to_blender_map[obj_i]]
                            sk_data = ob.data.shape_keys
                            if not sk_data:
                                print('  frame track on {} but no shape keys; skipped'.format(ob.name))
                                continue
                            sk_data.use_relative = True

                            # key_value is the morph frame index; frame 0 is the
                            # Basis (represented as None -> all keys off).
                            # Each key carries a fractional position within the
                            # sequence -- place it there so the morph spans the
                            # sequence's full frame range like in the engine.
                            seq_len_ft = max(last_subseq_len, subseq.num_keyframes)
                            entries = []
                            for key in range(first_keyframe, first_keyframe + subseq.num_keyframes):
                                kf = keyframes[key]
                                sk = sk_data.key_blocks.get('frame_{:03d}'.format(kf.key_value))
                                pos = getattr(kf, 'position', 0.0) or 0.0
                                entries.append((frame_id + int(round(pos * seq_len_ft)), sk))
                            if all(fr == entries[0][0] for fr, _ in entries):
                                # no usable positions; fall back to sequential
                                entries = [(frame_id + n, sk) for n, (_, sk) in enumerate(entries)]

                            # Anchor every used key at 0 at the sequence start:
                            # without this, fcurve extrapolation holds a key's
                            # FIRST keyed value (1) backwards to frame 0 and the
                            # relative keys stack into stretched geometry.
                            scene.frame_set(frame_id)
                            for sk in set(s for _, s in entries if s is not None):
                                sk.value = 0
                                sk.keyframe_insert(data_path="value", index=-1)

                            prev_sk = None
                            for fr, sk in entries:
                                scene.frame_set(fr)
                                if sk != prev_sk:
                                    if prev_sk is not None:
                                        prev_sk.value = 0
                                        prev_sk.keyframe_insert(data_path="value", index=-1)
                                    if sk is not None:
                                        sk.value = 1
                                        sk.keyframe_insert(data_path="value", index=-1)
                                    prev_sk = sk

                            # Morph switching must be a hard step: the
                            # keyframe_new_interpolation_type preference is
                            # ignored by keyframe_insert in Blender 5, and
                            # BEZIER ramps leave a dozen relative keys partially
                            # active at once (stretched-spike geometry).
                            for fc in _action_fcurves(sk_data.animation_data):
                                for kp in fc.keyframe_points:
                                    kp.interpolation = 'CONSTANT'
                            last_subseq_len = max(last_subseq_len, subseq.num_keyframes)

                    frame_id += last_subseq_len
                scene.timeline_markers.new('End of {}'.format(seq_name), frame=frame_id)

            # Extend the playback range to cover every imported sequence
            # (Blender's default End of 250 cuts the loop off partway through).
            scene.frame_start = 0
            if scene.frame_end < frame_id:
                scene.frame_end = frame_id
            scene.frame_set(0)

        # =========================================================================
        # LOD ORGANIZATION: Create collections for each LOD level
        # =========================================================================
        if self.organize_by_lod:
            # Use the shape's ACTUAL detail sizes (characters are 36/10/2, but
            # deployables use e.g. 15/4/1) instead of hardcoded buckets. Only
            # organize the meshes created by THIS import.
            try:
                detail_sizes = sorted({int(d.size) for d in shape_data.details}, reverse=True)
            except NameError:
                detail_sizes = []
            rank_labels = ['High', 'Medium', 'Low']
            size_to_coll = {}
            for rank, size in enumerate(detail_sizes):
                label = rank_labels[rank] if rank < len(rank_labels) else 'L{}'.format(rank)
                size_to_coll[size] = 'LOD_{:02d}_{}'.format(size, label)

            def _get_lod_coll(name):
                coll = bpy.data.collections.get(name)
                if coll is None:
                    coll = bpy.data.collections.new(name)
                    context.scene.collection.children.link(coll)
                return coll

            moved_counts = {}
            for actual_name in obj_dts_to_blender_map.values():
                obj = bpy.context.scene.objects.get(actual_name)
                if obj is None or obj.type != 'MESH':
                    continue

                # Trailing number of the DTS name = detail size ("jammer 15",
                # "submesh_head 36"); strip any Blender .00x suffix first
                base = re.sub(r'\.\d+$', '', obj.name)
                m = re.search(r'(\d+)$', base)
                coll_name = 'LOD_Other'
                if m and int(m.group(1)) in size_to_coll:
                    coll_name = size_to_coll[int(m.group(1))]

                target_coll = _get_lod_coll(coll_name)

                # Unlink from ALL current collections first (this makes eye toggle work!)
                for coll in list(obj.users_collection):
                    coll.objects.unlink(obj)

                # Link to LOD collection
                target_coll.objects.link(obj)
                moved_counts[coll_name] = moved_counts.get(coll_name, 0) + 1

            # NOTE: deliberately NOT auto-hiding the lower LOD collections.
            # Hidden objects can't be selected, which silently breaks
            # select-all round-trip exports (only the visible LOD gets
            # exported and the header splice bails). Use the outliner eye
            # icons to hide LODs manually while editing.
            print("LOD Organization:", moved_counts)
                    
        return {'FINISHED'}