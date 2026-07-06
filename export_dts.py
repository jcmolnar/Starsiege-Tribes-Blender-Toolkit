# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
DTS Exporter for Starsiege: Tribes
Exports Blender meshes to the DTS (Dynamix Three Space) format.

Based on the dts.ksy Kaitai Struct specification.
"""

import bpy
import bmesh
import struct
import math
import os
import mathutils
import re
from bpy_extras.io_utils import ExportHelper
from bpy.props import StringProperty, BoolProperty, EnumProperty, FloatProperty
from io import BytesIO

# Add addon folder to sys.path for local kaitaistruct
import sys
_addon_dir = os.path.dirname(__file__)
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

from kaitaistruct import KaitaiStream

# Use the same DTS parser as the importer (dts.py). Previously this module
# relied on the untracked scratch file dts_inspect.py and on a bare `Dts`
# name that was never imported (a latent NameError swallowed by try/except,
# which silently disabled name-based mesh->bone mapping during round-trips).
try:
    from .dts import Dts
except ImportError:
    from dts import Dts

print("DEBUG: LOADING export_dts.py MODULE v1.0.1 - FLATTEN HIERARCHY SUPPORT")



# DTS format constants
PERS_MAGIC = 0x53524550  # "PERS" in little-endian

# Different class types use different versions
SHAPE_VERSION = 8   # TS::Shape version
MESH_VERSION = 3    # TS::CelAnimMesh version  
MATERIAL_VERSION = 4  # TS::MaterialList version

# Material flags -- CORRECTED to the engine's actual layout (ts_material.h).
# fFlags packs three FIELDS, not a bitmask of effects:
#   type nibble  0x000f: 0=null 1=palette 2=RGB 3=TEXTURE
#   shading      0x0f00: 0x100=none (fullbright) 0x200=flat 0x400=smooth
#   texture      0xf000: 0x1000=transparent (cutout) 0x2000=translucent (blend)
# (The previous constants here -- 0x4 "translucent", 0x8 "additive", SWRAP/
# TWRAP etc. -- came from a different engine generation and set meaningless
# bits in Tribes/Darkstar; there is no additive-blend material flag at all.)
MAT_TYPE_TEXTURE = 0x3
MAT_SHADING_MASK = 0xf00
MAT_SHADING_NONE = 0x100      # fullbright: flames, glows, self-illuminated
MAT_SHADING_SMOOTH = 0x400
MAT_TEX_TRANSPARENT = 0x1000  # palette index 0 = cutout
MAT_TEX_TRANSLUCENT = 0x2000  # alpha blended (index 0 transparent)

# Keyframe type flags (from mat_index field)
FLAG_FRAME_TRACK = 0x1000      # Frame track animation (shape keys/vertex morphing)
FLAG_MATERIAL_TRACK = 0x2000   # Material track animation
FLAG_VISIBILITY_TRACK = 0x4000 # Visibility track animation

# Normal lookup table (256 pre-computed normals used by DTS)
# Extracted from Darkstar engine source: ts_vertex.cpp - PackedVertex::fNormalTable
# DO NOT MODIFY - This must match the engine's exact values for correct lighting.
NORMAL_TABLE = [
    (0.565061, -0.270644, -0.779396),
    (-0.309804, -0.731114, 0.607860),
    (-0.867412, 0.472957, 0.154619),
    (-0.757488, 0.498188, -0.421925),
    (0.306834, -0.915340, 0.260778),
    (0.098754, 0.639153, -0.762713),
    (0.713706, -0.558862, -0.422252),
    (-0.890431, -0.407603, -0.202466),
    (0.848050, -0.487612, -0.207475),
    (-0.232226, 0.776855, 0.585293),
    (-0.940195, 0.304490, -0.152706),
    (0.602019, -0.491878, -0.628991),
    (-0.096835, -0.494354, -0.863850),
    (0.026630, -0.323659, -0.945799),
    (0.019208, 0.909386, 0.415510),
    (0.854440, 0.491730, 0.167731),
    (-0.418835, 0.866521, -0.271512),
    (0.465024, 0.409667, 0.784809),
    (-0.674391, -0.691087, -0.259992),
    (0.303858, -0.869270, -0.389922),
    (0.991333, 0.090061, -0.095640),
    (-0.275924, -0.369550, 0.887298),
    (0.426545, -0.465962, 0.775202),
    (-0.482741, -0.873278, -0.065920),
    (0.063616, 0.932012, -0.356800),
    (0.624786, -0.061315, 0.778385),
    (-0.530300, 0.416850, 0.738253),
    (0.312144, -0.757028, -0.573999),
    (0.399288, -0.587091, -0.704197),
    (-0.132698, 0.482877, 0.865576),
    (0.950966, 0.306530, 0.041268),
    (-0.015923, -0.144300, 0.989406),
    (-0.407522, -0.854193, 0.322925),
    (-0.932398, 0.220464, 0.286408),
    (0.477509, 0.876580, 0.059936),
    (0.337133, 0.932606, -0.128796),
    (-0.638117, 0.199338, 0.743687),
    (-0.677454, 0.445349, 0.585423),
    (-0.446715, 0.889059, -0.100099),
    (-0.410024, 0.909168, 0.072759),
    (0.708462, 0.702103, -0.071641),
    (-0.048801, -0.903683, -0.425411),
    (-0.513681, -0.646901, 0.563606),
    (-0.080022, 0.000676, -0.996793),
    (0.066966, -0.991150, -0.114615),
    (-0.245220, 0.639318, -0.728793),
    (0.250978, 0.855979, 0.452006),
    (-0.123547, 0.982443, -0.139791),
    (-0.794825, 0.030254, -0.606084),
    (-0.772905, 0.547941, 0.319967),
    (0.916347, 0.369614, -0.153928),
    (-0.388203, 0.105395, 0.915527),
    (-0.700468, -0.709334, 0.078677),
    (-0.816193, 0.390455, 0.425880),
    (-0.043007, 0.769222, -0.637533),
    (0.911444, 0.113150, 0.395560),
    (0.845801, 0.156091, -0.510153),
    (0.829801, -0.029340, 0.557287),
    (0.259529, 0.416263, 0.871418),
    (0.231128, -0.845982, 0.480515),
    (-0.626203, -0.646168, 0.436277),
    (-0.197047, -0.065791, 0.978184),
    (-0.255692, -0.637488, -0.726794),
    (0.530662, -0.844385, -0.073567),
    (-0.779887, 0.617067, -0.104899),
    (0.739908, 0.113984, 0.662982),
    (-0.218801, 0.930194, -0.294729),
    (-0.374231, 0.818666, 0.435589),
    (-0.720250, -0.028285, 0.693137),
    (0.075389, 0.415049, 0.906670),
    (-0.539724, -0.106620, 0.835063),
    (-0.452612, -0.754669, -0.474991),
    (0.682822, 0.581234, -0.442629),
    (0.002435, -0.618462, -0.785811),
    (-0.397631, 0.110766, -0.910835),
    (0.133935, -0.985438, 0.104754),
    (0.759098, -0.608004, 0.232595),
    (-0.825239, -0.256087, 0.503388),
    (0.101693, -0.565568, 0.818408),
    (0.386377, 0.793546, -0.470104),
    (-0.520516, -0.840690, 0.149346),
    (-0.784549, -0.479672, 0.392935),
    (-0.325322, -0.927581, -0.183735),
    (-0.069294, -0.428541, 0.900861),
    (0.993354, -0.115023, -0.004288),
    (-0.123896, -0.700568, 0.702747),
    (-0.438031, -0.120880, -0.890795),
    (0.063314, 0.813233, 0.578484),
    (0.322045, 0.889086, -0.325289),
    (-0.133521, 0.875063, -0.465228),
    (0.637155, 0.564814, 0.524422),
    (0.260092, -0.669353, 0.695930),
    (0.953195, 0.040485, -0.299634),
    (-0.840665, -0.076509, 0.536124),
    (-0.971350, 0.202093, 0.125047),
    (-0.804307, -0.396312, -0.442749),
    (-0.936746, 0.069572, 0.343027),
    (0.426545, -0.465962, 0.775202),
    (0.794542, -0.227450, 0.563000),
    (-0.892172, 0.091169, -0.442399),
    (-0.312654, 0.541264, 0.780564),
    (0.590603, -0.735618, -0.331743),
    (-0.098040, -0.986713, 0.129558),
    (0.569646, 0.283078, -0.771603),
    (0.431051, -0.407385, -0.805129),
    (-0.162087, -0.938749, -0.304104),
    (0.241533, -0.359509, 0.901341),
    (-0.576191, 0.614939, 0.538380),
    (-0.025110, 0.085740, 0.996001),
    (-0.352693, -0.198168, 0.914515),
    (-0.604577, 0.700711, 0.378802),
    (0.465024, 0.409667, 0.784809),
    (-0.254684, -0.030474, -0.966544),
    (-0.604789, 0.791809, 0.085259),
    (-0.705147, -0.399298, 0.585943),
    (0.185691, 0.017236, -0.982457),
    (0.044588, 0.973094, 0.226052),
    (-0.405463, 0.642367, 0.650357),
    (-0.563959, 0.599136, -0.568319),
    (0.367162, -0.072253, -0.927347),
    (0.960429, -0.213570, -0.178783),
    (-0.192629, 0.906005, 0.376893),
    (-0.199718, -0.359865, -0.911378),
    (0.485072, 0.121233, -0.866030),
    (0.467163, -0.874294, 0.131792),
    (-0.638953, -0.716603, 0.279677),
    (-0.622710, 0.047813, -0.780990),
    (0.828724, -0.054433, -0.557004),
    (0.130241, 0.991080, 0.028245),
    (0.310995, -0.950076, -0.025242),
    (0.818118, 0.275336, 0.504850),
    (0.676328, 0.387023, 0.626733),
    (-0.100433, 0.495114, -0.863004),
    (-0.949609, -0.240681, -0.200786),
    (-0.102610, 0.261831, -0.959644),
    (-0.845732, -0.493136, 0.203850),
    (0.672617, -0.738838, 0.041290),
    (0.380465, 0.875938, 0.296613),
    (-0.811223, 0.262027, -0.522742),
    (-0.074423, -0.775670, -0.626736),
    (-0.286499, 0.755850, -0.588735),
    (0.291182, -0.276189, -0.915933),
    (-0.638117, 0.199338, 0.743687),
    (0.439922, -0.864433, -0.243359),
    (0.177649, 0.206919, 0.962094),
    (0.277107, 0.948521, 0.153361),
    (0.507629, 0.661918, -0.551523),
    (-0.503110, -0.579308, -0.641313),
    (0.600522, 0.736495, -0.311364),
    (-0.691096, -0.715301, -0.103592),
    (-0.041083, -0.858497, 0.511171),
    (0.207773, -0.480062, -0.852274),
    (0.795719, 0.464614, 0.388543),
    (-0.100433, 0.495114, -0.863004),
    (0.703249, 0.065157, -0.707951),
    (-0.324171, -0.941112, 0.096024),
    (-0.134933, -0.940212, 0.312722),
    (-0.438240, 0.752088, -0.492249),
    (0.964762, -0.198855, 0.172311),
    (-0.831799, 0.196807, 0.519015),
    (-0.508008, 0.819902, 0.263986),
    (0.471075, -0.001146, 0.882092),
    (0.919512, 0.246162, -0.306435),
    (-0.960050, 0.279828, -0.001187),
    (0.110232, -0.847535, -0.519165),
    (0.208229, 0.697360, 0.685806),
    (-0.199680, -0.560621, 0.803637),
    (0.170135, -0.679985, -0.713214),
    (0.758371, -0.494907, 0.424195),
    (0.077734, -0.755978, 0.649965),
    (0.612831, -0.672475, 0.414987),
    (0.142776, 0.836698, -0.528726),
    (-0.765185, 0.635778, 0.101382),
    (0.669873, -0.419737, 0.612447),
    (0.593549, 0.194879, 0.780847),
    (0.646930, 0.752173, 0.125368),
    (0.837721, 0.545266, -0.030127),
    (0.541505, 0.768070, 0.341820),
    (0.760679, -0.365715, -0.536301),
    (0.381516, 0.640377, 0.666605),
    (0.565794, -0.072415, -0.821361),
    (-0.466072, -0.401588, 0.788356),
    (0.987146, 0.096290, 0.127560),
    (0.509709, -0.688886, -0.515396),
    (-0.135132, -0.988046, -0.074192),
    (0.600499, 0.476471, -0.642166),
    (-0.732326, -0.275320, -0.622815),
    (-0.881141, -0.470404, 0.048078),
    (0.051548, 0.601042, 0.797553),
    (0.402027, -0.763183, 0.505891),
    (0.404233, -0.208288, 0.890624),
    (-0.311793, 0.343843, 0.885752),
    (0.098132, -0.937014, 0.335223),
    (0.537158, 0.830585, -0.146936),
    (0.725277, 0.298172, -0.620538),
    (-0.882025, 0.342976, -0.323110),
    (-0.668829, 0.424296, -0.610443),
    (-0.408835, -0.476442, -0.778368),
    (0.809472, 0.397249, -0.432375),
    (-0.909184, -0.205938, -0.361903),
    (0.866930, -0.347934, -0.356895),
    (0.911660, -0.141281, -0.385897),
    (-0.431404, -0.844074, -0.318480),
    (-0.950593, -0.073496, 0.301614),
    (-0.719716, 0.626915, -0.298305),
    (-0.779887, 0.617067, -0.104899),
    (-0.475899, -0.542630, 0.692151),
    (0.081952, -0.157248, -0.984153),
    (0.923990, -0.381662, -0.024025),
    (-0.957998, 0.120979, -0.260008),
    (0.306601, 0.227975, -0.924134),
    (-0.141244, 0.989182, 0.039601),
    (0.077097, 0.186288, -0.979466),
    (-0.630407, -0.259801, 0.731499),
    (0.718150, 0.637408, 0.279233),
    (0.340946, 0.110494, 0.933567),
    (-0.396671, 0.503020, -0.767869),
    (0.636943, -0.245005, 0.730942),
    (-0.849605, -0.518660, -0.095724),
    (-0.388203, 0.105395, 0.915527),
    (-0.280671, -0.776541, -0.564099),
    (-0.601680, 0.215451, -0.769131),
    (-0.660112, -0.632371, -0.405412),
    (0.921096, 0.284072, 0.266242),
    (0.074850, -0.300846, 0.950731),
    (0.943952, -0.067062, 0.323198),
    (-0.917838, -0.254589, 0.304561),
    (0.889843, -0.409008, 0.202219),
    (-0.565849, 0.753721, -0.334246),
    (0.791460, 0.555918, -0.254060),
    (0.261936, 0.703590, -0.660568),
    (-0.234406, 0.952084, 0.196444),
    (0.111205, 0.979492, -0.168014),
    (-0.869844, -0.109095, -0.481113),
    (-0.337728, -0.269701, -0.901777),
    (0.366793, 0.408875, -0.835634),
    (-0.098749, 0.261316, 0.960189),
    (-0.272379, -0.847100, 0.456324),
    (-0.319506, 0.287444, -0.902935),
    (0.873383, -0.294109, 0.388203),
    (-0.088950, 0.710450, 0.698104),
    (0.551238, -0.786552, 0.278340),
    (0.724436, -0.663575, -0.186712),
    (0.529741, -0.606539, 0.592861),
    (-0.949743, -0.282514, 0.134809),
    (0.155047, 0.419442, -0.894443),
    (-0.562653, -0.329139, -0.758346),
    (0.816407, -0.576953, 0.024576),
    (0.178550, -0.950242, -0.255266),
    (0.479571, 0.706691, 0.520192),
    (0.391687, 0.559884, -0.730145),
    (0.724872, -0.205570, -0.657496),
    (-0.663196, -0.517587, -0.540624),
    (-0.660054, -0.122486, -0.741165),
    (-0.531989, 0.374711, -0.759328),
    (0.194979, -0.059120, 0.979024),
]


# Memoization cache for find_closest_normal. Meshes reuse the same loop
# normals heavily (flat-shaded faces, mirrored geometry), so caching on the
# rounded components turns ~256 distance tests per face corner into a dict hit.
_normal_cache = {}


def find_closest_normal(normal):
    """Find the index of the closest pre-computed normal using Euclidean distance.

    This matches the Darkstar engine's PackedVertex::encodeNormal() which uses
    m_dist() (Euclidean distance) to find the best fit, NOT dot product.
    (Squared distance is compared here; argmin is identical without the sqrt.)
    """
    key = (round(normal[0], 4), round(normal[1], 4), round(normal[2], 4))
    cached = _normal_cache.get(key)
    if cached is not None:
        return cached
    best_idx = 0
    best_dist = 999999.0
    for i, n in enumerate(NORMAL_TABLE):
        dist = (normal[0] - n[0])**2 + (normal[1] - n[1])**2 + (normal[2] - n[2])**2
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    _normal_cache[key] = best_idx
    return best_idx


def material_map_file(mat):
    """Texture filename for the DTS material list.

    Prefer the actual image plugged into the material's node tree (what the
    engine must load), falling back to the material name. Blender-side .png
    conversions are mapped back to the engine's .bmp naming, mirroring the
    importer's .bmp -> .png substitution.
    """
    try:
        if mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    name = os.path.basename(node.image.filepath) or node.image.name
                    base, ext = os.path.splitext(name)
                    if ext.lower() == '.png':
                        name = base + '.bmp'
                    if name:
                        return name
    except Exception:
        pass
    return mat.name


def iter_anim_fcurves(anim_data):
    """Yield fcurves from AnimData across legacy AND slotted (Blender 4.4+)
    actions. Blender 5.0 removed Action.fcurves for layered actions; the
    curves live in a channelbag keyed by the animated ID's action slot.
    """
    act = anim_data.action if anim_data else None
    if act is None:
        return
    legacy = getattr(act, 'fcurves', None)
    if legacy is not None:
        yield from legacy
        return
    slot = getattr(anim_data, 'action_slot', None)
    for layer in getattr(act, 'layers', ()):
        for strip in getattr(layer, 'strips', ()):
            cb = None
            if slot is not None and hasattr(strip, 'channelbag'):
                try:
                    cb = strip.channelbag(slot)
                except Exception:
                    cb = None
            if cb is not None:
                yield from cb.fcurves


def get_object_ancestry(obj):
    """Returns a list of objects from the given object up to the root parent.
    First item of list is the object itself, last is root.
    """
    chain = []
    current = obj
    while current:
        chain.append(current)
        current = current.parent
    return chain


# ============================================================================
# COORDINATE SYSTEM CONVERSION
# Blender uses Z-up, right-handed. DTS/Darkstar uses Y-up, right-handed.
# Mapping: Blender X -> DTS X, Blender Z -> DTS Y, Blender -Y -> DTS Z
# ============================================================================

def blender_to_dts_point(co):
    """Convert a Blender coordinate to DTS coordinate system.
    
    Blender (Z-up) -> DTS (Y-up):
        X -> X
        Z -> Y (up)
       -Y -> Z (forward)
    """
    return (co[0], co[2], -co[1])


def blender_to_dts_normal(n):
    """Convert a Blender normal vector to DTS coordinate system."""
    return (n[0], n[2], -n[1])


def blender_to_dts_quat(q):
    """Convert a Blender quaternion to DTS coordinate system.
    
    For quaternions, the axis components follow the same mapping as vectors,
    but W stays as-is.
    """
    # q = (w, x, y, z) in Blender
    # DTS expects: (x, y, z, w) with coordinate swap
    return (q.x, q.z, -q.y, q.w)


def get_accumulated_parent_scale(obj):
    """Compute the accumulated scale from root to obj's parent.
    
    Walks up the hierarchy from obj.parent to root, multiplying all local scales.
    Returns a Vector(1,1,1) if obj has no parent.
    """
    if not obj.parent:
        return mathutils.Vector((1.0, 1.0, 1.0))
    
    accumulated = mathutils.Vector((1.0, 1.0, 1.0))
    current = obj.parent
    while current:
        accumulated.x *= current.scale.x
        accumulated.y *= current.scale.y
        accumulated.z *= current.scale.z
        current = current.parent
    return accumulated

def collect_object_keyframes(obj, scene, context, flatten_hierarchy=False, frame_range=None, scale=1.0):
    """Collect keyframes from an object's animation data.
    
    Returns a list of tuples: (frame_number, rotation_quaternion, location)
    """
    keyframes = []
    
    # Determine if object actually has animation data
    has_action = obj.animation_data and obj.animation_data.action
    has_shape_keys = obj.data and hasattr(obj.data, 'shape_keys') and obj.data.shape_keys
    
    if not has_action and not has_shape_keys and not frame_range:
        return []

    keyframes = []
    
    # If a specific range is requested, we sample directly.
    # We should still check if the object's matrix actually CHANGES over this range.
    if frame_range:
        start_frame, end_frame = frame_range
    else:
        # Fallback to current action range
        if not has_action:
            return []
        action = obj.animation_data.action
        start_frame = int(action.frame_range[0])
        end_frame = int(action.frame_range[1])
    
    depsgraph = context.evaluated_depsgraph_get()
    
    first_mat = None
    is_animated = False
    
    for frame in range(start_frame, end_frame + 1):
        scene.frame_set(frame)
        obj_eval = obj.evaluated_get(depsgraph)
        
        if not flatten_hierarchy and obj_eval.parent:
            mat = obj_eval.matrix_local.copy()
        else:
            mat = obj_eval.matrix_world.copy()
            
        rot = mat.to_quaternion()
        # Compute ACCUMULATED scale from root to this node's parent.
        # This ensures all descendants of the scaled root get properly scaled translations.
        if not flatten_hierarchy and obj.parent:
            accumulated_scale = get_accumulated_parent_scale(obj)
            loc = mathutils.Vector((
                mat.translation.x * accumulated_scale.x * scale,
                mat.translation.y * accumulated_scale.y * scale,
                mat.translation.z * accumulated_scale.z * scale
            ))
        else:
            loc = mat.translation * scale
        
        # Visibility check (Simplified - Tribes uses bit 15)
        # 0x8000 = Visible
        is_visible = not (obj.hide_viewport or obj.hide_render)
        mat_idx = 0x8000 if is_visible else 0x0000
        
        if first_mat is None:
            first_mat = mat.copy()
            first_rot = rot.copy()
            first_loc = loc.copy()
        elif not is_animated:
            # Check for any change in rotation/translation
            loc_diff = (loc - first_loc).length
            rot_diff = rot.rotation_difference(first_rot).angle
            if loc_diff > 0.02 or rot_diff > 0.02:
                is_animated = True
            
            # HARD FILTER: Force static nodes to be non-animated to prevent bloat/crashes
            # These nodes are structural roots and should never have animation tracks.
            name_lower = obj.name.lower()
            # 'dummy' catches 'dummyalways', 'dummy hand', 'dummy eye', etc.
            limiters = ['always', 'dummy', 'bounds', 'vicon', 'mount', 'cam', 'eye']
            if any(bad in name_lower for bad in limiters):
                is_animated = False
                
        keyframes.append({'frame': frame, 'rot': rot, 'loc': loc, 'mat_idx': mat_idx})
        
    # AUTOMATED MOVEMENT DETECTION:
    # If it's a sequence range, we only include nodes that actually CHANGE.
    # This matches Axe.dts behavior (only muzzles move).
    if frame_range:
        if is_animated:
            return keyframes
        return []

    # Bind pose defaults ALWAYS return (used for the transform table)
    return keyframes


def process_node_hierarchy(objects, writer, flatten_hierarchy=False):
    """Builds DTS node hierarchy from Blender objects."""
    nodes = []
    obj_to_node_idx = {}
    
    # Create mapping of blender object -> node index
    # We maintain the order of 'objects' list so indices match
    for i, obj in enumerate(objects):
        obj_to_node_idx[obj] = i
        
    # Build basic nodes with parent links
    
    # 1. Identify the Main Root (Node 0)
    # Usually 'bounds'. If not found, use first object.
    root_obj_idx = -1
    for i, obj in enumerate(objects):
        if obj.name.lower() == 'bounds':
            root_obj_idx = i
            break
    if root_obj_idx == -1 and objects:
        root_obj_idx = 0
        
    # Rearrange objects to ensure Root Object is at index 0 (if not already)
    # Actually, we preserve the input order or handle it via parent logic.
    # DTS often requires Node 0 to be the root of the hierarchy.
    
    for i, obj in enumerate(objects):
        parent_idx = -1
        
        if flatten_hierarchy:
            if i == root_obj_idx:
                parent_idx = -1
            else:
                parent_idx = root_obj_idx
        else:
            if obj.parent and obj.parent in obj_to_node_idx:
                parent_idx = obj_to_node_idx[obj.parent]
            else:
                parent_idx = -1
            
        nodes.append({
            'name': writer.add_name(obj.name),
            'parent': parent_idx,
            'child': -1,
            'sibling': -1,
            'first_object': -1, 
        })

    # 2. Link children and siblings 
    # (DTS uses first-child/next-sibling structure)
    for i in range(len(nodes)):
        # Find all children of Node i
        # Filter for nodes where parent == i
        child_indices = [j for j, n in enumerate(nodes) if n['parent'] == i]
        
        if child_indices:
            # Sort children by name for deterministic ordering
            child_indices.sort(key=lambda j: objects[j].name)
            
            # Parent links to its first child
            nodes[i]['child'] = child_indices[0]
            
            # Each child links to the next child (sibling)
            for k in range(len(child_indices) - 1):
                curr = child_indices[k]
                next_node = child_indices[k+1]
                nodes[curr]['sibling'] = next_node
                
    # 3. Ensure Single Root (Torque Requirement)
    # TRIBES REQUIREMENT: 'bounds' MUST be Node 0 and the root (Parent: -1).
    # All other nodes (including 'always') should be descendants of 'bounds'.
    
    # Find the 'bounds' node index
    bounds_idx = -1
    for i, obj in enumerate(objects):
        if obj.name.lower() == 'bounds':
            bounds_idx = i
            break
            
    if bounds_idx != -1:
        # 1. Force bounds to be a root
        nodes[bounds_idx]['parent'] = -1
        
        # 2. Parent all other current roots to 'bounds'
        for i in range(len(nodes)):
            if i != bounds_idx and nodes[i]['parent'] == -1:
                # CYCLE CHECK: Ensure we don't parent a node to its own descendant
                is_descendant = False
                curr = nodes[bounds_idx]['parent']
                while curr != -1:
                    if curr == i:
                        is_descendant = True
                        break
                    curr = nodes[curr]['parent']
                
                if not is_descendant:
                    nodes[i]['parent'] = bounds_idx
                else:
                    print(f"DEBUG: Skipping parenting {objects[i].name} to {objects[bounds_idx].name} to avoid cycle.")
    else:
        # Fallback if no bounds: ensure single root
        roots = [i for i, n in enumerate(nodes) if n['parent'] == -1]
        if len(roots) > 1:
            # Pick 'always' as root if it exists
            master_root = -1
            for r_idx in roots:
                if objects[r_idx].name.lower() == 'always':
                    master_root = r_idx
                    break
            if master_root == -1:
                master_root = roots[0]
                
            for r_idx in roots:
                if r_idx != master_root:
                    nodes[r_idx]['parent'] = master_root

    # 4. CRITICAL: Ensure Node 0 is the root (Parent: -1)
    # Re-calculate link structure after parent changes
    for n in nodes:
        n['child'] = -1
        n['sibling'] = -1
        
    for i in range(len(nodes)):
        child_indices = [j for j, n in enumerate(nodes) if n['parent'] == i]
        if child_indices:
            child_indices.sort(key=lambda j: objects[j].name)
            nodes[i]['child'] = child_indices[0]
            for k in range(len(child_indices) - 1):
                nodes[child_indices[k]]['sibling'] = child_indices[k+1]

    # Find the current root node index
    root_indices = [i for i, n in enumerate(nodes) if n['parent'] == -1]
    if root_indices and root_indices[0] != 0:
        root_idx = root_indices[0]
        # Swap Node 0 and the root node
        nodes[0], nodes[root_idx] = nodes[root_idx], nodes[0]
        # Keep objects[i] <-> nodes[i] in sync: everything downstream (vertex
        # node-space transforms, LOD detail detection, default transforms)
        # indexes the objects list by node index. objects is swapped in place
        # so the caller's list reflects the change too.
        objects[0], objects[root_idx] = objects[root_idx], objects[0]
        # Update all parent/child/sibling references
        for n in nodes:
            if n['parent'] == 0: n['parent'] = root_idx
            elif n['parent'] == root_idx: n['parent'] = 0
            
            if n['child'] == 0: n['child'] = root_idx
            elif n['child'] == root_idx: n['child'] = 0
            
            if n['sibling'] == 0: n['sibling'] = root_idx
            elif n['sibling'] == root_idx: n['sibling'] = 0
            
        # Update obj_to_node_idx
        for obj, idx in list(obj_to_node_idx.items()):
            if idx == 0: obj_to_node_idx[obj] = root_idx
            elif idx == root_idx: obj_to_node_idx[obj] = 0
        print(f"DEBUG: Swapped Node 0 and Node {root_idx} to ensure root is at index 0.")
                
    return nodes, obj_to_node_idx


class DTSWriter:
    """Writes Blender data to DTS binary format."""
    
    def __init__(self):
        self.shape_version = SHAPE_VERSION
        self.mesh_version = MESH_VERSION
        self.material_version = MATERIAL_VERSION
        self.names = []
        self.name_to_index = {}
        
    def add_name(self, name):
        """Add a name to the name table, returning its index."""
        if name in self.name_to_index:
            return self.name_to_index[name]
        idx = len(self.names)
        # Pad name to 24 chars (DTS requirement)
        # Truncate to 23 chars to leave room for null terminator (engine MaxNameSize=24 includes null)
        padded = name[:23].ljust(24, '\x00')
        self.names.append(padded)
        self.name_to_index[name] = idx
        return idx
    
    def write_u8(self, stream, value):
        stream.write(struct.pack('<B', value))
        
    def write_u16(self, stream, value):
        stream.write(struct.pack('<H', value))
        
    def write_s16(self, stream, value):
        stream.write(struct.pack('<h', value))
        
    def write_u32(self, stream, value):
        stream.write(struct.pack('<I', value))
        
    def write_s32(self, stream, value):
        stream.write(struct.pack('<i', value))
        
    def write_f32(self, stream, value):
        stream.write(struct.pack('<f', value))
        
    def write_point3f(self, stream, x, y, z):
        self.write_f32(stream, x)
        self.write_f32(stream, y)
        self.write_f32(stream, z)
        
    def write_point2f(self, stream, x, y):
        self.write_f32(stream, x)
        self.write_f32(stream, y)
        
    def write_quat16(self, stream, x, y, z, w):
        """Write quaternion as 4 signed 16-bit integers."""
        def float_to_s16(f):
            # round() instead of int() truncation: halves the quantization error
            return max(-32767, min(32767, int(round(f * 32767))))
        self.write_s16(stream, float_to_s16(x))
        self.write_s16(stream, float_to_s16(y))
        self.write_s16(stream, float_to_s16(z))
        self.write_s16(stream, float_to_s16(w))
        
    def write_box3f(self, stream, min_pt, max_pt):
        self.write_point3f(stream, min_pt[0], min_pt[1], min_pt[2])
        self.write_point3f(stream, max_pt[0], max_pt[1], max_pt[2])
        
    def write_pers_header(self, stream, classname, data_size, version):
        """Write PERS chunk header.
        
        Args:
            stream: Output stream
            classname: Class name WITHOUT null terminator (e.g., 'TS::Shape')
            data_size: Size of the data that follows the header
            version: Version number for this class type
        """
        # Magic number
        self.write_u32(stream, PERS_MAGIC)
        
        # Classname length is WITHOUT null terminator
        classname_bytes = classname.encode('ascii')
        name_len = len(classname_bytes)
        
        # Padded length: round up to even number
        # Formula matches Kaitai parser: (classname_len + 1) & ~1
        # e.g., len=9 -> 10 bytes, len=16 -> 16 bytes
        padded_len = (name_len + 1) & (~1)
        
        # Size = 2 (classname_len) + padded_len + 4 (version) + data_size
        total_size = 2 + padded_len + 4 + data_size
        self.write_u32(stream, total_size)
        
        # Class name length (without null)
        self.write_u16(stream, name_len)
        
        # Class name + padding to even length (may or may not include null)
        stream.write(classname_bytes)
        if padded_len > name_len:
            stream.write(b'\x00' * (padded_len - name_len))
        # Version
        self.write_u32(stream, version)
        
    def compute_mesh_bounds(self, mesh):
        """Compute bounding box for a mesh."""
        if not mesh.vertices:
            return (0, 0, 0), (0, 0, 0)
        
        min_x = min_y = min_z = float('inf')
        max_x = max_y = max_z = float('-inf')
        
        for v in mesh.vertices:
            min_x = min(min_x, v.co.x)
            min_y = min(min_y, v.co.y)
            min_z = min(min_z, v.co.z)
            max_x = max(max_x, v.co.x)
            max_y = max(max_y, v.co.y)
            max_z = max(max_z, v.co.z)
            
        return (min_x, min_y, min_z), (max_x, max_y, max_z)
    
    def pack_vertices(self, mesh, scale, origin):
        """Convert mesh vertices to DTS packed format (0-255 range)."""
        packed = []
        for v in mesh.vertices:
            # Normalize to 0-255 range
            x = int((v.co.x - origin[0]) / scale[0] * 255) if scale[0] != 0 else 128
            y = int((v.co.y - origin[1]) / scale[1] * 255) if scale[1] != 0 else 128
            z = int((v.co.z - origin[2]) / scale[2] * 255) if scale[2] != 0 else 128
            
            # Clamp to valid range
            x = max(0, min(255, x))
            y = max(0, min(255, y))
            z = max(0, min(255, z))
            
            # Find closest normal
            normal_idx = find_closest_normal(v.normal)
            
            packed.append((x, y, z, normal_idx))
        return packed
    
    def write_ts_shape(self, stream, shape_data):
        """Write TS::Shape section."""
        # Header counts
        self.write_u32(stream, shape_data['num_nodes'])
        self.write_u32(stream, shape_data['num_sequences'])
        self.write_u32(stream, shape_data['num_subsequences'])
        self.write_u32(stream, shape_data['num_keyframes'])
        self.write_u32(stream, shape_data['num_transforms'])
        self.write_u32(stream, len(self.names))
        self.write_u32(stream, shape_data['num_objects'])
        self.write_u32(stream, shape_data['num_details'])
        self.write_u32(stream, shape_data['num_meshes'])
        self.write_u32(stream, shape_data['num_transitions'])
        self.write_u32(stream, shape_data['num_frame_triggers'])
        
        # Radius and center
        self.write_f32(stream, shape_data['radius'])
        self.write_point3f(stream, *shape_data['center'])
        
        # Bounds (v8+)
        if self.shape_version >= 8:
            self.write_box3f(stream, shape_data['bounds_min'], shape_data['bounds_max'])
        
        # Nodes (v8 format)
        for node in shape_data['nodes']:
            self.write_u16(stream, node['name'])
            self.write_s16(stream, node['parent'])
            self.write_u16(stream, node['num_subsequences'])
            self.write_u16(stream, node['first_subsequence'])
            self.write_u16(stream, node['default_transform'])
            
        # Sequences
        for seq in shape_data['sequences']:
            self.write_u32(stream, seq['name'])
            self.write_u32(stream, seq['cyclic'])
            self.write_f32(stream, seq['duration'])
            self.write_u32(stream, seq['priority'])
            self.write_u32(stream, seq['first_frame_trigger'])
            self.write_u32(stream, seq['num_frame_triggers'])
            self.write_u32(stream, seq['num_ifl_subsequences'])
            self.write_u32(stream, seq['first_ifl_subsequence'])
            
        # Subsequences (v8 format)
        for subseq in shape_data['subsequences']:
            self.write_u16(stream, subseq['sequence_index'])
            self.write_u16(stream, subseq['num_keyframes'])
            self.write_u16(stream, subseq['first_keyframe'])
            
        # Keyframes (v8 format)
        for kf in shape_data['keyframes']:
            self.write_f32(stream, kf['position'])
            self.write_u16(stream, kf['key_value'])
            self.write_u16(stream, kf['mat_index'])
            
        # Transforms (v8 format)
        for xform in shape_data['transforms']:
            # NOTE: Rotation is stored as (x, y, z, w) tuple by get_transform_index.
            # The importer (main.py:804) negates W when placing nodes in Blender,
            # so we must also negate X,Y,Z to match the round-trip convention.
            # This was previously changed but did NOT fix the furball issue—
            # the real problem is in vertex-to-node transformation (see below).
            rot = xform['rotation']
            self.write_quat16(stream, -rot[0], -rot[1], -rot[2], rot[3])
            self.write_point3f(stream, *xform['translation'])
            
        # Names
        for name in self.names:
            stream.write(name.encode('ascii')[:24].ljust(24, b'\x00'))
            
        # Objects (v8 format)
        for obj in shape_data['objects']:
            self.write_s16(stream, obj['name'])
            self.write_s16(stream, obj['flags'])
            self.write_s32(stream, obj['mesh_index'])
            self.write_s16(stream, obj['node_index'])
            self.write_u16(stream, 0)  # dummy
            self.write_point3f(stream, *obj['offset'])
            self.write_s16(stream, obj['num_subsequences'])
            self.write_s16(stream, obj['first_subsequence'])
            
        # Details
        # Details
        for detail in shape_data['details']:
            self.write_u32(stream, detail['root_node_index']) # Name
            self.write_f32(stream, detail['size']) # Size
            
        # Transitions (v8 format)
        for trans in shape_data['transitions']:
            self.write_u32(stream, trans['start_sequence'])
            self.write_u32(stream, trans['end_sequence'])
            self.write_f32(stream, trans['start_position'])
            self.write_f32(stream, trans['end_position'])
            self.write_f32(stream, trans['duration'])
            self.write_quat16(stream, *trans['rotation'])
            self.write_point3f(stream, *trans['translation'])
            
        # Frame triggers
        for trigger in shape_data['frame_triggers']:
            self.write_f32(stream, trigger['position'])
            self.write_u32(stream, trigger['value'])
            
        # Default material (v5+)
        if self.shape_version >= 5:
            self.write_u32(stream, shape_data.get('default_material', 1))
            
        # Always-animate node (v6+): fAlwaysNode is the index of the node named
        # "always" (the engine animates it regardless of detail level), or -1 if
        # there is none. Previously hardcoded to 1, which only happens to be right
        # when "always" is node 1 (true for stock characters/weapons) and pointed
        # at the wrong node for shapes lacking an "always" node.
        if self.shape_version >= 6:
            always_idx = -1
            for i, node in enumerate(shape_data['nodes']):
                nidx = node['name']
                nm = self.names[nidx] if 0 <= nidx < len(self.names) else ''
                # names are null-padded fixed-width fields; strip padding before compare
                if nm.split('\x00')[0].strip().lower() == 'always':
                    always_idx = i
                    break
            self.write_s32(stream, always_idx)
            
    def write_ts_animmesh(self, stream, mesh_data):
        """Write TS::CelAnimMesh section."""
        self.write_u32(stream, mesh_data['num_vertices'])
        self.write_u32(stream, mesh_data['num_vertices_per_frame'])
        self.write_u32(stream, mesh_data['num_texture_vertices'])
        self.write_u32(stream, mesh_data['num_faces'])
        self.write_u32(stream, mesh_data['num_frames'])
        
        # v2+ has texture vertices per frame
        if self.mesh_version >= 2:
            self.write_u32(stream, mesh_data['num_texture_vertices_per_frame'])
        
        # v2 and below have scale/origin here, v3+ has them in frame data
        # We use v3, so skip scale_v2/origin_v2
            
        # Radius
        self.write_f32(stream, mesh_data['radius'])
        
        # Packed vertices
        for v in mesh_data['vertices']:
            self.write_u8(stream, v[0])  # x
            self.write_u8(stream, v[1])  # y
            self.write_u8(stream, v[2])  # z
            self.write_u8(stream, v[3])  # normal index
            
        # Texture vertices (UV coords)
        for uv in mesh_data['texture_vertices']:
            self.write_point2f(stream, uv[0], uv[1])
            
        # faces: 3 pairs of (VertexIndex, TextureIndex) + material = 28 bytes total
        # (matches vertex_index_pair in dts.ksy: vertex_index first, then texture_index)
        for face in mesh_data['faces']:
            for i in range(3):
                self.write_u32(stream, face['vertex_indices'][i])
                self.write_u32(stream, face['texture_indices'][i])
            self.write_u32(stream, face['material'])  # Material index at end
            
        # Frames (v3+ format)
        for frame in mesh_data['frames']:
            self.write_s32(stream, frame['first_vert'])
            self.write_point3f(stream, *frame['scale'])
            self.write_point3f(stream, *frame['origin'])
            
    def write_ts_material_list(self, stream, materials):
        """Write TS::MaterialList section."""
        self.write_u32(stream, 1)  # num_details
        self.write_u32(stream, len(materials))
        
        for mat in materials:
            self.write_s32(stream, mat['flags'])
            self.write_f32(stream, mat['alpha'])
            self.write_s32(stream, mat['index'])
            
            # RGB
            self.write_u8(stream, mat['rgb'][0])
            self.write_u8(stream, mat['rgb'][1])
            self.write_u8(stream, mat['rgb'][2])
            self.write_u8(stream, mat.get('rgb_flags', 0))
            
            # Map file (32-byte field for v2+). Truncate to 31 chars so the
            # field always contains a null terminator; a full 32-char name
            # would make the engine read past the field.
            map_file = mat.get('map_file', '')[:31].ljust(32, '\x00')
            stream.write(map_file.encode('ascii'))
            
                # v3+ fields
            if self.material_version >= 3:
                self.write_s32(stream, mat.get('type', 0))
                self.write_f32(stream, mat.get('elasticity', 0.0))
                self.write_f32(stream, mat.get('friction', 0.0))
                
            # v4+ fields
            if self.material_version >= 4:
                self.write_u32(stream, mat.get('use_default_props', 1))


def collect_objects_with_parents(context, use_selected=True):
    """Collect objects and recursively include their parents (if Mesh or Empty)."""
    if use_selected:
        initial_objects = context.selected_objects
    else:
        initial_objects = context.scene.objects
        
    collected = set()
    to_process = [obj for obj in initial_objects if obj.type in ('MESH', 'EMPTY')]
    
    while to_process:
        obj = to_process.pop()
        if obj in collected:
            continue
        
        # Add to collected
        collected.add(obj)
        
    # Add parent if exists and is valid type
        if obj.parent and obj.parent.type in ('MESH', 'EMPTY'):
            to_process.append(obj.parent)
            
    # Hidden DTS members can't be selected in the viewport (e.g. "hide muzzle"
    # flash meshes that a visibility track keeps hidden outside "fire"), so a
    # select-all export would silently drop them. Pull in hidden objects that
    # were imported as part of the model (dts_object_index) when their parent
    # made it into the selection.
    if use_selected:
        for obj in context.scene.objects:
            if obj in collected or obj.type not in ('MESH', 'EMPTY'):
                continue
            if 'dts_object_index' not in obj.keys():
                continue
            if not (obj.hide_viewport or obj.hide_render):
                continue
            if obj.parent is not None and obj.parent in collected:
                collected.add(obj)

    # Functional Node Auto-Collection:
    # Always include nodes that are likely required by the engine (muzzle, tag, eye, etc.)
    # even if not selected, as long as they are related to children of collected nodes
    # or exist in the scene root.
    functional_patterns = ('muzzle', 'tag', 'always', 'bounds', 'start', 'eye')
    
    for obj in context.scene.objects:
        if obj.type == 'EMPTY' and any(p in obj.name.lower() for p in functional_patterns):
            if obj not in collected:
                # Include it if its parent is already included, or if it's a world-root node
                if not obj.parent or obj.parent in collected:
                    collected.add(obj)
                    # Also include its cousins/siblings if they are also functional
                    for child in obj.children:
                        if child.type == 'EMPTY' and any(p in child.name.lower() for p in functional_patterns):
                            collected.add(child)
            
    # Sort by name for deterministic order (Blender's default is usually name based in UI)
    return sorted(list(collected), key=lambda x: x.name)


class ExportDTS(bpy.types.Operator, ExportHelper):
    """Export a mesh to Starsiege: Tribes DTS format"""
    bl_idname = "export_mesh.dts"
    bl_label = "Export Tribes DTS (Updated)"
    bl_description = "Export mesh to Starsiege: Tribes DTS format"
    bl_options = {'PRESET', 'UNDO'}
    
    filename_ext = ".dts"
    filter_glob: StringProperty(default="*.dts", options={'HIDDEN'})
    
    # ========== COMMON OPTIONS (for both round-trips and new models) ==========
    
    global_scale: FloatProperty(
        name="Global Scale",
        description="[BOTH] Scale all geometry. Use 1.0 for round-trips; adjust for new models if needed",
        default=1.0,
    )

    export_selected: BoolProperty(
        name="Selected Only",
        description="[BOTH] Export only selected objects. Applies to all workflows",
        default=True,
    )
    
    export_materials: BoolProperty(
        name="Export Materials",
        description="[BOTH] Include material/texture data. Usually enabled for all workflows",
        default=True,
    )
    
    apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="[BOTH] Bake modifiers (mirror, subdivision, etc) into mesh before export",
        default=True,
    )

    # ========== ROUND-TRIP OPTIONS (for editing imported DTS files) ==========

    flatten_hierarchy: BoolProperty(
        name="Flatten Hierarchy",
        description="[ROUND-TRIP] Export all nodes to world space. Use for animation fixes on imported models",
        default=False,
    )

    original_dts_path: StringProperty(
        name="Donor DTS Path",
        description="[ROUND-TRIP] Path to original DTS for skeleton sync. Preserves animations from source file",
        default="",
        subtype='FILE_PATH',
    )


    # ========== NEW MODEL OPTIONS (for models created from scratch in Blender) ==========

    convert_axes: BoolProperty(
        name="Convert Axes (Z→Y)",
        description="[NEW MODEL] Convert Blender Z-up to DTS Y-up coordinates. Enable for models created in Blender; OFF for round-trips",
        default=False,
    )

    convert_winding: BoolProperty(
        name="Convert Winding (CCW→CW)",
        description="[NEW MODEL] Convert Blender CCW faces to DTS CW. Enable for new models; OFF for round-trips",
        default=False,
    )

    use_high_lod_all: BoolProperty(
        name="Use High LOD for All",
        description="[BOTH] Copy LOD36 to LOD10/2 on export. Only edit LOD36 when using this! Eliminates distance-based detail reduction",
        default=False,
    )



    def get_transform_index(self, shape_data, rot, loc):
        """Deduplicate transforms to match efficiency of Axe.dts."""
        # Use rounding to avoid floating point jitter causing duplicates
        key = (
            round(rot.x, 6), round(rot.y, 6), round(rot.z, 6), round(rot.w, 6),
            round(loc.x, 6), round(loc.y, 6), round(loc.z, 6)
        )
        if hasattr(self, '_xfm_cache'):
            if key in self._xfm_cache:
                return self._xfm_cache[key]
        else:
            self._xfm_cache = {}

        idx = len(shape_data['transforms'])
        shape_data['transforms'].append({
            'rotation': (rot.x, rot.y, rot.z, rot.w),
            'translation': (loc.x, loc.y, loc.z),
        })
        self._xfm_cache[key] = idx
        return idx
    
    def _copy_lod36_to_lower(self, objects):
        """Copy mesh data from LOD36 objects to LOD10 and LOD2 counterparts.
        
        This eliminates distance-based detail reduction - all LODs use high detail.
        Returns the number of objects updated.
        """
        import bmesh
        
        # Map base names to LOD36 source objects
        lod36_suffixes = [' 36', '36']
        target_suffixes = [' 10', '10', ' 2', '2']
        
        sources = {}
        for obj in objects:
            if obj.type != 'MESH':
                continue
            name = obj.name
            for suffix in lod36_suffixes:
                if name.endswith(suffix):
                    base = name[:-len(suffix)].strip()
                    sources[base] = obj
                    break
        
        if not sources:
            return 0
        
        copied_count = 0
        for obj in objects:
            if obj.type != 'MESH':
                continue
            
            name = obj.name
            target_suffix = ""
            base_name = ""
            
            # Check if this is a target LOD (10 or 2)
            for suffix in target_suffixes:
                if name.endswith(suffix):
                    base_name = name[:-len(suffix)].strip()
                    target_suffix = suffix.strip()
                    break
            
            if base_name in sources and target_suffix:
                src_obj = sources[base_name]
                if src_obj == obj:
                    continue
                
                # Copy mesh data from LOD36 source
                new_data = src_obj.data.copy()
                old_data = obj.data
                obj.data = new_data
                
                # Clean up old data if not used elsewhere
                if old_data.users == 0:
                    bpy.data.meshes.remove(old_data)
                
                copied_count += 1
        
        return copied_count


    def execute(self, context):
        return self.export_dts(context, self.filepath)
    
    def export_dts(self, context, filepath):
        """Main export function."""
        writer = DTSWriter()
        depsgraph = context.evaluated_depsgraph_get()
        
        # Track if ANY geometry was modified - if so, skip Hybrid Export
        # to preserve our new scaled transforms instead of using originals.
        any_geometry_modified = False
        
        # PRE-REGISTER NAMES to match Axe.dts indices
        # 0 = activation, 1 = fire, 2 = reload
        writer.add_name("activation")
        writer.add_name("fire")
        writer.add_name("reload")

        # Get objects to export
        objects = collect_objects_with_parents(context, self.export_selected)
            
        if not objects:
            self.report({'ERROR'}, "No mesh objects to export")
            return {'CANCELLED'}

        # =========================================================================
        # LOD OPTIMIZATION: Copy LOD36 mesh data to LOD10/LOD2
        # =========================================================================
        if self.use_high_lod_all:
            lod_copied = self._copy_lod36_to_lower(objects)
            if lod_copied > 0:
                self.report({'INFO'}, f"Copied LOD36 mesh data to {lod_copied} lower LOD objects")


        # =========================================================================
        # SKELETAL SYNCHRONIZATION: Parse donor DTS to get forced node indices
        # =========================================================================
        donor_node_map = {}  # name -> donor_index
        donor_world_matrices = {}  # node_idx -> mathutils.Matrix (for coordinate space sync)
        use_donor_sync = False
        original_dts_path = self.original_dts_path if hasattr(self, 'original_dts_path') else None
        
        if original_dts_path and os.path.exists(original_dts_path):
            try:
                donor_dts = Dts.from_file(original_dts_path)
                donor_shape = donor_dts.shape.data.obj_data
                donor_names = [n.split(b'\x00')[0].decode('ascii', errors='ignore') for n in donor_shape.names]
                donor_nodes = getattr(donor_shape, 'nodes', []) or getattr(donor_shape, 'nodes_v7', [])
                donor_transforms = getattr(donor_shape, 'transforms', []) or getattr(donor_shape, 'transforms_v7', [])
                
                # Build name -> index map
                for i, node in enumerate(donor_nodes):
                    name = donor_names[node.name]
                    donor_node_map[name] = i
                
                # =====================================================================
                # COORDINATE SPACE SYNC: Extract transforms and calculate world matrices
                # =====================================================================
                donor_world_matrices = {}  # node_idx -> mathutils.Matrix
                donor_local_transforms = {} # node_idx -> (trans, rot)
                
                def short_to_float(val):
                    """Convert DTS short quaternion component to float."""
                    return val / 32767.0
                
                def get_local_matrix(node_idx):
                    """Get local transform matrix for a node."""
                    if node_idx >= len(donor_nodes):
                        return mathutils.Matrix.Identity(4)
                    
                    node = donor_nodes[node_idx]
                    xf_idx = node.default_transform
                    if xf_idx >= len(donor_transforms):
                        return mathutils.Matrix.Identity(4)
                    
                    xf = donor_transforms[xf_idx]
                    
                    # Extract translation
                    trans = mathutils.Vector((xf.translate.x, xf.translate.y, xf.translate.z))
                    
                    # Extract rotation (DTS uses x, y, z, w order as shorts)
                    qx = short_to_float(xf.rotate.x)
                    qy = short_to_float(xf.rotate.y)
                    qz = short_to_float(xf.rotate.z)
                    qw = short_to_float(xf.rotate.w)
                    # NOTE: The importer (main.py:804) applies Blender Quaternion as
                    # (-W, X, Y, Z) to match engine conventions. For round-trip,
                    # we don't negate here—that's done during export (see line ~564).
                    # Previously attempted to "fix" this, but the furball distortion
                    # is actually caused by vertex-to-node coordinate mismatch, not
                    # quaternion component ordering.
                    rot = mathutils.Quaternion((qw, qx, qy, qz))
                    
                    donor_local_transforms[node_idx] = (trans, rot)
                    return mathutils.Matrix.LocRotScale(trans, rot, (1.0, 1.0, 1.0))
                
                def calc_world_matrix(node_idx, cache):
                    """Recursively calculate world matrix by walking up parent hierarchy."""
                    if node_idx in cache:
                        return cache[node_idx]
                    
                    if node_idx < 0 or node_idx >= len(donor_nodes):
                        return mathutils.Matrix.Identity(4)
                    
                    node = donor_nodes[node_idx]
                    local_mat = get_local_matrix(node_idx)
                    
                    if node.parent < 0:
                        # Root node
                        cache[node_idx] = local_mat
                    else:
                        # Recurse to parent
                        parent_world = calc_world_matrix(node.parent, cache)
                        cache[node_idx] = parent_world @ local_mat
                    
                    return cache[node_idx]
                
                # Calculate world matrix for all nodes
                for i in range(len(donor_nodes)):
                    donor_world_matrices[i] = calc_world_matrix(i, donor_world_matrices)
                
                print(f"DEBUG: Calculated {len(donor_world_matrices)} world matrices from donor DTS.")
                print(f"DEBUG: Parsed {len(donor_node_map)} nodes from donor DTS for skeletal sync.")
                use_donor_sync = True
            except Exception as e:
                import traceback
                print(f"WARNING: Failed to parse donor DTS for skeletal sync: {e}")
                traceback.print_exc()
                use_donor_sync = False
        
        # =========================================================================
        # SKELETAL SYNCHRONIZATION: Rigid Anchoring (Dummy Node Injection)
        # =========================================================================
        dummy_objects = []
        if use_donor_sync and donor_node_map:
            print(f"DEBUG: Starting Rigid Skeletal Anchoring for {len(donor_node_map)} donor nodes.")
            # We want the first len(donor_nodes) objects to match the donor EXACTLY.
            new_synced_list = [None] * len(donor_node_map)
            existing_map = {obj.name: obj for obj in objects}
            
            # 1. Map existing objects to their donor slots (FUZZY MATCHING)
            # Create a clean_name -> original_name lookup
            clean_to_original = {}
            for obj_name in existing_map.keys():
                # Strip .001, .002 etc suffixes from Blender object names
                clean_name = obj_name.split('.')[0] if '.' in obj_name and obj_name.split('.')[-1].isdigit() else obj_name
                clean_to_original[clean_name.lower()] = obj_name
            
            for donor_name, donor_idx in donor_node_map.items():
                # Try exact match first
                if donor_name in existing_map:
                    new_synced_list[donor_idx] = existing_map[donor_name]
                    del existing_map[donor_name]
                else:
                    # Try fuzzy match (lowercase, no suffix)
                    donor_clean = donor_name.lower()
                    if donor_clean in clean_to_original:
                        orig_name = clean_to_original[donor_clean]
                        new_synced_list[donor_idx] = existing_map[orig_name]
                        del existing_map[orig_name]
                        print(f"DEBUG: Fuzzy matched '{orig_name}' -> '{donor_name}' at index {donor_idx}")
            
            # 2. Fill gaps with Dummy Empties to preserve indices
            for i, obj in enumerate(new_synced_list):
                if obj is None:
                    # Find the name for this index
                    name = next((n for n, idx in donor_node_map.items() if idx == i), f"node{i}")
                    dummy = bpy.data.objects.new(name, None)
                    # Note: We don't link it to the scene if we don't want it visible,
                    # but collect_objects_with_parents expects them to be in the graph.
                    context.collection.objects.link(dummy)
                    new_synced_list[i] = dummy
                    dummy_objects.append(dummy)
                    # print(f"DEBUG: Created dummy node for index {i}: '{name}'")
            
            # 3. Align Hierarchy (Crucial for process_node_hierarchy)
            for i, obj in enumerate(new_synced_list):
                donor_node = donor_nodes[i]
                if donor_node.parent >= 0:
                    parent_obj = new_synced_list[donor_node.parent]
                    # Force parentage to match donor
                    if obj.parent != parent_obj:
                        obj.parent = parent_obj
            
            # 3.5 Apply local transforms to dummies (SKELETAL TRANSFORM ALIGNMENT)
            # This ensures the Blender skeleton matches the donor skeleton exactly
            for i, obj in enumerate(new_synced_list):
                if obj in dummy_objects and i in donor_local_transforms:
                    trans, rot = donor_local_transforms[i]
                    obj.location = trans
                    obj.rotation_mode = 'QUATERNION'
                    obj.rotation_quaternion = rot
            
            # 4. Final objects list: Synced (1:1 with donor) + Extras
            objects = new_synced_list + list(existing_map.values())
            print(f"DEBUG: Rigid Anchoring COMPLETE. {len(dummy_objects)} dummies added. Total nodes: {len(new_synced_list)}")
        else:
            # ORIGINAL BEHAVIOR: Sort by dts_object_index or name
            print(f"DEBUG: No donor sync or missing map. Using default sort.")
            objects = sorted(objects, key=lambda x: (x.get("dts_object_index", 999999), x.name))
        
        # Find root 'bounds' and ensure it's first (but don't swap if using donor sync)
        root_obj = None
        for obj in objects:
            if obj.name.lower() == 'bounds':
                root_obj = obj
                break
        
        if not root_obj and objects:
            root_obj = objects[0]
            
        if root_obj and not use_donor_sync:
            # Only reorder if NOT using donor sync (donor sync already has correct order)
            children = [obj for obj in objects if obj != root_obj]
            objects = [root_obj] + children
            
        # Build node hierarchy (with donor sync active, Node 0 swapping is implicitly disabled)
        nodes_list, obj_to_node_idx = process_node_hierarchy(objects, writer, self.flatten_hierarchy)
        
        # Prepare mesh data
        mesh_list = []
        materials_set = {}
        
        # Calculate Shape-level bounds (must encompass the entire model)
        shape_min_all = [float('inf')] * 3
        shape_max_all = [float('-inf')] * 3
        all_mesh_verts = []
        
        # Collect ALL vertices from ALL meshes for an accurate global bounding box
        for obj in objects:
            if obj.type == 'MESH':
                matrix = obj.matrix_world
                for v in obj.data.vertices:
                    world_v = matrix @ v.co
                    v_scaled = [world_v[i] * self.global_scale for i in range(3)]
                    # Apply axis conversion if enabled
                    if self.convert_axes:
                        v_scaled = [v_scaled[0], v_scaled[2], -v_scaled[1]]
                    all_mesh_verts.append(v_scaled)
                    for i in range(3):
                        shape_min_all[i] = min(shape_min_all[i], v_scaled[i])
                        shape_max_all[i] = max(shape_max_all[i], v_scaled[i])


        # If no meshes found, use a default to avoid NANs
        if shape_min_all[0] == float('inf'):
            shape_min_all = [-1.0, -1.0, -1.0]
            shape_max_all = [1.0, 1.0, 1.0]
            center = (0.0, 0.0, 0.0)
            radius = 1.0
        else:
            # Center is geometric midpoint of the bounding box
            center = tuple((shape_min_all[i] + shape_max_all[i]) / 2.0 for i in range(3))
            
            # Radius is the distance to the farthest vertex from the center
            radius = 0.01
            for v in all_mesh_verts:
                dist = math.sqrt(sum((v[i] - center[i])**2 for i in range(3)))
                if dist > radius:
                    radius = dist
                    
        # If a 'bounds' object exists, honor its authored culling box -- but UNION it
        # with the geometry box rather than replacing it. The engine's fBounds must
        # always enclose the actual geometry (Shape::read stores it directly in v8 and
        # culls/clips against it); shipping a box smaller than the model -- e.g. a donor
        # 'bounds' mesh left over after geometry was swapped in -- makes parts vanish or
        # mis-cull. Union guarantees enclosure while preserving any intentional padding.
        for obj in objects:
            if obj.name.lower().startswith('bounds'):
                if obj.type == 'MESH' and len(obj.data.vertices) >= 2:
                    b_matrix = obj.matrix_world
                    b_min = [float('inf')] * 3
                    b_max = [float('-inf')] * 3
                    for v in obj.data.vertices:
                        world_v = b_matrix @ v.co
                        v_scaled = [world_v[i] * self.global_scale for i in range(3)]
                        # Apply axis conversion if enabled
                        if self.convert_axes:
                            v_scaled = [v_scaled[0], v_scaled[2], -v_scaled[1]]
                        for i in range(3):
                            b_min[i] = min(b_min[i], v_scaled[i])
                            b_max[i] = max(b_max[i], v_scaled[i])
                    shape_min_all = [min(shape_min_all[i], b_min[i]) for i in range(3)]
                    shape_max_all = [max(shape_max_all[i], b_max[i]) for i in range(3)]
                    center = tuple((shape_min_all[i] + shape_max_all[i]) / 2.0 for i in range(3))
                    # Radius covers the box corners (safe over-estimate => no false culling)
                    radius = 0.5 * math.sqrt(sum((shape_max_all[i] - shape_min_all[i])**2 for i in range(3)))
                break

        
        # Store for Hybrid Export patching
        self.shape_center = center
        self.shape_radius = radius
        self.min_global = shape_min_all
        self.max_global = shape_max_all
        
        for obj in objects:
            obj_eval = None # Initialize to avoid UnboundLocalError
            _muted_fcurves = []  # shape-key value fcurves muted during export

            is_bounds_empty = (obj.name.lower() == 'bounds' and obj.type == 'EMPTY')
            
            # Skip non-mesh objects (unless it's the special bounds empty)
            if obj.type != 'MESH' and not is_bounds_empty:
                continue
                
            # Store reference to original mesh data (for shape keys)
            original_mesh_data = obj.data if obj.type == 'MESH' else None
                
            mesh = None
            if is_bounds_empty:
                # Generate minimal 2-vertex bounds mesh that defines the shape's physical limits
                mesh = bpy.data.meshes.new("Bounds_Dummy")
                
                # Coords relative to the 'bounds' object location
                b_loc = [l * self.global_scale for l in obj.location]
                rel_min = [shape_min_all[i] - b_loc[i] for i in range(3)]
                rel_max = [shape_max_all[i] - b_loc[i] for i in range(3)]
                
                mesh.from_pydata([rel_min, rel_max], [], []) # 2 Verts defining the diagonal
                print(f"DEBUG: Generated bounds mesh matching header: {rel_min} to {rel_max}")
            else:
                # Vertex-morph (frame-track) meshes: the per-frame geometry lives in
                # the shape keys, which we read separately. Zero all key values first
                # so the EVALUATED basis mesh is the rest pose, not basis + the sum of
                # every relative key (which inflates/skews the bounds and packing).
                if obj.type == 'MESH' and obj.data.shape_keys:
                    for _skb in obj.data.shape_keys.key_blocks:
                        _skb.value = 0.0
                    # If the key VALUES are fcurve-driven (the importer animates
                    # them along the timeline for playback), the animation system
                    # would overwrite the zeroing on the next evaluation. Mute
                    # the curves during export; restored in the loop's cleanup.
                    _sk_anim = obj.data.shape_keys.animation_data
                    if _sk_anim and _sk_anim.action:
                        try:
                            for _fc in iter_anim_fcurves(_sk_anim):
                                if not _fc.mute:
                                    _fc.mute = True
                                    _muted_fcurves.append(_fc)
                        except Exception as _e:
                            print(f"WARNING: could not mute shape-key fcurves: {_e}")
                    # depsgraph must re-evaluate or evaluated_get() returns the stale
                    # (non-zero-valued) mesh and the bounds inflate.
                    depsgraph.update()
                # Get evaluated mesh (with modifiers applied)
                if self.apply_modifiers:
                    try:
                        obj_eval = obj.evaluated_get(depsgraph)
                        mesh = obj_eval.to_mesh()
                    except Exception as e:
                        print(f"DEBUG: Error creating evaluated mesh: {e}")
                        mesh = obj.data.copy()
                else:
                    mesh = obj.data.copy()
                
            # Triangulate
            bm = bmesh.new()
            bm.from_mesh(mesh)
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            bm.to_mesh(mesh)
            bm.free()
            
            # Determine target node Space
            node_idx = obj_to_node_idx[obj]
            node_obj = objects[node_idx]
            
            # CRITICAL FIX V18: When syncing to a donor skeleton, use the DONOR's bone
            # world matrices for vertex transformation, not Blender's native node matrices.
            # The mesh vertices need to be stored relative to the DONOR skeleton's bone
            # positions, not Blender's scene positions. This prevents the "spiky" distortion
            # when splicing geometry into a donor model.
            if use_donor_sync and node_idx in donor_world_matrices:
                # Use donor DTS skeleton matrix
                donor_node_world = donor_world_matrices[node_idx]
                # Extract location and rotation, strip scale
                d_loc, d_rot, d_scale = donor_node_world.decompose()
                node_world_unscaled = mathutils.Matrix.LocRotScale(d_loc, d_rot, (1.0, 1.0, 1.0))
                print(f"DEBUG V18: Using donor matrix for node {node_idx} ('{node_obj.name}')")
            else:
                # Original behavior: use Blender's native node matrices
                # (for standalone exports without donor sync)
                node_world = node_obj.matrix_world.copy()
                u_loc, u_rot, u_scale = node_world.decompose()
                node_world_unscaled = mathutils.Matrix.LocRotScale(u_loc, u_rot, (1.0, 1.0, 1.0))
            
            node_matrix_inv = node_world_unscaled.inverted()

            
            transformed_verts_dict = {} # v_idx -> (x,y,z)
            min_local = [float('inf')] * 3
            max_local = [float('-inf')] * 3
            
            for v_idx, v in enumerate(mesh.vertices):
                # 1. Get world-space vertex position (includes all scales/modifiers)
                world_v = obj.matrix_world @ v.co
                
                # 2. Convert to unscaled node space
                v_node = node_matrix_inv @ world_v
                
                # 3. Apply global scale (usually 1.0, but used for unit conversion)
                v_s = [v_node[i] * self.global_scale for i in range(3)]
                
                # 4. Apply axis conversion if enabled (Blender Z-up -> DTS Y-up)
                if self.convert_axes:
                    # Blender X -> DTS X, Blender Z -> DTS Y, Blender -Y -> DTS Z
                    v_s = [v_s[0], v_s[2], -v_s[1]]
                
                transformed_verts_dict[v_idx] = v_s
                for i in range(3):
                    min_local[i] = min(min_local[i], v_s[i])
                    max_local[i] = max(max_local[i], v_s[i])

            # Vertex-morph (shape-key) animation: expand the packing bounds to the
            # UNION over ALL frames. The 8-bit packed verts are normalized to this
            # box; if a morph frame moves a vertex outside the rest frame's box it
            # would clamp to 0/255 and flatten (the "thin sliver" bug). Same
            # node-space transform as the base verts above.
            if original_mesh_data and original_mesh_data.shape_keys:
                for _kb in original_mesh_data.shape_keys.key_blocks:
                    for _d in _kb.data:
                        _wv = obj.matrix_world @ _d.co
                        _vn = node_matrix_inv @ _wv
                        _vs = [_vn[i] * self.global_scale for i in range(3)]
                        if self.convert_axes:
                            _vs = [_vs[0], _vs[2], -_vs[1]]
                        for i in range(3):
                            min_local[i] = min(min_local[i], _vs[i])
                            max_local[i] = max(max_local[i], _vs[i])

            min_pt = tuple(min_local)
            max_pt = tuple(max_local)

            # Compute bounds size in local space
            bounds_size = (
                max(0.0001, max_pt[0] - min_pt[0]),
                max(0.0001, max_pt[1] - min_pt[1]),
                max(0.0001, max_pt[2] - min_pt[2])
            )
            
            # For packing, we need to normalize world coords to 0-255
            # packed = (world_coord - min_pt) / bounds_size * 255
            
            # NEW: Split vertices for sharp normals
            # DTS only supports one normal per vertex, so sharp edges must be split.
            # (calc_normals_split is obsolete in Blender 4.0+)
            
            # CULLING FIX: Darkstar engine requires the first two vertices of every mesh 
            # to serve as the culling Bounding Box. If these are missing, the engine 
            # culls parts based on arbitrary geometry points.
            # v0 = (0,0,0) Min, v1 = (255,255,255) Max
            dts_vertices = [(0, 0, 0, 0), (255, 255, 255, 0)]
            vert_lookup = {} # (v_idx, n_idx) -> dts_idx
            loop_to_dts_vert = {}
            
            for poly in mesh.polygons:
                for loop_idx in poly.loop_indices:
                    v_idx = mesh.loops[loop_idx].vertex_index
                    loop_normal = mesh.loops[loop_idx].normal
                    
                    # Apply axis conversion to normal if enabled
                    if self.convert_axes:
                        converted_normal = (loop_normal[0], loop_normal[2], -loop_normal[1])
                    else:
                        converted_normal = loop_normal
                    n_idx = find_closest_normal(converted_normal)

                    
                    key = (v_idx, n_idx)
                    if key not in vert_lookup:
                        # Index starts at 2 because of injected culling box
                        dts_idx = len(dts_vertices)
                        vert_lookup[key] = dts_idx
                        
                        local_x, local_y, local_z = transformed_verts_dict[v_idx]
                        
                        # Normalize to 0-255 using local bounds
                        x = int(round((local_x - min_pt[0]) / bounds_size[0] * 255))
                        y = int(round((local_y - min_pt[1]) / bounds_size[1] * 255))
                        z = int(round((local_z - min_pt[2]) / bounds_size[2] * 255))
                        x = max(0, min(255, x)); y = max(0, min(255, y)); z = max(0, min(255, z))
                        
                        dts_vertices.append((x, y, z, n_idx))
                    
                    loop_to_dts_vert[loop_idx] = vert_lookup[key]

            # NEW: Include orphan vertices (not referenced by faces)
            # These are critical for bounding box padding / radius calculation in some Torque models.
            # We use a default normal index of 0 for these.
            processed_v_indices = set(k[0] for k in vert_lookup.keys())
            for v_idx, v in enumerate(mesh.vertices):
                if v_idx not in processed_v_indices:
                    # This is an orphan vertex
                    local_x, local_y, local_z = transformed_verts_dict[v_idx]
                    
                    # Normalize to 0-255 using local bounds
                    x = int(round((local_x - min_pt[0]) / bounds_size[0] * 255))
                    y = int(round((local_y - min_pt[1]) / bounds_size[1] * 255))
                    z = int(round((local_z - min_pt[2]) / bounds_size[2] * 255))
                    x = max(0, min(255, x)); y = max(0, min(255, y)); z = max(0, min(255, z))
                    
                    # Add to dts_vertices
                    dts_vertices.append((x, y, z, 0)) # Default normal 0
                    # We don't update vert_lookup since faces won't reference it anyway
            
            # SPECIAL CASE: For bounds objects with vertices but no faces
            if not dts_vertices and len(mesh.vertices) > 0:
                for v_idx in range(len(mesh.vertices)):
                    local_x, local_y, local_z = transformed_verts_dict[v_idx]
                    x = int(round((local_x - min_pt[0]) / bounds_size[0] * 255))
                    y = int(round((local_y - min_pt[1]) / bounds_size[1] * 255))
                    z = int(round((local_z - min_pt[2]) / bounds_size[2] * 255))
                    x = max(0, min(255, x)); y = max(0, min(255, y)); z = max(0, min(255, z))
                    dts_vertices.append((x, y, z, 0)) # Default normal 0
            
            packed_verts = dts_vertices
            
            # Frame scale and origin: Use stored DTS values if available and geometry is NOT modified
            # This preserves correct bounds/culling for originals, but allows edits to scale up.
            geometry_modified = False
            
            # 1. Check for scale change (If current world scale is not 1.0, it's modified)
            world_scale_vec = obj.matrix_world.to_scale()
            if any(abs(s - 1.0) > 0.001 for s in world_scale_vec):
                geometry_modified = True
                any_geometry_modified = True
                print(f"DEBUG: Object '{obj.name}' modified scale: {world_scale_vec}")
            
            if not geometry_modified and "dts_frame_scale_x" in obj:
                # 2. Check vertex count change
                stored_verts = obj.get("dts_vertex_count", -1)
                if stored_verts != -1 and len(mesh.vertices) != stored_verts:
                    geometry_modified = True
                
                # 3. Check for specific location offset if applicable
                if any(abs(l) > 0.001 for l in obj.location):
                    geometry_modified = True
                
                # 4. Check scale/size change (Compare current scaled bounds with original)
                if not geometry_modified:
                    import_scale = obj.get("dts_import_scale", 1.0)
                    orig_size = (
                        obj["dts_frame_scale_x"] * 255.0 / import_scale,
                        obj["dts_frame_scale_y"] * 255.0 / import_scale,
                        obj["dts_frame_scale_z"] * 255.0 / import_scale
                    )
                    for k in range(3):
                        # If any dimension is significantly different from original (>5%), it's modified.
                        if abs(bounds_size[k] - orig_size[k]) > orig_size[k] * 0.05:
                            geometry_modified = True
                            break
            
            if "dts_frame_scale_x" in obj and not geometry_modified:
                # Use original DTS values stored by importer (Preserve perfect bounds)
                import_scale = obj.get("dts_import_scale", 1.0)
                frame_scale = (
                    obj["dts_frame_scale_x"] / import_scale,
                    obj["dts_frame_scale_y"] / import_scale,
                    obj["dts_frame_scale_z"] / import_scale
                )
                frame_origin = (
                    obj["dts_frame_origin_x"] / import_scale,
                    obj["dts_frame_origin_y"] / import_scale,
                    obj["dts_frame_origin_z"] / import_scale
                )
            else:
                # Recalculate from geometry (For new models or modified geometry)
                # Bounds already include object scale/rotation transformations.
                frame_scale = (bounds_size[0] / 255.0, bounds_size[1] / 255.0, bounds_size[2] / 255.0)
                frame_origin = min_pt
            
            # Get UVs and associate with loops
            uv_layer = mesh.uv_layers.active
            texture_verts = []
            loop_to_uv_idx = {}
            if uv_layer:
                uv_dict = {} # (u, v) -> uv_idx
                for loop in mesh.loops:
                    uv = uv_layer.data[loop.index].uv
                    uv_val = (uv.x, 1.0 - uv.y)
                    if uv_val not in uv_dict:
                        uv_idx = len(texture_verts)
                        uv_dict[uv_val] = uv_idx
                        texture_verts.append(uv_val)
                    loop_to_uv_idx[loop.index] = uv_dict[uv_val]
            else:
                texture_verts = [(0.0, 0.0)]
                for loop in mesh.loops:
                    loop_to_uv_idx[loop.index] = 0

            # =====================================================================
            # FAITHFUL ROUND-TRIP FAST PATH
            # ---------------------------------------------------------------------
            # The importer stores each mesh's vertices in their original DTS
            # node-local frame (v.co == packed*scale + origin) and saves the exact
            # original frame scale/origin as custom props. For an UNMODIFIED
            # imported object we must reproduce that exactly. The general path
            # above reprojects through node_inv @ matrix_world (which does NOT
            # reconstruct the original frame once origin_set leaves a residual
            # object transform), splits shared verts, and re-injects the culling
            # box -- all of which corrupt weapon/character round-trips.
            #
            # When faithful, bypass that: pack v.co directly against the stored
            # scale/origin, in original vertex order (keeping the culling box at
            # indices 0/1), one normal per vertex, no splitting, no box re-inject.
            # Only applies to plain round-trips (import_scale 1.0, no axis/winding
            # conversion, no donor sync, no shape keys, unmodified vertex count
            # and unit world scale); new/edited models keep the general path.
            # Multi-frame (vertex-morph) meshes can also round-trip faithfully
            # when the importer stored the per-frame layout: each shape key
            # is packed against ITS OWN original frame scale/origin and written
            # back at its original first_vert offset (frames may overlap).
            _sk = obj.data.shape_keys if obj.type == 'MESH' else None
            _stored_fv = list(obj.get("dts_frame_first_verts", []))
            if _sk is None:
                _morph_faithful_ok = len(_stored_fv) <= 1
            else:
                _morph_faithful_ok = (
                    len(_stored_fv) >= 2
                    and len(_sk.key_blocks) == len(_stored_fv)
                    and "dts_frame_scales" in obj
                    and "dts_frame_origins" in obj
                )
            faithful = (
                "dts_frame_scale_x" in obj
                and abs(obj.get("dts_import_scale", 1.0) - 1.0) < 1e-6
                and not self.convert_axes
                and not self.convert_winding
                and not use_donor_sync
                and _morph_faithful_ok
                and obj.get("dts_vertex_count", -1) == len(mesh.vertices)
                and all(abs(s - 1.0) <= 0.001 for s in obj.matrix_world.to_scale())
            )
            faithful_morph = None
            if faithful:
                frame_scale = (obj["dts_frame_scale_x"],
                               obj["dts_frame_scale_y"],
                               obj["dts_frame_scale_z"])
                frame_origin = (obj["dts_frame_origin_x"],
                                obj["dts_frame_origin_y"],
                                obj["dts_frame_origin_z"])
                geometry_modified = False
                packed_verts = []
                for v in mesh.vertices:
                    co = v.co
                    comps = []
                    for i in range(3):
                        sc = frame_scale[i]
                        comps.append(int(round((co[i] - frame_origin[i]) / sc)) if sc else 0)
                    x, y, z = (max(0, min(255, c)) for c in comps)
                    packed_verts.append((x, y, z, find_closest_normal(v.normal)))
                # Identity loop->vertex mapping (no splitting)
                loop_to_dts_vert = {li: mesh.loops[li].vertex_index
                                    for poly in mesh.polygons
                                    for li in poly.loop_indices}
                # Keep min_pt/bounds_size consistent for any downstream packing
                min_pt = frame_origin
                bounds_size = tuple(max(0.0001, s * 255.0) for s in frame_scale)

                # Faithful MULTI-FRAME reconstruction: rebuild the original
                # vertex array (original total length, frames at their original
                # first_vert offsets) with each frame packed against its own
                # stored scale/origin. Basis (frame 0) reuses packed_verts.
                # Morph frames reuse the basis vertex normals: shape keys carry
                # no normals, and re-lighting morphs with rest normals matches
                # what the general path does.
                if _sk is not None and len(_stored_fv) >= 2:
                    _fscales = list(obj["dts_frame_scales"])
                    _forigins = list(obj["dts_frame_origins"])
                    _nvpf = len(mesh.vertices)
                    _total_nv = int(obj.get("dts_total_vertices", 0))
                    if _total_nv < _nvpf:
                        _total_nv = max(int(fv) + _nvpf for fv in _stored_fv)
                    _all_verts = [(0, 0, 0, 0)] * _total_nv
                    _frames_list = []
                    _kbs = _sk.key_blocks
                    for _f, _fv in enumerate(_stored_fv):
                        _fv = int(_fv)
                        _fs = tuple(_fscales[_f * 3:_f * 3 + 3])
                        _fo = tuple(_forigins[_f * 3:_f * 3 + 3])
                        if _f == 0:
                            for _i in range(_nvpf):
                                _all_verts[_fv + _i] = packed_verts[_i]
                        else:
                            for _i in range(_nvpf):
                                _co = _kbs[_f].data[_i].co
                                _c = []
                                for _a in range(3):
                                    _sc_ = _fs[_a]
                                    _c.append(int(round((_co[_a] - _fo[_a]) / _sc_)) if _sc_ else 0)
                                _all_verts[_fv + _i] = (
                                    max(0, min(255, _c[0])),
                                    max(0, min(255, _c[1])),
                                    max(0, min(255, _c[2])),
                                    packed_verts[_i][3])
                        _frames_list.append({'first_vert': _fv, 'scale': _fs, 'origin': _fo})
                    faithful_morph = {'verts': _all_verts, 'frames': _frames_list}

            # Check if object has negative scale (determinant < 0)
            # Negative determinant means additional flip is needed
            is_flipped = obj.matrix_world.determinant() < 0
            
            # Collect materials FIRST and build the slot -> global-list remap.
            # Faces must reference the shape-wide material list, but
            # poly.material_index is the OBJECT's slot number. On round-trips
            # the importer gives every object the full material list in the
            # same order (slot == global), but a fresh multi-object model with
            # differing slots needs the remap or faces point at wrong materials.
            slot_to_global = []
            for i, mat_slot in enumerate(obj.material_slots):
                if mat_slot.material and mat_slot.material.name not in materials_set:
                    mat = mat_slot.material
                    mat_name = mat.name
                    mat_name_upper = mat_name.upper()
                    
                    # Base flags: Textured + Smooth shading (stock 0x403)
                    mat_flags = MAT_TYPE_TEXTURE | MAT_SHADING_SMOOTH
                    mat_alpha = 1.0

                    # Detect special material effects from name suffixes,
                    # mapped onto the engine's REAL flag fields:
                    # _GLOW/_SELFILLUM/_LIT -> ShadingNone (fullbright; how
                    # stock flames render -- there is no additive blending)
                    if ('_GLOW' in mat_name_upper or '_ADDITIVE' in mat_name_upper
                            or '_SELFILLUM' in mat_name_upper or '_LIT' in mat_name_upper):
                        mat_flags = (mat_flags & ~MAT_SHADING_MASK) | MAT_SHADING_NONE
                        print(f"  Material '{mat_name}': fullbright (ShadingNone) enabled")

                    # _TRANSLUCENT/_ALPHA -> alpha blend; _TRANSPARENT/_CUTOUT
                    # -> palette-index-0 cutout
                    if '_TRANSLUCENT' in mat_name_upper or '_ALPHA' in mat_name_upper:
                        mat_flags |= MAT_TEX_TRANSLUCENT
                        mat_alpha = 0.5  # Default 50% alpha
                        print(f"  Material '{mat_name}': Translucent enabled")
                    elif '_TRANSPARENT' in mat_name_upper or '_CUTOUT' in mat_name_upper:
                        mat_flags |= MAT_TEX_TRANSPARENT
                        print(f"  Material '{mat_name}': Transparent cutout enabled")
                    
                    materials_set[mat.name] = {
                        'flags': mat_flags,
                        'alpha': mat_alpha,
                        # fIndex is the palette index for RGB materials and 0 for
                        # TEXTURE materials (ts_material.cpp). It is NOT the material's
                        # list position (faces reference that separately). Using the
                        # slot number here gave non-zero indices for multi-texture
                        # shapes, which the engine mis-handled -> crash on equip.
                        'index': 0,
                        'rgb': (255, 255, 255),
                        'map_file': material_map_file(mat)[:31],
                        'type': 0,
                        'elasticity': 1.0,
                        'friction': 1.0,
                    }

                # Map this slot to its global material-list index (insertion
                # order of materials_set). Empty slots keep the raw slot index
                # (previous behavior; there is nothing sensible to point at).
                if mat_slot.material:
                    slot_to_global.append(list(materials_set.keys()).index(mat_slot.material.name))
                else:
                    slot_to_global.append(i)

            # Build face data
            # DTS uses CLOCKWISE (CW) winding, Blender uses CCW.
            # For NEW models: swap indices 1↔2 to convert CCW→CW (if convert_winding is ON).
            # For ROUND-TRIPS: leave as-is since importer already reads CW data.
            faces = []
            for poly in mesh.polygons:
                if len(poly.loop_indices) != 3:
                    continue

                v = [loop_to_dts_vert[li] for li in poly.loop_indices]
                t = [loop_to_uv_idx[li] for li in poly.loop_indices]

                # CCW→CW conversion: only apply if user enabled it for new models
                # Also account for negative determinant (flipped scale) which inverts winding
                should_swap = self.convert_winding and not is_flipped
                if should_swap:
                    v = [v[0], v[2], v[1]]
                    t = [t[0], t[2], t[1]]

                # Translate the object slot index to the shape-wide material index
                if poly.material_index < len(slot_to_global):
                    mat_global = slot_to_global[poly.material_index]
                else:
                    mat_global = poly.material_index

                face = {
                    'vertex_indices': v,
                    'texture_indices': t,
                    'material': mat_global
                }
                faces.append(face)

            # REFRESH: Axe.dts has faces in REVERSED order compared to Blender loops
            faces.reverse()

            # Compute radius: Use stored DTS value if available AND geometry not modified
            if "dts_mesh_radius" in obj and not geometry_modified:
                radius = obj["dts_mesh_radius"]
            else:
                # Recalculate for new models or modified geometry
                is_bounds_obj = (obj.name.lower() == 'bounds') or (mesh.name.lower().startswith('bounds'))
                if len(transformed_verts_dict) > 0:
                    # Geometric radius from center (0,0,0) covering all transformed vertices.
                    radius = max(math.sqrt(v[0]**2 + v[1]**2 + v[2]**2) for v in transformed_verts_dict.values())
                    if not is_bounds_obj:
                        # Add 5% safety margin on the radius to prevent close-range culling
                        radius *= 1.05
                else:
                    radius = 0.01
            # Get object rotation (Handle both Euler and Quaternion modes)
            if obj.rotation_mode == 'QUATERNION':
                obj_rot = obj.rotation_quaternion
            else:
                obj_rot = obj.rotation_euler.to_quaternion()
            
            # DTS format: x, y, z, -w (negated W, and order is xyzw not wxyz)
            dts_rotation = (obj_rot.x, obj_rot.y, obj_rot.z, -obj_rot.w)
            
            # Check for shape keys (frame track animation) - use original mesh, not triangulated copy
            shape_keys = []
            all_frames_verts = [packed_verts]  # Frame 0 = basis
            frames_data = [{
                'first_vert': 0,
                'scale': frame_scale,
                'origin': frame_origin
            }]
            
            # Check shape key status on ORIGINAL mesh data
            # (skipped when the faithful multi-frame path already rebuilt the
            # original frame layout above)
            has_shape_keys = original_mesh_data and original_mesh_data.shape_keys

            if faithful_morph is None and has_shape_keys and original_mesh_data.shape_keys.key_blocks:
                key_blocks = original_mesh_data.shape_keys.key_blocks
                # Skip the first one (Basis) since we already have it
                for key_idx, key_block in enumerate(key_blocks[1:], start=1):
                    shape_keys.append(key_block.name)
                    
                    # Initialize frame_verts with basis frame data (handles culling box AND orphans)
                    frame_verts = list(packed_verts)
                    
                    for (v_idx, n_idx), dts_idx in vert_lookup.items():
                        # Shape-key coords are object-local; run them through the
                        # SAME transform chain as the basis vertices (world ->
                        # node space -> global scale -> axis conversion). The
                        # bounds-union pass above already does this; packing raw
                        # local coords here mangled morph frames whenever the
                        # object had a non-identity transform or convert_axes on.
                        sk_world = obj.matrix_world @ key_block.data[v_idx].co
                        sk_node = node_matrix_inv @ sk_world
                        sk = [sk_node[i] * self.global_scale for i in range(3)]
                        if self.convert_axes:
                            sk = [sk[0], sk[2], -sk[1]]
                        local_x, local_y, local_z = sk

                        # Pack to 0-255 using the SAME local bounds as the basis frame
                        x = int(round((local_x - min_pt[0]) / bounds_size[0] * 255))
                        y = int(round((local_y - min_pt[1]) / bounds_size[1] * 255))
                        z = int(round((local_z - min_pt[2]) / bounds_size[2] * 255))
                        x = max(0, min(255, x)); y = max(0, min(255, y)); z = max(0, min(255, z))
                        
                        frame_verts[dts_idx] = (x, y, z, n_idx)
                    
                    all_frames_verts.append(frame_verts)
                    frames_data.append({
                        'first_vert': len(packed_verts) * key_idx,
                        'scale': frame_scale,
                        'origin': frame_origin
                    })
            
            # Flatten all frame vertices into one list
            all_verts_flat = []
            for frame_verts in all_frames_verts:
                all_verts_flat.extend(frame_verts)

            num_frames = len(all_frames_verts)

            # Faithful multi-frame path: use the reconstructed original layout
            # (frames at original first_vert offsets, per-frame scale/origin).
            if faithful_morph is not None:
                all_verts_flat = faithful_morph['verts']
                frames_data = faithful_morph['frames']
                num_frames = len(frames_data)
                shape_keys = [kb.name for kb in obj.data.shape_keys.key_blocks[1:]]
            
            mesh_data = {
                'name': obj.name,
                'num_vertices': len(all_verts_flat),
                'num_vertices_per_frame': len(packed_verts),
                'num_texture_vertices': len(texture_verts),
                'num_texture_vertices_per_frame': len(texture_verts),
                'num_faces': len(faces),
                'num_frames': num_frames,
                'radius': radius,
                'vertices': all_verts_flat,
                'texture_vertices': texture_verts,
                'faces': faces,
                'frames': frames_data,
                'bounds_min': min_pt,
                'bounds_max': max_pt,
                'rotation': dts_rotation,
                'shape_keys': shape_keys,  # Store for animation export
                'node_index': obj_to_node_idx[obj],
            }
            mesh_list.append(mesh_data)
            
            # Clean up
            if self.apply_modifiers and obj_eval:
                obj_eval.to_mesh_clear()
            elif mesh:
                # If we created a temp mesh (either from dummy or copy) and it wasn't cleared above
                bpy.data.meshes.remove(mesh)
            # Restore any shape-key value fcurves muted for basis evaluation
            for _fc in _muted_fcurves:
                _fc.mute = False
        
        # Add object names
        print("DEBUG: ENTERING NAME LOOP", flush=True)
        for mesh_data in mesh_list:
            mesh_data['name_index'] = writer.add_name(mesh_data['name'])
            print(f"DEBUG: Object {mesh_data['name']} assigned Name Index {mesh_data['name_index']} -> {writer.names[mesh_data['name_index']]}", flush=True)
            
        # DYNAMIC CALCULATION: Shape Header values must match Mesh 0 (bounds) exactly
        # Calculate Shape-level bounds (must encompass the entire model)
        # Update: We now add a global 10% safety margin to the calculated bounds-mesh limits
        center_raw = tuple((shape_min_all[i] + shape_max_all[i]) / 2.0 for i in range(3))
        size_raw = tuple(shape_max_all[i] - shape_min_all[i] for i in range(3))
        
        # Expand bounds by 10% outwards from center
        min_all = tuple(center_raw[i] - (size_raw[i] * 0.55) for i in range(3))
        max_all = tuple(center_raw[i] + (size_raw[i] * 0.55) for i in range(3))
        
        # Store for Hybrid Export patching (used if splicing originals)
        self.shape_center = center_raw
        self.shape_radius = radius # Calculated from raw before margin expansion? No, let's keep it.
        self.min_global = list(min_all)
        self.max_global = list(max_all)
        
        # Calculate center as geometric midpoint of the shape bounds
        center = center_raw
        
        # Calculate radius as half the bounding box diagonal (matches Axe.dts logic)
        # Add a 20% safety margin for giant models to prevent aggressive culling
        radius = 0.5 * math.sqrt(sum((max_all[i] - min_all[i])**2 for i in range(3)))
        radius *= 1.20 # 20% buffer
        if radius <= 0: radius = 1.0

        # Find the best visual root node (handle 128 or largest mesh)
        visual_node_idx = 0
        if mesh_list:
            # Look for 'handle'
            for m in mesh_list:
                if 'handle' in objects[m['node_index']].name.lower():
                    visual_node_idx = m['node_index']
                    break
            else:
                # Fallback to mesh with most vertices (not counting bounds)
                best_m = None
                max_v = -1
                for m in mesh_list:
                    if objects[m['node_index']].name.lower() != 'bounds':
                        if m['num_vertices'] > max_v:
                            max_v = m['num_vertices']
                            best_m = m
                if best_m:
                    visual_node_idx = best_m['node_index']
                else:
                    visual_node_idx = mesh_list[0]['node_index']

        # 0. Intelligent LOD Detection (Highest Common Ancestor)
        # Groups meshes into LOD detail levels.
        details = []
        size_groups = {} # size -> [meshes]

        # Group meshes by size suffix (detail32, mesh10, etc)
        for m_data in mesh_list:
            obj_name = m_data['name'].lower()
            size = 128.0
            match = re.search(r'[\s_](-?\d+)', obj_name)
            if match:
                size = float(match.group(1))
            elif 'collision' in obj_name:
                size = -1.0
            elif 'los' in obj_name:
                size = -2.0
                
            if size not in size_groups:
                size_groups[size] = []
            size_groups[size].append(m_data)

        # For each size group, find the best "Common Ancestor" node
        for size, meshes in size_groups.items():
            if not meshes: continue
            
            # Candidate 1: Check for an explicit container node (mesh36, detail36)
            container_node_idx = -1
            search_pattern = rf"[\s_]{int(size)}"
            for i, n in enumerate(nodes_list):
                if re.search(search_pattern, objects[i].name.lower()):
                    container_node_idx = i
                    break
            
            if container_node_idx != -1:
                root_idx = container_node_idx
            else:
                # Candidate 2: Trace up to find a node with the size in its name
                first_mesh_node = meshes[0]['node_index']
                curr = first_mesh_node
                root_idx = curr
                while curr != -1:
                    node_name = objects[curr].name.lower()
                    match = re.search(rf'[\s_]({int(size)})', node_name)
                    if match:
                        root_idx = curr
                    curr = nodes_list[curr]['parent']

            details.append({
                'root_node_index': root_idx,
                'size': size,
                'meshes': meshes
            })

        # Build shape data
        shape_data = {
            'num_nodes': len(nodes_list),
            'num_sequences': 0,
            'num_subsequences': 0,
            'num_keyframes': 0,
            'num_transforms': 0,
            'num_objects': len(mesh_list),
            'num_details': len(details),
            'num_meshes': len(mesh_list),
            'num_transitions': 0,
            'num_frame_triggers': 0,
            'radius': radius,
            'center': center,
            'bounds_min': tuple(min_all),
            'bounds_max': tuple(max_all),
            'nodes': nodes_list,
            'sequences': [],
            'subsequences': [],
            'keyframes': [],
            'transforms': [],
            'objects': [],
            'details': details,
            'transitions': [],
            'frame_triggers': [],
            'default_material': 1,
            'always_animate': 1,
        }
        
        # 0. Initialize nodes list with BIND POSE TRANSFORMS from donor
        # This is critical - without proper transforms, bones collapse to origin
        for i, obj in enumerate(objects):
            # BIND POSE TRANSFORM RECOVERY: Use donor transforms if available
            if use_donor_sync and i in donor_local_transforms:
                trans, rot = donor_local_transforms[i]
                # Add this bind pose to the transforms table and get its index
                transform_idx = self.get_transform_index(shape_data, rot, trans)
            else:
                # Fallback: Use Blender object's local transform
                loc = obj.location.copy()
                if obj.rotation_mode == 'QUATERNION':
                    rot = obj.rotation_quaternion.copy()
                else:
                    rot = obj.rotation_euler.to_quaternion()
                transform_idx = self.get_transform_index(shape_data, rot, loc)
            
            nodes_list[i].update({
                'default_transform': transform_idx,
                'num_subsequences': 0,
                'first_subsequence': 0,
            })
        
        # (A former pre-pass here frame-sampled every object's whole action into
        # an animation_data list that was never read -- removed: it only cost a
        # full depsgraph evaluation per frame per object.)
        scene = context.scene
        original_frame = scene.frame_current

        # Determine animation sequences from timeline markers
        markers = sorted(scene.timeline_markers, key=lambda m: m.frame)
        sequence_ranges = []
        
        if markers:
            for i in range(len(markers)):
                name = markers[i].name
                if name.lower().startswith("end of") or name.lower() == "end":
                    continue
                    
                start = markers[i].frame
                if i + 1 < len(markers):
                    end = markers[i+1].frame - 1
                else:
                    end = scene.frame_end
                
                if end >= start:
                    sequence_ranges.append((name, start, end))
        
        # Ensure 'activation' exists if markers are used (common Tribes requirement)
        has_activation = any(s[0].lower() == 'activation' for s in sequence_ranges)
        if markers and not has_activation:
            # Match original Axe.dts duration (0.433s)
            sequence_ranges.insert(0, ('activation', scene.frame_start, scene.frame_start + 1))

        # 1. Create all Sequence headers first
        for seq_idx, (seq_name, start_f, end_f) in enumerate(sequence_ranges):
            seq_name_idx = writer.add_name(seq_name)
            
            # Match hardcoded Tribes durations if possible
            # DTS uses 30 FPS (4800 ticks/sec, 160 ticks/frame)
            duration = (end_f - start_f + 1) / 30.0
            if seq_name.lower() == 'activation': duration = 0.433
            elif seq_name.lower() == 'fire': duration = 0.633
            elif seq_name.lower() == 'reload': duration = 0.500
            
            # Match original Axe.dts priorities
            priority = 0
            if seq_name.lower() in ('activation', 'reload'):
                priority = 4096
                
            shape_data['sequences'].append({
                'name': seq_name_idx,
                'cyclic': 0,
                'duration': max(0.01, duration),
                'priority': priority,
                'first_frame_trigger': 0xFFFFFFFF,
                'num_frame_triggers': 0,
                'num_ifl_subsequences': 0,
                'first_ifl_subsequence': 3220516234, # Match Axe.dts mysterious value
            })

        # 2. Process Object/Node Subsequences CONTIGUOUSLY
        # For each object (Mesh/Empty), we collect its subsequences for ALL sequences
        scene.frame_set(scene.frame_start) # Ensure BIND POSE (Frame 0/Start) for default transforms!
        
        for obj_idx, obj in enumerate(objects):
            # Check if this object is a mesh
            mesh_data = None
            if obj_idx < len(mesh_list):
                 mesh_data = mesh_list[obj_idx]
            
            node = shape_data['nodes'][obj_idx] if shape_data['nodes'] else None

            # --- POPULATE DEFAULT TRANSFORM FOR THIS NODE ---
            # Re-set the bind-pose frame EVERY iteration: collect_object_keyframes
            # below leaves the scene at the last frame of the last sequence, so
            # without this, every object after the first would have its default
            # transform sampled from an animated pose instead of the bind pose.
            scene.frame_set(scene.frame_start)
            # Use evaluated object to get current world scale (handles inherited scaling)
            obj_eval = obj.evaluated_get(depsgraph)
            
            # Calculate transform relative to parent
            if not self.flatten_hierarchy and obj.parent and obj.parent in obj_to_node_idx:
                mat = obj.matrix_local
                # Compute ACCUMULATED scale from root to this node's parent.
                accumulated_scale = get_accumulated_parent_scale(obj)
                print(f"DEBUG ACCUM: '{obj.name}' accumulated_scale={accumulated_scale} local_trans={mat.translation}")
                loc = mathutils.Vector((
                    mat.translation.x * accumulated_scale.x * self.global_scale,
                    mat.translation.y * accumulated_scale.y * self.global_scale,
                    mat.translation.z * accumulated_scale.z * self.global_scale
                ))
                print(f"DEBUG ACCUM: '{obj.name}' final_loc={loc}")
                rot = mat.to_quaternion()
            else:
                mat = obj.matrix_world
                loc = mat.translation * self.global_scale
                rot = mat.to_quaternion()
            
            # Add to transform table (each node's default is added here)
            idx = self.get_transform_index(shape_data, rot, loc)
            if node:
                node['default_transform'] = idx
            
            has_node_anim = False
            has_shape_anim = False
            
            # Record first subsequence index for this node/object
            current_sub_idx = len(shape_data['subsequences'])
            num_shape_subs = 0
            num_node_subs = 0
            
            # A. Process Shape Key Subsequences first
            if mesh_data and mesh_data.get('shape_keys') and mesh_data['num_frames'] > 1:
                mesh_data['first_subsequence'] = current_sub_idx
                for seq_idx, (seq_name, start_f, end_f) in enumerate(sequence_ranges):
                    first_k = len(shape_data['keyframes'])
                    num_k = 0
                    for f in range(start_f, end_f + 1):
                        # Map the morph frame RELATIVE to this sequence's start, so EACH
                        # sequence (activation/fire/reload) plays the morph from frame 0.
                        # (Absolute f only worked for a sequence starting at frame 0/1,
                        # so fire/reload after it got no morph.)
                        rel = f - start_f
                        if rel < mesh_data['num_frames']:
                            shape_data['keyframes'].append({
                                'position': float(num_k) / max(1.0, float(end_f - start_f)),
                                'key_value': rel,
                                'mat_index': FLAG_FRAME_TRACK,
                            })
                            num_k += 1
                    
                    if num_k > 0:
                        shape_data['subsequences'].append({
                            'sequence_index': seq_idx,
                            'num_keyframes': num_k,
                            'first_keyframe': first_k,
                        })
                        num_shape_subs += 1
                mesh_data['num_subsequences'] = num_shape_subs
                current_sub_idx += num_shape_subs

            # B. Process Node Transform Subsequences second
            # Record first subsequence index for this node/object
            current_sub_idx = len(shape_data['subsequences'])
            num_node_subs = 0
            if node:
                node['first_subsequence'] = current_sub_idx
                for seq_idx, (seq_name, start_f, end_f) in enumerate(sequence_ranges):
                    keyframes = collect_object_keyframes(obj, scene, context, self.flatten_hierarchy, (start_f, end_f), self.global_scale)
                    
                    # UNIVERSAL ANIMATION: Any node that moves is included.
                    # static nodes in Axe (most muzzles in activation) will return [] and be skipped.
                    if keyframes:
                        first_k = len(shape_data['keyframes'])
                        num_k = len(keyframes)
                        
                        for kf in keyframes:
                            transform_idx = self.get_transform_index(shape_data, kf['rot'], kf['loc'])
                            # If only 1 frame, position is 0
                            div = max(1.0, float(end_f - start_f))
                            rel_pos = float(kf['frame'] - start_f) / div
                            shape_data['keyframes'].append({
                                'position': min(1.0, rel_pos),
                                'key_value': transform_idx,
                                'mat_index': kf['mat_idx'],
                            })
                        
                        shape_data['subsequences'].append({
                            'sequence_index': seq_idx,
                            'num_keyframes': num_k,
                            'first_keyframe': first_k,
                        })
                        num_node_subs += 1
                node['num_subsequences'] = num_node_subs

        # Keyframe sampling moved the timeline around; put it back where the user had it
        scene.frame_set(original_frame)

        shape_data['num_sequences'] = len(shape_data['sequences'])
        shape_data['num_subsequences'] = len(shape_data['subsequences'])
        shape_data['num_keyframes'] = len(shape_data['keyframes'])
        shape_data['num_transforms'] = len(shape_data['transforms'])
        
        # Add object references
        for i, mesh_data in enumerate(mesh_list):
            # Check if this object has shape key animation
            # has_shape_key_anim = mesh_data.get('num_subsequences', 0) > 0
            
            shape_data['objects'].append({
                'name': mesh_data['name_index'],
                'flags': 0,
                'mesh_index': i,
                'node_index': mesh_data['node_index'],
                'offset': (0.0, 0.0, 0.0),
                'num_subsequences': mesh_data.get('num_subsequences', 0),
                'first_subsequence': mesh_data.get('first_subsequence', 0),
            })
        
        # Update transform count (should be populated from nodes + animation)
        shape_data['num_transforms'] = len(shape_data['transforms'])
        
        # Prepare the monolithic content
        import io
        monolithic_buffer = io.BytesIO()
        
        # 1. Write the base Shape fields (num_nodes, etc.)
        # Note: We don't write the PERS header for the shape here! 
        # The writer's write_ts_shape method should be split or handled.
        # Looking at export_dts.py, DTSWriter.write_ts_shape writes only the FIELDS.
        writer.write_ts_shape(monolithic_buffer, shape_data)
        
        # 2. Write the meshes as sub-PERS blocks
        for mesh_data in mesh_list:
            mesh_buffer = io.BytesIO()
            writer.write_ts_animmesh(mesh_buffer, mesh_data)
            mesh_bytes = mesh_buffer.getvalue()
            
            writer.write_pers_header(monolithic_buffer, 'TS::CelAnimMesh', len(mesh_bytes), writer.mesh_version)
            monolithic_buffer.write(mesh_bytes)
            
        # 3. Write materials flag and Materials sub-PERS block
        if self.export_materials and materials_set:
            writer.write_s32(monolithic_buffer, 1)  # has_materials = true
            
            mat_buffer = io.BytesIO()
            writer.write_ts_material_list(mat_buffer, list(materials_set.values()))
            mat_bytes = mat_buffer.getvalue()
            
            writer.write_pers_header(monolithic_buffer, 'TS::MaterialList', len(mat_bytes), writer.material_version)
            monolithic_buffer.write(mat_bytes)
        else:
            writer.write_s32(monolithic_buffer, 0)  # has_materials = false
            
        # GET FINAL DATA
        content_bytes = monolithic_buffer.getvalue()
        
        # Write the file with the MASTER PERS WRAPPER
        with open(filepath, 'wb') as f:
            # The master wrapper uses TS::Shape and its size covers EVERYTHING that follows
            writer.write_pers_header(f, 'TS::Shape', len(content_bytes), writer.shape_version)
            f.write(content_bytes)
        
        # ROUND-TRIP HEADER PRESERVATION:
        # If objects came from an imported DTS, splice the original header for compatibility
        original_dts_path = self.original_dts_path
        if not original_dts_path:
            for obj in objects:
                # Object-level prop first: the importer's LOD organizer moves
                # objects into LOD_* collections, away from the collection that
                # carries dts_source_file.
                if obj.get("dts_source_file"):
                    original_dts_path = obj["dts_source_file"]
                    break
                for coll in obj.users_collection:
                    if "dts_source_file" in coll:
                        original_dts_path = coll["dts_source_file"]
                        break
                if original_dts_path:
                    break
        
        print(f"DEBUG: Checking Hybrid Splicing. Path: {original_dts_path}, any_geometry_modified={any_geometry_modified}")
        if original_dts_path and os.path.exists(original_dts_path):
            try:
                # DYNAMIC SPLICING: Find PERS blocks in both files and replace only the meshes
                with open(original_dts_path, "rb") as f:
                    original_bytes = f.read()
                with open(filepath, "rb") as f:
                    exported_bytes = f.read()
                
                def find_pers_blocks(data):
                    blocks = []
                    p = 0
                    while True:
                        p = data.find(b'PERS', p)
                        if p == -1: break
                        sz = struct.unpack('<I', data[p+4:p+8])[0]
                        nl = struct.unpack('<H', data[p+8:p+10])[0]
                        # Strip any null terminators for comparison
                        nm = data[p+10:p+10+nl].split(b'\x00')[0].decode('ascii', errors='ignore')
                        blocks.append({'pos': p, 'size': sz + 8, 'name': nm})
                        p += 4
                    return blocks
                
                orig_blks = find_pers_blocks(original_bytes)
                exp_blks = find_pers_blocks(exported_bytes)
                
                orig_meshes = [b for b in orig_blks if b['name'] == 'TS::CelAnimMesh']
                exp_meshes = [b for b in exp_blks if b['name'] == 'TS::CelAnimMesh']
                
                # Check for geometry compatibility
                if len(orig_meshes) == len(exp_meshes):
                    # NEW: Name-based mapping to fix the "alphabetical sort" breaking bone links
                    try:
                        # 1. Parse original to get name -> mesh_index map
                        orig_dts_obj = Dts.from_file(original_dts_path)
                        orig_shape = orig_dts_obj.shape.data.obj_data
                        orig_names_list = [n.split(b'\x00')[0].decode('ascii', errors='ignore') for n in orig_shape.names]
                        orig_objs_list = getattr(orig_shape, 'objects', []) or getattr(orig_shape, 'objects_v7', [])
                        
                        # DUPLICATE NAMES ARE REAL in stock files (FlameThrower
                        # has several objects all named 'flame'; Blender renames
                        # them flame.001 etc. on import). A plain name->chunk
                        # dict collapses them, splicing ONE flame's geometry
                        # into every slot. Map by (name, occurrence) instead:
                        # occurrence = Nth appearance in object-table order,
                        # which matches Blender's .001/.002 suffix ordering.
                        def _base_name(n):
                            return re.sub(r'\.\d+$', '', n)

                        # mesh_index -> (name, occurrence) from the original
                        orig_idx_info = {}
                        _occ = {}
                        for o in orig_objs_list:
                            nm = orig_names_list[o.name]
                            k = _occ.get(nm, 0)
                            _occ[nm] = k + 1
                            orig_idx_info.setdefault(o.mesh_index, (nm, k))

                        # 2. Parse exported to get (base name, occurrence) -> chunk
                        exp_dts_obj = Dts(KaitaiStream(BytesIO(exported_bytes)))
                        exp_shape = exp_dts_obj.shape.data.obj_data
                        exp_names_list = [n.split(b'\x00')[0].decode('ascii', errors='ignore') for n in exp_shape.names]
                        exp_objs_list = getattr(exp_shape, 'objects', []) or getattr(exp_shape, 'objects_v7', [])

                        _exp_named = []
                        for o in exp_objs_list:
                            name = exp_names_list[o.name]
                            if o.mesh_index < len(exp_meshes):
                                blk = exp_meshes[o.mesh_index]
                                chunk = exported_bytes[blk['pos'] : blk['pos'] + blk['size']]
                                _exp_named.append((name, chunk))
                        # 'flame' < 'flame.001' < 'flame.002': suffix sort puts
                        # occurrences in import order
                        _exp_named.sort(key=lambda t: t[0])
                        exp_name_to_chunk = {}
                        for name, chunk in _exp_named:
                            exp_name_to_chunk.setdefault(_base_name(name), []).append(chunk)

                    except Exception as e:
                        print(f"WARNING: Name-based mapping failed, falling back to index-based: {e}")
                        exp_name_to_chunk = None
                    # Start with original up to first mesh (preserves header, sequences, animations)
                    header_end = orig_meshes[0]['pos']
                    hybrid = bytearray(original_bytes[:header_end])
                    



                    # 7. Add exported meshes (RE-ORDERED BY ORIGINAL NAME + OCCURRENCE)
                    if exp_name_to_chunk:
                        # Append meshes in the order the ORIGINAL header expects (by mesh_index)
                        # We must satisfy every mesh slot the original header points to.
                        for i in range(len(orig_meshes)):
                            name, occurrence = orig_idx_info.get(i, (None, 0))
                            # The 'bounds' mesh is synthesized by the exporter: the importer
                            # turns the bounds object into an empty, discarding its original
                            # 2-vertex culling box, so the exported chunk is wrong (inflated /
                            # whole-model extents). The bounds box is never edited in Blender,
                            # so reuse the ORIGINAL bounds mesh bytes for a faithful round-trip.
                            chunks = exp_name_to_chunk.get(name) if name else None
                            if name and name.lower() == 'bounds':
                                blk = orig_meshes[i]
                                hybrid += original_bytes[blk['pos'] : blk['pos'] + blk['size']]
                            elif chunks and occurrence < len(chunks):
                                hybrid += chunks[occurrence]
                            else:
                                # Fallback: if name not found, use index-based or original
                                print(f"WARNING: Mesh index {i} ('{name}' #{occurrence}) not found in export! Using original data.")
                                blk = orig_meshes[i]
                                hybrid += original_bytes[blk['pos'] : blk['pos'] + blk['size']]
                    else:
                        # Fallback: simple index-based order (fragile!)
                        for i in range(len(exp_meshes)):
                            mesh_chunk = bytearray(exported_bytes[exp_meshes[i]['pos'] : exp_meshes[i]['pos'] + exp_meshes[i]['size']])
                            hybrid += mesh_chunk
                    
                    # Revert to ORIGINAL tail (Triggers, Transitions, Materials)
                    last_m_end = orig_meshes[-1]['pos'] + orig_meshes[-1]['size']
                    hybrid += bytearray(original_bytes[last_m_end:])
                    
                    # Update master PERS size to reflect new geometry
                    new_size = len(hybrid) - 8
                    struct.pack_into('<I', hybrid, 4, new_size)
                    
                    with open(filepath, "wb") as f:
                        f.write(hybrid)
                    
                    self.report({'INFO'}, f"Hybrid Export: Spliced {len(exp_meshes)} meshes into original header")
                else:
                    self.report({'WARNING'}, f"Mesh count mismatch ({len(exp_meshes)} vs original {len(orig_meshes)}) - using generated header")
            except Exception as e:
                self.report({'WARNING'}, f"Could not preserve original structure: {e}")
        
        self.report({'INFO'}, f"Exported {len(mesh_list)} mesh(es) to {filepath}")
        return {'FINISHED'}
    
    def draw(self, context):
        layout = self.layout
        
        # ========== COMMON OPTIONS ==========
        box_common = layout.box()
        box_common.label(text="Common Options:", icon='SETTINGS')
        box_common.prop(self, "global_scale")
        box_common.prop(self, "export_selected")
        box_common.prop(self, "export_materials")
        box_common.prop(self, "apply_modifiers")
        
        # ========== ROUND-TRIP OPTIONS ==========
        layout.separator()
        box_rt = layout.box()
        box_rt.label(text="Round-Trip Options (Editing Imported DTS):", icon='FILE_REFRESH')
        box_rt.prop(self, "flatten_hierarchy")
        box_rt.prop(self, "original_dts_path", text="Donor DTS")
        
        # ========== NEW MODEL OPTIONS ==========
        layout.separator()
        box_new = layout.box()
        box_new.label(text="New Model Options (Created in Blender):", icon='MESH_CUBE')
        box_new.prop(self, "convert_axes")
        box_new.prop(self, "convert_winding")
        
        # ========== OPTIMIZATION OPTIONS ==========
        layout.separator()
        box_opt = layout.box()
        box_opt.label(text="Optimization:", icon='MOD_DECIM')
        box_opt.prop(self, "use_high_lod_all")






def menu_func_export(self, context):
    self.layout.operator(ExportDTS.bl_idname, text="Tribes DTS (.dts)")


def register():
    bpy.utils.register_class(ExportDTS)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.utils.unregister_class(ExportDTS)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)


if __name__ == "__main__":
    register()
