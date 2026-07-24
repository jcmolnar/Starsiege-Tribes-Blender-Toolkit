# Handoff: Starsiege Herc → Tribes port

**Updated:** 2026-07-24
**Goal:** Field Starsiege mechs (Hercs) as playable, animated units in Tribes
(Kingdom of Kronos native client).
**Status:** **WORKING** — a Starsiege Talon walks around in Tribes, textured,
collides, aims, with a proper chase camera and working first person. One
cosmetic bug open (see-through top), plus feature work (jetpack, more mechs).

Repo: `Starsiege-Tribes-Blender-Toolkit` (working dir `C:\Users\Joe\Tribes DTS Blender`).
Engine: `C:\Users\Joe\Desktop\Tribes Native Build` (custom NATIVE-PORT build,
separate git repo, rebuildable). Game install: `C:\Dynamix\Tribes`.

---

## TL;DR — how to use what exists

The pipeline turns any Starsiege Herc into a Tribes armor with one command:

```
python tools/starsiege_to_tribes.py tr_talon --install "C:\Dynamix\Tribes\base"
```

Then in-game (listen/non-dedicated server on `base`):
1. Host, spawn in as a normal player.
2. Console (`~`): `beHercTrtalon();`  (back to normal: `beHercTrtalon_off();`)

`--list` shows all Herc-like shapes. `beHercTrtalon` is generated into the
datablock `.cs`; it resolves the SERVER player object and calls
`Player::setArmor`.

---

## What works (verified in-game)

- **Format**: Starsiege ships DTS **version 7**; Tribes' engine reads v7
  natively (`Shape::read`, ts_shape.cpp). No offline v7→v8 rewrite needed.
- **Walks / runs / strafes / crouches / falls** — all automatic. Tribes picks
  the animation from velocity and speed-scales one forward cycle [0.66,1.5]×.
- **Textures** — correct Starsiege colors, via truecolor PNG (the native
  client prefers a `.png` sibling and skips the palette remap that mangled BMP).
- **Collision** — sized to the model's measured extents (no more fall-through).
- **Chase camera** — scaled stock third-person cam (accurate aim + no clipping),
  tunable with `$pref::hercCamScale`.
- **First person** — looks out over the mech's front.
- **Datablock registration** — via a loose `base\armordata.cs` that execs the
  Herc `.cs` at server startup (console-time exec does NOT register).

---

## The pipeline (tools/starsiege_to_tribes.py)

Per Herc, in order:
1. **Extract** the `.dts` from `Starsiege\...\gameobjects.vol`.
2. **Textures** → truecolor `<name>.png` (+ 24-bit `.bmp` fallback), resolving
   PBMP indices through the Starsiege world `.ppl`.
3. **Rename sequences** in place to the names Tribes' `animData` expects
   (Seq00_walk→run, Seq02_stand→root, Seq04_squat→crouch root,
   Seq09_fall→fall, Seq10_land→landing, Seq07_cockpit→looks, …).
4. **Inject player nodes** (tools/inject_player_nodes.py): `dummyalways root`,
   `dummyalways chasecam` (z = 0.7·head), and per-detail-size
   `dummy hand<N>/unused<N>/midback<N>/lowback<N>/eye<N>`. **Name suffix is the
   detail SIZE (64/32/16/8/4), not the LOD ordinal** — `getNodeAtCurrentDetail`
   appends `int(fSize)`. Eye is offset forward/up out of the cockpit.
5. **Detach "looks"** — remove the aim sequence's tracks from all nodes so
   Tribes' priority-5000 viewThread doesn't clamp the whole body (see BUGS
   HISTORY). Rebuilds the subsequence array, repointing node AND object refs.
6. **Full LOD** — point every render detail's root at detail 0's root so all
   LODs draw the full mesh (`--keep-lods` to skip).
7. **Collision box + emit PlayerData `.cs`** with animData mapping, sized box,
   and the `beHerc*` console helpers.
