"""engine_mount_contract -- the ONE list of node names the ENGINE resolves by name.

A node the engine looks up by name is a MOUNT: a weapon muzzle, an eye, a seat, a
collision proxy.  Rename it, drop it, or repurpose it as an animation host and the
lookup returns -1 and the feature silently stops working -- no crash, no log.

This module existed twice before, as an `ENGINE_MOUNT_NAMES` tuple copy-pasted into
`tools/dts2glb.py` and `tools/check_sequence_contract.py`, and ★the two copies had
already drifted apart★: the converter listed "kill", the checker listed "kill 15",
and BOTH were missing every name in player.cpp's mount table and both numbered
families.  A converter that protects a different set than the checker verifies is
worse than either alone.

Every entry below is a live call site in this tree.  Nothing here is inferred.

  exact, resolved by Shape::findNode / getNodeAtCurrentDetail:
    collision              staticBase.cpp:195
    dummy exit             vehicle.cpp:177
    dummy eye              vehicle.cpp:169,180  turret.cpp:216  player.cpp:579
    dummy muzzle           turret.cpp:162,217,658,738,811,1834  vehicle.cpp:179
    dummy pilot            vehicle.cpp:178
    dummy hand             player.cpp:360   PrimaryMount
    dummy unused           player.cpp:361   SecondaryMount
    dummy midback          player.cpp:362   BackpackMount
    dummy lowback          player.cpp:363   JetExhaust
    dummyalways chasecam   player.cpp:578
    dummyalways muzzle     playerInventory.cpp:845
    dummyalways root       player.cpp:377

  prefix, resolved by ShapeInstance::insertOverride:
    lowerback              player.cpp:379-380

  numbered families, built with sprintf and probed from 1 upward:
    dummy muzzle<N>        vehicle.cpp:598,604,613
    dummy passenger<N>     vehicle.cpp:626,632,641

★"kill" / "kill 15" is NOT an engine mount and never was.★  The only occurrence in
the tree is turret.cpp:120, and it is COMMENTED OUT:

    //          int node = ts_shape.findNode ("kill 15");

Both old lists carried it, disagreeing on the spelling, which is the signature of a
name copied from dead code rather than from a call site.  It is classified here as
HISTORICAL: reported so a converter can still refuse to repurpose it (harmless
caution), but never counted as a mount a shape is required to provide.

------------------------------------------------------------------------------
Two matching rules, because the engine genuinely has two
------------------------------------------------------------------------------

★`findNode` is case-INSENSITIVE and `insertOverride` is case-SENSITIVE.★
Shape::findNode -> lookupName compares with `stricmp` (ts_shape.cpp:379-381);
insertOverride compares with `strncmp` (ts_shapeInst.cpp:2837).  A classifier that
picks one rule for both is wrong for half its entries.

★The per-detail suffix has NO SEPARATOR.★  getNodeAtCurrentDetail builds the name
with `itoa(intDet, tempName + strlen(tempName), 10)` (ts_shapeInst.cpp:2634-2637),
so the detail-32 eye is `dummy eye32`, NOT `dummy eye 32`.  The old converter
comment claimed the spaced form, which would never have resolved.

Those two facts pull in opposite directions, so this module answers two different
questions rather than blurring them into one:

  resolves(name)    -- STRICT.  Would the engine actually find this node?
                       Used by the compatibility gate, which must not credit a
                       shape for a mount the engine cannot resolve.

  isReserved(name)  -- CONSERVATIVE.  Might this name be a mount in any plausible
                       spelling (including the spaced detail form)?
                       Used by orphan hosting and by the converter, where
                       over-protecting costs nothing and under-protecting silently
                       breaks a mount.
"""

import re


# -- exact names, resolved by findNode / getNodeAtCurrentDetail ---------------

EXACT_MOUNTS = (
    "collision",
    "dummy exit",
    "dummy eye",
    "dummy muzzle",
    "dummy pilot",
    "dummy hand",
    "dummy unused",
    "dummy midback",
    "dummy lowback",
    "dummyalways chasecam",
    "dummyalways muzzle",
    "dummyalways root",
)

# -- prefix names, resolved by insertOverride (strncmp, CASE-SENSITIVE) -------

PREFIX_MOUNTS = ("lowerback",)

# -- numbered families, sprintf'd and probed from 1 upward -------------------

NUMBERED_MOUNTS = ("dummy muzzle", "dummy passenger")

# -- names carried by the old lists that no live call site resolves ----------

HISTORICAL_NAMES = ("kill",)


EXACT = "exact"
PREFIX = "prefix"
NUMBERED = "numbered"
HISTORICAL = "historical"
DETAIL_VARIANT = "detailVariant"


# A per-detail variant is the base name with the integer detail size appended and
# NO separator (ts_shapeInst.cpp:2634-2637).  The spaced form is accepted only by
# isReserved(), never by resolves().
_STRICT_DETAIL = re.compile(r"^(?P<base>.*?)(?P<det>\d+)$")
_LOOSE_DETAIL = re.compile(r"^(?P<base>.*?)[ _]?(?P<det>\d+)$")


