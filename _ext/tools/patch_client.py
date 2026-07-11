#!/usr/bin/env python
"""Reversible byte-patches for SWGTCGGame.exe (online 1v1 StartOfGame fixes).

The retail online client built its game locally (createGame), which populates the starting-landscape
work-list Game+0x31c AND deals AND picks missions. The standalone's mirrored-engine online launch
(80008) builds a BARE game and relies on the game-global "self-resolve" flag game+0x139 -- which is 0
online, so the client's own StartOfGame build (advanceTurn -> StartOfGame-entry) is gated OFF and the
game stalls at phase 5 (empty Game+0x31c -> no mission pick -> bounce).

These patches force the client to self-resolve its StartOfGame locally (as it does offline), so each
client builds+deals+picks from the decks it was delivered:

  P1  0x65a6c3  `0F 84 6B 02 00 00`  -> 6x 90        NOP the mission auto-pick skip branch in
                EQStartingLandscapeState::updateState (FUN_0065a530). One reader of game+0x139.
  P2  0x4cf f00 `8A 81 39 01 00 00 C3` -> `B0 01 C3 90 90 90 90`   FUN_004cff00 (the game+0x139
                getter) -> always return 1. Un-gates advanceTurn's StartOfGame-entry (FUN_006300f0)
                so Game+0x31c gets populated -- the actual unblock. (Patch the READER, not the setter
                FUN_004cfee0, which also zeroes the global game ptr DAT_00b54ed8.)

Reversible: the original exe is backed up to SWGTCGGame.exe.orig on first patch; `restore` reverts.
Idempotent per-patch; `patch` REFUSES a site whose bytes are neither the expected original nor the
expected patched form (guards against a different client build). Offsets are computed from the PE
section table. Usage:  python patch_client.py [status|patch|restore]
"""
import sys, os, struct, shutil

VA_BASE = 0x400000

# (name, VA, original_bytes, patched_bytes)
# NOTE: game+0x139 is NOT force-patched here anymore -- forcing the reader to 1 globally makes the client
# self-resolve the whole game (auto-plays both sides -> game-over -> starscape). Instead game+0x139 is
# TOGGLED dynamically (1 for StartOfGame+opening-deal, 0 for networked play) -- prototyped in the cdb
# harness (.re/harness/probe_sog_standalone.cdb), to be baked into a code-cave once the timing is proven.
PATCHES = [
    ("mission auto-pick gate (je -> NOP)",           0x0065a6c3,
     bytes.fromhex("0f846b020000"),     bytes.fromhex("909090909090")),
    ("mulligan skip (SOG phase 6 -> 7)",             0x0065e060,
     bytes.fromhex("7428"),             bytes.fromhex("9090")),
]


def exe_path():
    here = os.path.dirname(os.path.abspath(__file__))          # _ext/tools
    root = os.path.dirname(os.path.dirname(here))              # repo root
    return os.path.join(root, "TCGStandalone", "SWGTCGGame.exe")


def va_to_fileoff(data, va):
    rva = va - VA_BASE
    pe  = struct.unpack_from("<I", data, 0x3c)[0]
    if data[pe:pe+4] != b"PE\x00\x00":
        raise ValueError("not a PE file")
    nsec    = struct.unpack_from("<H", data, pe + 6)[0]
    optsize = struct.unpack_from("<H", data, pe + 20)[0]
    sectbl  = pe + 24 + optsize
    for i in range(nsec):
        s      = sectbl + i * 40
        vsize  = struct.unpack_from("<I", data, s + 8)[0]
        vaddr  = struct.unpack_from("<I", data, s + 12)[0]
        rawsz  = struct.unpack_from("<I", data, s + 16)[0]
        rawptr = struct.unpack_from("<I", data, s + 20)[0]
        if vaddr <= rva < vaddr + max(vsize, rawsz):
            return rawptr + (rva - vaddr)
    raise ValueError("VA 0x%x not in any section" % va)


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    exe = exe_path()
    if not os.path.exists(exe):
        print("SWGTCGGame.exe not found at: %s" % exe); return 1
    bak = exe + ".orig"
    with open(exe, "rb") as f:
        data = bytearray(f.read())

    if cmd == "status":
        for name, va, orig, patched in PATCHES:
            off = va_to_fileoff(data, va)
            cur = bytes(data[off:off + len(orig)])
            state = "PATCHED  " if cur == patched else ("unpatched" if cur == orig else "UNKNOWN  ")
            print("[%s] %-42s VA 0x%x file 0x%x" % (state, name, va, off))
        return 0

    if cmd == "restore":
        if os.path.exists(bak):
            shutil.copy2(bak, exe); print("Restored SWGTCGGame.exe from %s" % bak); return 0
        print("No backup found at %s (nothing to restore)." % bak); return 1

    if cmd == "patch":
        # validate ALL sites first
        plan = []
        for name, va, orig, patched in PATCHES:
            off = va_to_fileoff(data, va)
            cur = bytes(data[off:off + len(orig)])
            if cur == patched:
                print("  already patched: %s" % name); continue
            if cur != orig:
                print("REFUSING: %s @ 0x%x has %s, expected %s (different client build?)"
                      % (name, off, cur.hex(), orig.hex()))
                return 1
            plan.append((name, off, patched))
        if not plan:
            print("All patches already applied -- nothing to do."); return 0
        if not os.path.exists(bak):
            shutil.copy2(exe, bak); print("Backed up original -> %s" % bak)
        for name, off, patched in plan:
            data[off:off + len(patched)] = patched
            print("  patched: %s @ file 0x%x" % (name, off))
        with open(exe, "wb") as f:
            f.write(data)
        print("Done (%d patch(es) applied)." % len(plan))
        return 0

    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(main())