8. **--install** — copy files to `base\` and hook the datablock into
   `armordata.cs` for startup registration.

Binary-DTS invariants the toolkit checks: layout arithmetic verified against
the parsed name table before writing; no subsequence `sequence_index` ≥
num_seq (that OOB crashes `UpdateSequenceSubscriberLists`); re-parse + render
after each transform.

---

## Engine changes (in Tribes Native Build, done by the engine agent)

All in `program/code/player.cpp` + `program/inc/player.h`:
- `Player::useMechCam()` — true when shape radius > 5 (Herc 9.2, trooper 2.2).
- `Player::getCameraTransform` — in the third-person branch, scales the
  pushback distance by `fRadius/2.3` (or `$pref::hercCamScale`). Uses the STOCK
  viewPitch rotation + `validateEyePoint` (accurate aim, no clip).
- `Player::forceThirdPerson()` → false (mechs can toggle first/third).
- Temporary `[HERCANIM]` logging (gated on `useMechCam()`) — REMOVE when done.

Specs written for the agent (in the engine tree): `HERC_CHASE_CAMERA_SPEC.md`
(v1, superseded), `HERC_ANIMATION_DIAGNOSTIC.md`, `HERC_CAMERA_FIX_2.md`
(current camera), `HERC_ANIM_FINDINGS.md` (agent's diagnosis).

---

## OPEN BUGS

### 1. Top of mech is see-through in third person (cosmetic, unsolved)
- First person is fine. Legs render; the upper body reads as see-through.
- **NOT LOD** — forcing full detail at every LOD (commit 862c056) did not fix
  it (and did no harm; keep it).
- **NOT near-plane** (1.0, camera ~16 units back), **NOT** camera-into-mesh
  (`validateEyePoint` excludes self).
- **Cannot be reproduced in the viewer** even single-sided from any angle
  (low/close/looking-up all render solid). So it's specific to the in-game
  render path for the player's OWN model in third person — the viewer doesn't
  replicate whatever it is.
- **Next steps (need in-game):** (a) log the actual camera eye position + view
  direction in `getCameraTransform` for the mech and confirm where it really
  is; (b) test `$pref::hercCamScale` much larger — does the top solidify when
  farther? (c) look at how the engine renders the controlled player's own shape
  in third person (any translucency / partial-hide / depth-sort path that
  treats "self" specially); (d) check far-plane / depth-buffer range at mech
  scale. Suspect it's a self-model render or depth/sort issue, not geometry.

---

## FUTURE IMPLEMENTATIONS

### A. Jetpack / flight (user requested)
- **Player jetpack (recommended, datablock-only):** set `jetForce`,
  `minJetEnergy`, `jetEnergyDrain` (currently 0) in the generated PlayerData —
  scale `jetForce` to the mech's mass. Gives the trooper jet on the jet key
  while KEEPING legs + everything else. Caveat: Herc has no jet anim (reuse
  `fall` or accept static). Easy; do in the toolkit's emit_playerdata.
- **True flight (FlierData):** real hover physics + the chocobo camera for
  free, but Fliers are RIGID → lose leg animation. Not recommended unless a
  hover-tank variant is wanted. Would be a separate emit path.

### B. Animations
- Only forward/back/strafe/crouch/fall/landing are real; a mech has no
  jump/throw/death/taunt/signal anims (mapped to idle/squat). This is inherent
  to the source model — nothing to "enable".
- Quick win: remap forward gait to `fastrun` (currently the Starsiege *walk*)
  for a runnier look — one line in SEQUENCE_RENAMES/animData.
- `fastrun`/`sprint`/turn/leg-hit sequences exist in the DTS but Tribes has no
  matching animData slots, so they're unused. Manual demo:
  `Player::setAnimation(%p, N)` (N = animData index 0-50).

### C. Batch all 32 Hercs
- The pipeline is per-shape and parameterized. Loop over the mech list
  (tr/kn/cy/mg/pl/… prefixes × talon/mino/apoc/basl/gorg/exec/judg/seek/shep/
  goad/eman/oly/recl/bolo/pred). Each needs its own datablock name + skin.
  Deploy all + one combined armordata hook.
- Faction skins share geometry (e.g. all Talons = tr_talon.dts with different
  skin bmp), so texture handling per-faction.

### D. Weapons / combat
- Mount points resolve (`dummy hand<N>` injected). Wiring Herc weapons or
  reusing Tribes weapons on the hardpoints is untouched. Starsiege weapon defs
  are in `datherc_*.cs` (`newHardPoint`/`newMountPoint`).

### E. Cleanup
- Remove the `[HERCANIM]` engine logging once the see-through bug is closed.
- Consider committing the finished mech assets somewhere (currently deploy-only
  to `C:\Dynamix\Tribes\base`).

---

## Key files & references

| What | Where |
|---|---|
| Port pipeline | `tools/starsiege_to_tribes.py` |
| Binary DTS surgery (nodes/looks/LOD) | `tools/inject_player_nodes.py` |
| DTS parser | `dts.py` / `dts.ksy` (v7 `transition.duration` fix = commit f5d2025) |
| Viewer (no LOD culling!) | `tools/dts_viewer.py` |
| Deployed Herc | `C:\Dynamix\Tribes\base\` (tr_talon.dts, *.png/.bmp, herctrtalon.cs, armordata.cs) |
| Engine | `C:\Users\Joe\Desktop\Tribes Native Build` (player.cpp, ts_shape*.cpp) |
| Engine specs | same tree, `HERC_*.md` |

**Commit trail** (this effort): f5d2025 read v7 → 4114a33 pipeline → 84a0c08
nodes → 440fb25/7e6aee3/56ad3be datablock+register → b3d33db collision/cam/skin
→ 99d9a19 PNG → 138ad6e/0a255c4 looks-detach(+crash fix) → 0415a68 eye →
862c056 full-LOD.

**Memory:** see `mech-to-tribes-animation`, `starsiege-mech-port` in the
project memory for the deep engine notes.
