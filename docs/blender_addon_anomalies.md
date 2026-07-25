# ⚠️ Tribes DTS Blender Addon Anomalies

This document lists discrepancies between the current Blender addon implementation (`Tribes DTS Blender`) and the verified technical specifications in the `darkstar_dts_master_reference.md`.

> [!NOTE]
> Anomalies marked ✅ are **confirmed valid** through code review against the master reference.
> Items marked ⚠️ require **additional context** — see notes.

---

## 1. Normal Encoding & Lookup Table ✅ CONFIRMED

- **Anomaly**: `export_dts.py` generates a normal table on the fly (lines 70-78) using a sphere distribution formula (`phi/theta` golden angle).
- **Reference**: The Darkstar engine uses a **static 256-entry lookup table** (`PackedVertex::fNormalTable`) defined in `ts_vertex.cpp`.
- **Impact**: Exported normals will be "off" compared to engine expectations, leading to lighting artifacts or incorrect normal quantization.
- **Encoding Algorithm**: The addon uses a **Dot Product** (max value) to find the best fit, while the engine uses **Euclidean Distance** (min value).

**Fix**: Replace `NORMAL_TABLE` in `export_dts.py` with the exact 256 `Point3F` values from `ts_vertex.cpp`.

---

## 2. Coordinate System & Axis Mapping ✅ CONFIRMED

- **Anomaly**: The scripts (`main.py`, `export_dts.py`) do not perform an explicit axis swap between Blender (Z-Up) and Darkstar (Y-Up).
- **Reference**: Mapping should be **Blender X -> DTS X**, **Blender Z -> DTS Y**, **Blender -Y -> DTS Z**.
- **Impact**: Models may appear rotated 90 degrees or oriented incorrectly in-game unless the user manually rotates the root node in Blender.

**Fix IMPLEMENTED**: The "Convert Axes [NEW MODEL]" option in the exporter now performs this conversion automatically for vertices, normals, and bounds. It is recommended for models created from scratch in Blender.

---

## 3. Vertex Winding Order ⚠️ PARTIALLY CORRECT

- **Observed Behavior**: `export_dts.py` does the following:
  1. Reverses vertex/texture indices **only if the object has a negative determinant** (flipped scale).
  2. Reverses the **face list order** globally (`faces.reverse()`).
- **Reference**: Darkstar uses **Clockwise (CW)** winding. Blender is **Counter-Clockwise (CCW)**.
- **Analysis**: 
  - The `faces.reverse()` call reverses the *order* of faces in the list, not the winding of each face.
  - The per-face `reverse()` call only triggers for negative-determinant objects.
  - **For normal (positive-determinant) objects, no winding swap occurs.**
- **Impact**: Backface culling may hide the front faces of models, or normals may be calculated "inward" by the engine.

~~**Fix**: For **all** faces, swap indices 1 and 2 before writing.~~
★**SUPERSEDED — that fix is wrong, and measurement says so.**★ A blanket per-face swap assumes
every model is uniformly mis-wound. Measured across `Entities.vol` — **1,944 meshes, 30,882 faces
of genuine Dynamix content — 5.3% are wound against their own vertex normals**, concentrated in
tiny flat cards where the surface is ambiguous or legitimately double-sided. Blanket-flipping
damages stock-like content.

**Fix as shipped** (`validate_face_winding()`, `export_dts.py`): test each face against the normals
its own vertices already carry — a correct face's geometric normal points OPPOSITE the mean of its
three stored normals — then **correct only a mesh that is MOSTLY backwards** (the mirrored-part
signature; a mirror inverts winding for that object alone). Scattered violations are reported and
left alone. On `tr_talon.dts` this fixes 131 faces across 4 meshes and touches **0** of the 1,944
stock meshes. Complements the conversion-side centroid vote in `tools/starsiege_to_tribes.py`
(commit `28fa645`); `tools/winding_diag.py` exists to expose where a majority vote goes wrong.

---

## 4. UV V-Coordinate Inversion ✅ ALREADY IMPLEMENTED

- **Status**: **NOT AN ANOMALY.** V-inversion is correctly implemented in both:
  - **Importer** (`main.py`, line 639): `array_val = [vert.x, 1 - vert.y]`
  - **Exporter** (`export_dts.py`, line 1312): `uv_val = (uv.x, 1.0 - uv.y)`

No fix needed.

---

## 5. Animation Timing & Frame Rate ✅ CONFIRMED

- **Anomaly**: `export_dts.py` uses **24 FPS** (line 1691) for duration calculations.
- **Reference**: Tribes 1 uses **30 FPS** (1 frame = 160 ticks, 4800 ticks per second).
- **Impact**: Animation durations in-game will be slightly out of sync with Blender's timeline if the user animates at 30 FPS but the script assumes 24 FPS.

**Fix**: Change the divisor from `24.0` to `30.0` (or make it a configurable option tied to Blender's scene FPS).

---

## 6. Name Length & Null Termination ✅ CONFIRMED

- **Anomaly**: `export_dts.py` (line 386) allows names up to 24 characters and pads them to exactly 24 bytes.
- **Reference**: The engine's `MaxNameSize` is 24, which **includes** the null terminator. Names should be capped at 23 characters.
- **Impact**: 24-character names may cause buffer overflows or read-past-end errors in the engine if it expects a null terminator within the first 24 bytes.

**Fix**: Truncate names at 23 characters before null-padding.

---

## 7. Material Versioning ⚠️ NEEDS VERIFICATION

- **Observation**: `export_dts.py` uses `MATERIAL_VERSION = 4`.
- **Reference**: Tribes 1 documentation and typical v8 DTS files use `version 3` for materials. Version 4 includes `use_default_props` which might be a later addition (Torque/Tribes 2).

**Needs Verification**: Check if Tribes 1 engine reads material version 4 without error. If not, downgrade to `3`.

---

## 8. Culling Box Vertex Injection ✅ CONFIRMED REQUIRED (not a workaround)

- **Observation**: `export_dts.py` injects two "culling box" vertices `(0,0,0,0)` and `(255,255,255,0)` at the start of every mesh.
- **VERDICT — required by the engine, do not remove.** The first two vertices of every frame block
  *are* that frame's bounding box: `CelAnimMesh` unpacks `fVerts[FirstVert]` and `[FirstVert+1]`
  into a `BoundingBox` for the visibility test (`ts_CelAnimMesh.cpp:129-141`), collision reads the
  same two (`:551-554`), and every real-geometry loop starts at `i=2` (`:692`).
- **Consequence for emitters**: geometry vertices start at index 2, and ★a face referencing index
  0 or 1 draws a spike to the bounding-box corner★. `validate_face_winding()` in `export_dts.py`
  now flags exactly that.

---

## Summary of Fixes to Implement

| Anomaly | Location | Fix Type |
| :--- | :--- | :--- |
| **Normal Table** | `export_dts.py` lines 70-78 | Replace generated table with static engine table |
| **Normal Algorithm** | `export_dts.py` line 86 | Change from dot product (max) to Euclidean distance (min) |
| **Axis Mapping** | `export_dts.py` | ✅ IMPLEMENTED as optional "Convert Axes" |
| **Hybrid Scaling Bug** | `export_dts.py` | ✅ FIXED (Removed problematic transform patching) |
| **Winding Order** | `export_dts.py` lines 1336-1345 | Swap vertex indices 1↔2 for **all** faces |
| **Animation FPS** | `export_dts.py` line 1691 | Change `24.0` to `30.0` |
| **Name Length** | `export_dts.py` line 386 | Truncate at 23 characters |

---
*Verified against `darkstar_dts_master_reference.md` and source code inspection.*
