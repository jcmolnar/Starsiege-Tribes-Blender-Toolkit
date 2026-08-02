"""build_herc_pack -- convert EVERY Starsiege Herc to a usable Tribes armor, in one pass.

`starsiege_to_tribes.py` ports one Herc. This drives it across all of them and then
deploys the result into a playable tree.

★Why this exists rather than a shell loop over --install.★  `starsiege_to_tribes.install()`
writes its hook between BEGIN/END markers and REPLACES that block on re-run, which is
correct for one armor and wrong for thirty-four: installing them in sequence leaves only
the LAST one hooked, and the other thirty-three sit on disk as dead files that look
installed. This writes ONE block containing every exec.

★The Repack is not laid out the way the single-mech tool assumes.★  That tool shadows a
vol entry with a loose `base/armordata.cs`. The 1.40 Repack keeps its scripts LOOSE at
`base/scripts/ArmorData.cs` (and uses .zip volumes), so the hook is appended there
instead -- no shadowing, stock content untouched, and the block is marked so it can be
removed cleanly.

Usage:
    python -B tools/build_herc_pack.py --game "C:/Users/Joe/Documents/Starsiege" \
        --stage build/hercs --deploy "C:/Dynamix/Tribes - Repack - 1.40 Assets"
    python -B tools/build_herc_pack.py --list
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

MARK_BEGIN = "// >>> starsiege herc pack: PlayerData armors >>>"
MARK_END = "// <<< starsiege herc pack: PlayerData armors <<<"


def listMechs(game):
    p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "starsiege_to_tribes.py"),
                        "--list", "--game", game], capture_output=True, text=True)
    out = []
    for line in (p.stdout or "").splitlines():
        s = line.strip()
        if s.endswith(".dts") or (".dts" in s and "nodes=" in s):
            name = s.split()[0]
            if name.endswith(".dts"):
                out.append(name[:-4])
    return sorted(set(out))


def convertOne(game, mech, stage):
    outdir = os.path.join(stage, mech)
    p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "starsiege_to_tribes.py"),
                        mech, "--game", game, "-o", outdir],
                       capture_output=True, text=True)
    return p.returncode, outdir, (p.stdout or "") + (p.stderr or "")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=r"C:/Users/Joe/Documents/Starsiege")
    ap.add_argument("--stage", default=os.path.join(REPO, "build", "hercs"))
    ap.add_argument("--deploy", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default=None, help="comma-separated mech ids")
    args = ap.parse_args(argv)

    mechs = listMechs(args.game)
    if args.only:
        want = set(x.strip() for x in args.only.split(","))
        mechs = [m for m in mechs if m in want]

    if args.list:
        for m in mechs:
            print(m)
        print("%d mech(s)" % len(mechs))
        return 0

    os.makedirs(args.stage, exist_ok=True)
    report = {"mechs": [], "failed": [], "excluded": []}

    print("=" * 74)
    print("Starsiege Herc pack: %d mech(s) -> %s" % (len(mechs), args.stage))
    print("=" * 74)

    for m in mechs:
        rc, outdir, log = convertOne(args.game, m, args.stage)
        if rc != 0:
            report["failed"].append({"mech": m, "exit": rc,
                                     "log": log.strip().splitlines()[-6:]})
            print("  %-14s FAILED (exit %d)" % (m, rc))
            for l in log.strip().splitlines()[-4:]:
                print("      %s" % l)
            continue

        # ★Not every "Herc-like" shape is a WALKING mech, and the difference decides
        # whether it can be a PlayerData at all.★
        #
        # A Tribes player picks its animation by projecting velocity onto each sequence's
        # measured root motion (Player::pickAnimation). A shape with no locomotion
        # sequences therefore animates NOTHING and slides with frozen legs -- which is
        # precisely the failure mode starsiege_to_tribes' own WHY_PLAYERDATA note gives
        # as the reason not to mount a Herc as a vehicle.
        #
        # cy_bolo (Bolo tank), ha_pred (Predator) and rb_bike (bike) are Starsiege
        # VEHICLES: 4-6 sequences, no frame triggers, no transitions, and none of the
        # locomotion names. Converting them "successfully" and shipping them as armors
        # would produce three armors that look right standing still and are broken the
        # moment you move.
        dtsFiles = [f for f in (os.listdir(outdir) if os.path.isdir(outdir) else [])
                    if f.lower().endswith(".dts")]
        if dtsFiles:
            try:
                import dts_contract
                c = dts_contract.load(os.path.join(outdir, dtsFiles[0]))
                names = set(s["name"] for s in c.sequences)
                missing = {"run", "fastrun", "root", "looks"} - names
                if missing:
                    report.setdefault("excluded", []).append({
                        "mech": m, "reason": "noLocomotionSequences",
                        "missing": sorted(missing),
                        "sequences": [s["name"] for s in c.sequences],
                        "detail": "a Starsiege VEHICLE, not a walking Herc -- "
                                  "Player::pickAnimation would find no root motion and "
                                  "it would slide with frozen legs"})
                    print("  %-14s EXCLUDED: no locomotion sequences (%d seq, %d trigger, "
                          "%d transition) -- a vehicle, not a walking Herc"
                          % (m, len(c.sequences), len(c.frameTriggers),
                             len(c.transitions)))
                    continue
            except Exception as e:
                report["failed"].append({"mech": m, "exit": 0,
                                         "log": ["contract read failed: %s" % e]})
                print("  %-14s FAILED: converted output unreadable: %s" % (m, e))
                continue

        produced = sorted(os.listdir(outdir)) if os.path.isdir(outdir) else []
        dts = [f for f in produced if f.lower().endswith(".dts")]
        cs = [f for f in produced if f.lower().endswith(".cs")]
        tex = [f for f in produced if f.lower().endswith((".bmp", ".png"))]
        # The datablock name is what the .cs declares; recover it from the filename the
        # single-mech tool chose (herc<mech>.cs -> PlayerData Herc<Mech>).
        db = os.path.splitext(cs[0])[0] if cs else None
        report["mechs"].append({"mech": m, "dir": outdir, "dts": dts, "cs": cs,
                                "textures": tex, "datablock": db})
        print("  %-14s ok   %d dts, %d cs, %d texture(s)" % (m, len(dts), len(cs), len(tex)))

    print("-" * 74)
    print("%d converted, %d excluded, %d failed"
          % (len(report["mechs"]), len(report.get("excluded", [])), len(report["failed"])))

    reportPath = os.path.join(args.stage, "herc_pack_report.json")
    with open(reportPath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    print("report -> %s" % reportPath)

    if not args.deploy:
        return 1 if report["failed"] else 0

    # ---- deploy ---------------------------------------------------------------
    base = os.path.join(args.deploy, "base")
    if not os.path.isdir(base):
        print("DEPLOY ERROR: %s is not a folder" % base)
        return 2

    scripts = os.path.join(base, "scripts")
    os.makedirs(scripts, exist_ok=True)

    # Shapes and textures go in base/ (a ResourceManager search-path root); the PlayerData
    # scripts go in base/scripts/ alongside MechArmorData.cs, which is also on the search
    # path (`base\scripts`, EvalSearchPath in console.cs) so exec-by-name still resolves.
    copied = 0
    for e in report["mechs"]:
        for f in e["dts"] + e["textures"]:
            shutil.copy2(os.path.join(e["dir"], f), os.path.join(base, f))
            copied += 1
        for f in e["cs"]:
            shutil.copy2(os.path.join(e["dir"], f), os.path.join(scripts, f))
            copied += 1
    print("\ndeployed %d file(s): shapes/textures -> base\\, scripts -> base\\scripts\\"
          % copied)

    # ---- MechArmorData.cs: every mech armor, in ONE file, exec'd OPT-IN ---------
    #
    # ★BACKWARDS COMPATIBILITY IS THE WHOLE POINT OF THIS BEING OPT-IN.★
    #
    # DataBlockManager::sendDataToClient (dataBlockManager.cpp:246) posts a
    # DataBlockEvent for EVERY registered datablock to EVERY connecting client --
    # there is no "only if used" filter. The client preloads each one, and a shape it
    # cannot resolve produces "Unable to model: X.dts. The server has new art data you
    # are missing." followed by Net::setLastError, which DISCONNECTS it
    # (dataBlockManager.cpp:436-468, shapeBase.cpp:146-151).
    #
    # So merely DECLARING these armors on a server locks out every client that does not
    # have all 31 shapes -- and old clients cannot be updated. Registering them by
    # default would therefore break stock clients on any server that installed this pack,
    # even one whose host never intended to use mechs.
    #
    # Hence: the declarations live in their own file, and stock ArmorData.cs gets ONE
    # guarded line that does nothing unless the host opts in.
    mechLines = [
        "// MechArmorData.cs -- Starsiege Herc armors for Tribes.",
        "// Generated by tools/build_herc_pack.py. Do not hand-edit; re-run the tool.",
        "//",
        "// ★Exec'd ONLY when the host sets $MechPack::Enable = 1.★  Every datablock a",
        "// server registers is sent to every client, and a client missing the matching",
        "// .dts is DISCONNECTED with \"The server has new art data you are missing\"",
        "// (dataBlockManager.cpp:436, shapeBase.cpp:146). Registering these",
        "// unconditionally would lock stock clients out of any server that merely has",
        "// the pack installed, so this stays opt-in and old clients keep working.",
        "//",
        "// To use the mechs on a server you host:",
        "//     $MechPack::Enable = 1;      (in config, BEFORE createServer)",
        "// then in game:  beHercKntalon();  etc.",
        "//",
        "// Every connecting client then needs this pack too.",
        "",
    ]
    for e in sorted(report["mechs"], key=lambda x: x["mech"]):
        if e["cs"]:
            mechLines.append('exec("%s");' % e["cs"][0])
    mechPath = os.path.join(scripts, "MechArmorData.cs")
    with open(mechPath, "w", encoding="utf-8", errors="replace") as f:
        f.write("\n".join(mechLines) + "\n")
    print("wrote %s (%d armor(s), opt-in)" % (mechPath, len(report["mechs"])))

    # ---- the one guarded line in stock ArmorData.cs -----------------------------
    #
    # Two console traps, both avoided deliberately:
    #   * a BRACELESS top-level `if` is a SILENT syntax error -- the line would simply
    #     never run and nothing would say so. Braces are mandatory here.
    #   * `$var == 0` MATCHES an unset variable, because "" promotes to 0. Testing
    #     `== 1` is the safe direction: unset -> 0, which is not 1, so the default is
    #     OFF and stays OFF.
    hook = "\n".join([
        MARK_BEGIN,
        "// Starsiege mech armors -- OPT-IN, default OFF (see MechArmorData.cs).",
        "// Leave $MechPack::Enable unset and this server behaves exactly as it did",
        "// before the pack was installed: no mech datablocks are registered, so stock",
        "// clients connect normally.",
        "if($MechPack::Enable == 1) { exec(\"MechArmorData.cs\"); }",
        MARK_END,
    ]) + "\n"

    target = os.path.join(scripts, "ArmorData.cs")
    if not os.path.exists(target):
        print("DEPLOY ERROR: %s not found -- cannot hook the armors" % target)
        return 2

    cur = open(target, "r", errors="replace").read()
    backup = target + ".bak-hercpack"
    if MARK_BEGIN in cur:
        head = cur.split(MARK_BEGIN)[0].rstrip("\n")
        tail = cur.split(MARK_END, 1)[1] if MARK_END in cur else ""
        body = head + "\n\n" + hook + tail.lstrip("\n")
        how = "replaced existing block"
    else:
        if not os.path.exists(backup):
            shutil.copy2(target, backup)
        body = cur.rstrip("\n") + "\n\n" + hook
        how = "appended (backup: %s)" % os.path.basename(backup)
    with open(target, "w", encoding="utf-8", errors="replace") as f:
        f.write(body)
    print("hooked ArmorData.cs -- %s (ONE guarded line; default OFF)" % how)

    print("\nDefault: mechs are NOT registered, so stock clients are unaffected.")
    print("To enable on a server you host:  $MechPack::Enable = 1;  before createServer")
    print("Then in game:  beHerc<Name>();   e.g. %s"
          % (("beHerc" + report["mechs"][0]["mech"].replace("_", "").capitalize() + "();")
             if report["mechs"] else "n/a"))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
