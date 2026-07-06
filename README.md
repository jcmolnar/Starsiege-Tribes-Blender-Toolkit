# Starsiege: Tribes DTS — Blender Import & Export Plugin

Import **and export** Starsiege: Tribes / Darkstar `.DTS` models in Blender, plus a
scripted pipeline for retargeting **Mixamo** animations onto Tribes characters and
injecting them back into game-ready DTS files.

This is a fork of the original import-only *TribesToBlender* addon, extended with a
DTS **exporter**, character animation tooling, and a round-trip regression test.

> Format support targets the Tribes shape format: `TS::Shape` v8, `TS::CelAnimMesh`
> v3, `TS::MaterialList` v4 (the same structures used by the retail game and the
> Kingdom of Kronos RPG mod).

---

## Installation

1. On GitHub: **Code → Download ZIP**.
2. In Blender: **Edit → Preferences → Add-ons → Install…** and select the ZIP.
3. Enable **"Tribes DTS Format"**.

Once enabled:

- **File → Import → Tribes DTS (.dts)**
- **File → Export → Tribes DTS (.dts)**

Developed against Blender 3.0+ (current work is on 5.0).

---

## Features

### Import
- Static, animated, collision, debris, and hulk meshes
- Levels of Detail (LODs)
- Node/sequence animations (imported as empties + timeline markers)
- **Vertex-morph (frame track) animation** — morph frames import as shape keys
  (decoded with each frame's own scale/origin) with their playback keyframed on
  the timeline; survives edit + re-export (Sensor Jammer, bows, monsters,
  vehicle flames)
- **Visibility tracks** — e.g. muzzle flashes: hidden by default (object flag
  `0x1`), keyed visible at the correct fraction of the `fire` sequence
  (per-keyframe `position` + visible bit `0x8000`), exactly as the engine
  plays them; morph keys use the same position-accurate placement
- **Fully automatic textures — no manual extraction needed**:
  - loose `.png`/`.bmp` next to the `.dts` used first
  - Dynamix **PBMP** bitmaps decoded natively
  - missing textures pulled straight out of the game's `.vol` archives,
    auto-located by walking up from the `.dts` to the Tribes install
- **Fully automatic palettes**: the **PL98 multi-palette** is read directly
  from `*World.vol` (lushWorld preferred; drop a `.ppl` next to the `.dts` to
  force another world's tint). Each texture selects its own table via its
  `PiDX` id, so hulls, engine flames, and translucents all get correct colors;
  pure magenta is keyed transparent
- **Translucent/additive materials** (muzzle flashes, flames) approximate the
  engine's additive blending: luminance-driven alpha + emission (view in
  Material Preview / Rendered shading)
- IFL sequences (animated materials), keyframed in sync with their sequence
- "Organize by LOD" collections use the shape's **actual detail sizes**
  (36/10/2 characters, 15/4/1 deployables, ...); bounds/collision meshes
  import as wireframe so they don't cover the model
- Re-import into the same scene works (stale-name collisions and the global
  frame counter are handled); the playback range extends to cover all sequences
- Armors with bones, and vehicles

### Export
- Write meshes and node/sequence animation back out to `.DTS`
- **Character round-trips** (import → edit → export) are the validated path
- Material list and LOD structure preserved
- **Multi-texture models** — multiple single-material meshes with correct material
  `fIndex` (a texture material's index is 0; the per-mesh split avoids the
  mixed-material-per-mesh crash on equip)
- **Vertex-morph (shape-key) animation** — bake any source animation into per-frame
  shape keys and export a `CelAnimMesh` frame-track; used to give weapons their own
  swing/attack animation
- Hidden DTS members (e.g. default-hidden muzzle-flash meshes) are pulled back
  into "Selected Only" exports automatically — a select-all can't grab hidden
  objects, which used to silently drop them from round-trips

### Animation pipeline (`tools/`)
A headless workflow for getting new character animations into the game:

- **`retarget_mixamo.py`** — retarget a Mixamo FBX onto a Tribes character
  skeleton and emit a per-node keyframe sidecar (`.json`). World-orientation copy
  for body/legs/spine, direction-aim for the arms.
- **`patch_dts_animation.py`** — inject a retargeted animation into a DTS by
  **replacing a named sequence** (the engine plays animations by sequence name,
  e.g. `run`, `root`). Operates directly on the DTS binary, with cyclic and
  duration options (`keep` preserves the original timing to avoid foot-sliding on
  speed-synced locomotion).
- **`render_preview.py`** — quick Workbench renders of a DTS for visual QA.
- **`dts_viewer.py`** — visual verification WITHOUT the game: parses one or
  more `.dts` files directly (no Blender) and emits a self-contained HTML
  viewer (three.js) — models side by side (original vs round-trip), orbit
  camera, sequence playback (node tracks + vertex-morph frame tracks), LOD
  selection, wireframe/two-sided toggles.
  - **Game-accurate textures**: `--voldir <game>\base` pulls skins from `.vol`
    archives (recursive), decoding Darkstar PBMP bitmaps via the world `.ppl`
    multipalette; engine material flags render faithfully (translucent,
    cutout, fullbright flames, palette-index-0 transparency).
  - **Engine-accurate mounting**: `--equip weapon.dts` (player "dummy hand"),
    `--pilot player.dts` (vehicle "dummy pilot" + driverPose lean), generic
    `--attach`.
  - `python tools/dts_viewer.py flyer.dts --pilot player.dts --voldir "C:\game\base"`
- **`patch_node.py`** — move/rotate a node's default transform directly in the
  DTS binary (mount-point fixes that a Blender round-trip can't persist,
  because the hybrid splice preserves the original header).
- **`vol_tool.py`** (repo root) — standalone PVOL archive tool: `find`/`extract`
  any file from a `.vol`, plus `extract_pl98` to carve a world's PL98
  multi-palette out to a `.ppl`.

See [`tools/README.md`](tools/README.md) for exact command lines.

### Tests
- **`tests/roundtrip_regression.py`** — imports a DTS, exports it, and compares
  geometry/bounds/texverts/materials to catch round-trip regressions.

---

## Repository layout

| Path | Purpose |
|------|---------|
| `main.py`, `export_dts.py` | Import / export operators |
| `dts.py`, `dts.ksy`, `kaitaistruct.py` | DTS binary parser (Kaitai-based) |
| `tools/` | Mixamo retarget, animation patcher, render preview |
| `tests/` | Round-trip regression test |
| `docs/` | Format reference, character/animation guides, Mixamo workflow |

Useful docs:
[`docs/darkstar_dts_master_reference.md`](docs/darkstar_dts_master_reference.md),
[`docs/dts_character_creation_guide.md`](docs/dts_character_creation_guide.md),
[`docs/MIXAMO_WORKFLOW.md`](docs/MIXAMO_WORKFLOW.md),
[`docs/MIXAMO_BONE_MAPPING.md`](docs/MIXAMO_BONE_MAPPING.md).

---

## Status & known issues

- **Characters:** import, export, round-trip, and Mixamo animation injection work
  in-game (players and AI bots).
- **Weapons:** the proven authoring path is to **transplant** new geometry into a
  known-good donor weapon DTS (keeping its mount/animation/bounds/structure), which
  gives a correct in-hand mount for free. Pick the donor by class so the weapon
  inherits a fitting animation (broadsword/katana/elfinblade for swords, battleaxe
  for axes, mace/hammer for bludgeons, spear/trident for polearms, dagger/knife).
  Custom animation is added by baking a shape-key morph and injecting it as a
  frame-track. This pipeline shipped **46 weapons/shields/orbs** into the Kingdom of
  Kronos RPG.
- **Fresh (donor-less) weapon export works in-game**: a weapon exported with a
  fully generated header (no donor splice) equips and animates correctly
  (verified with a round-tripped Axe). The donor transplant remains the quickest
  path for adapting modern models, but it is no longer the only one.
- **Textures:** weapon/shield skins are 8-bit MS-BMP indexed to a world multipalette
  (`bfReserved2` = paletteIndex); the orb accessory shape needs native **PBMP**.
- Animated UVs are not supported.
- Re-importing into a used scene works, but timeline markers from earlier
  imports stick around (they belong to the scene) — one model per scene is
  still the cleanest workflow.
- Node transform keys currently get Blender's default (bezier) interpolation;
  morph and visibility keys are stepped like the engine.

### Notes on animation playback
- The Blender timeline is a **filmstrip of the model's sequences in file
  order**. The in-game order (e.g. chaingun: activation → spin → fire) is
  decided by the engine's weapon state machine in script (`item.cs`), not by
  the model — use the timeline markers to scrub individual sequences.
- Sequences that don't animate a node leave it wherever the previous state put
  it, matching the engine (e.g. the Sensor Jammer stays deployed during its
  `power` sequence).

## Wishlist
- Bone-based animation (auto-create bones, actions instead of markers)
- Animated UVs
- Support for `TED` and `DIL` files (a `DIS` exporter exists as a separate project)

## Credits
Fork of the original *TribesToBlender* import addon, extended with export and the
animation pipeline for the Kingdom of Kronos RPG project.