def _norm(name):
    return (name or "").strip()


def classify(name, strict=True):
    """Classify `name`, or return None if the engine does not resolve it.

    Returns a dict: {kind, base, detail, number, caseSensitive}.
      kind    one of EXACT / PREFIX / NUMBERED / HISTORICAL / DETAIL_VARIANT
      base    the engine-side name this node serves
      detail  the per-detail suffix as an int, or None
      number  the family member index for NUMBERED, or None

    `strict=True` follows the engine exactly.  `strict=False` also accepts the
    spaced/underscored detail form, for callers that would rather over-protect a
    name than silently break a mount.
    """
    n = _norm(name)
    if not n:
        return None
    low = n.lower()

    # exact, case-insensitive (findNode -> stricmp)
    for base in EXACT_MOUNTS:
        if low == base:
            return {"kind": EXACT, "base": base, "detail": None,
                    "number": None, "caseSensitive": False}

    # numbered family, case-insensitive.  "dummy muzzle" itself is an EXACT mount
    # and is matched above, so this only fires for a real trailing index.
    for base in NUMBERED_MOUNTS:
        if low.startswith(base):
            rest = low[len(base):]
            if rest.isdigit() and int(rest) >= 1:
                return {"kind": NUMBERED, "base": base, "detail": None,
                        "number": int(rest), "caseSensitive": False}

    # per-detail variant of an exact mount, e.g. "dummy eye32"
    m = (_STRICT_DETAIL if strict else _LOOSE_DETAIL).match(low)
    if m:
        stem = m.group("base").rstrip() if not strict else m.group("base")
        for base in EXACT_MOUNTS:
            if stem == base:
                return {"kind": DETAIL_VARIANT, "base": base,
                        "detail": int(m.group("det")), "number": None,
                        "caseSensitive": False}

    # prefix, CASE-SENSITIVE (insertOverride -> strncmp)
    for base in PREFIX_MOUNTS:
        if n.startswith(base):
            return {"kind": PREFIX, "base": base, "detail": None,
                    "number": None, "caseSensitive": True}

    for base in HISTORICAL_NAMES:
        if low == base or (low.startswith(base + " ") and low[len(base) + 1:].isdigit()):
            return {"kind": HISTORICAL, "base": base, "detail": None,
                    "number": None, "caseSensitive": False}

    return None


def resolves(name):
    """STRICT: would the engine actually find this node by name?

    HISTORICAL names return False -- there is no live call site, so a shape that
    provides one has provided nothing, and a compatibility gate must not credit it.
    """
    c = classify(name, strict=True)
    return bool(c) and c["kind"] != HISTORICAL


def isReserved(name):
    """CONSERVATIVE: might this name be a mount under any plausible spelling?

    Used where the cost is asymmetric -- orphan hosting must NEVER park a constant
    animation on a node the engine resolves by name, and refusing one extra node
    costs nothing while missing one silently breaks a mount.
    """
    return classify(name, strict=False) is not None


def mountsIn(names):
    """Every mount resolved by an iterable of node names.

    Returns {base: [{name, kind, detail, number}, ...]} keyed by engine-side base
    name, so a caller can ask "is `dummy muzzle` provided at any detail?" without
    re-deriving the suffix rules.
    """
    out = {}
    for nm in names:
        c = classify(nm, strict=True)
        if not c or c["kind"] == HISTORICAL:
            continue
        out.setdefault(c["base"], []).append(
            {"name": nm, "kind": c["kind"], "detail": c["detail"],
             "number": c["number"]})
    for v in out.values():
        v.sort(key=lambda e: (e["detail"] is None, e["detail"], e["number"] is None,
                              e["number"], e["name"]))
    return out


def describe(name):
    c = classify(name, strict=False)
    if not c:
        return "%r: not an engine mount" % name
    bits = ["%r: %s mount for %r" % (name, c["kind"], c["base"])]
    if c["detail"] is not None:
        bits.append("detail %d" % c["detail"])
    if c["number"] is not None:
        bits.append("family member %d" % c["number"])
    bits.append("case-sensitive" if c["caseSensitive"] else "case-insensitive")
    if c["kind"] == HISTORICAL:
        bits.append("NO live call site -- does not resolve")
    return ", ".join(bits)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for a in sys.argv[1:]:
            print(describe(a))
    else:
        print("engine mounts resolved by name in this tree:")
        for n in EXACT_MOUNTS:
            print("  exact      %s" % n)
        for n in PREFIX_MOUNTS:
            print("  prefix     %s* (case-sensitive)" % n)
        for n in NUMBERED_MOUNTS:
            print("  numbered   %s<N>, N from 1" % n)
        for n in HISTORICAL_NAMES:
            print("  historical %s (commented out at turret.cpp:120 -- not a mount)" % n)
