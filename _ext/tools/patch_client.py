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

# (name, VA, original_bytes, patched_bytes, group)
# Groups let you apply a subset:  patch ctrl   /   status ctrl   /   patch ctrl board
# Default (no group arg) = ALL groups.
#
# --- group "sog" : the OLD self-resolve StartOfGame patches (single-player-style deal on each client) ---
# NOTE: game+0x139 is NOT force-patched here -- forcing the reader to 1 globally self-resolves the whole game
# (auto-plays both sides -> game-over -> starscape). It is toggled dynamically in the cdb harness instead.
#
# --- group "ctrl" : CONTROLLER-BUILD (M2). Hijack the online 80008 handler FUN_00642020 to build the real
# 0x480 EQMainController (ctor FUN_0063f070, EQGame subobject at controller+8) instead of the bare 0x408 EQGame
# (ctor FUN_0063a5e0). Proven live (probe_b2/b4): native 65/58 then populate the players and the board renders.
#   push 408h @0x642093            -> push 480h                     (alloc the controller size)
#   call 0x63a5e0 @0x6420b2        -> call shim @0x6420f5           (redirect to the controller ctor)
#   int3 pad @0x6420f5 (9 of 11B)  -> shim: call 0x63f070; add eax,8; ret
#     e8 76 cf ff ff  = call 0x63f070  (rel = 0x63f070-0x6420fa)
#     83 c0 08        = add eax,8       (ctor returns controller; assert @0x6420c5 wants game=controller+8)
#     c3              = ret             (-> 0x6420b7 with eax=controller+8)
# Only affects the online path (offline uses createGame 0x6fa490, not 80008).
#
# --- group "board" : de-fang the board-build bounce. FUN_007f3f70 @0x7f40ea `je returnToMenu` when the
# opponent seat screen+0x28 is null -> "bounce to starscape". NOP it so the board HOLDS (scaffold until the
# server registers the opponent as a type-0xA LobbyService participant node -- see NETWORKED-PLAY-PLAN.md).
PATCHES = [
    ("mission auto-pick gate (je -> NOP)",           0x0065a6c3,
     bytes.fromhex("0f846b020000"),     bytes.fromhex("909090909090"), "sog"),
    ("mulligan skip (SOG phase 6 -> 7)",             0x0065e060,
     bytes.fromhex("7428"),             bytes.fromhex("9090"),         "sog"),
    ("ctrl: 80008 alloc 0x408 -> 0x480",             0x00642093,
     bytes.fromhex("6808040000"),       bytes.fromhex("6880040000"),   "ctrl"),
    ("ctrl: 80008 ctor 63a5e0 -> shim",              0x006420b2,
     bytes.fromhex("e82985ffff"),       bytes.fromhex("e83e000000"),   "ctrl"),
    ("ctrl: controller-ctor shim (cave)",            0x006420f5,
     bytes.fromhex("cccccccccccccccccc"), bytes.fromhex("e876cfffff83c008c3"), "ctrl"),
    ("board: de-fang bounce (je returnToMenu->NOP)", 0x007f40ea,
     bytes.fromhex("0f84ba000000"),     bytes.fromhex("909090909090"), "board"),
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
    groups = set(g.lower() for g in sys.argv[2:])          # optional group filter; empty = all
    def sel():
        return [p for p in PATCHES if not groups or p[4] in groups]
    exe = exe_path()
    if not os.path.exists(exe):
        print("SWGTCGGame.exe not found at: %s" % exe); return 1
    bak = exe + ".orig"
    with open(exe, "rb") as f:
        data = bytearray(f.read())

    if cmd == "status":
        for name, va, orig, patched, grp in sel():
            off = va_to_fileoff(data, va)
            cur = bytes(data[off:off + len(orig)])
            state = "PATCHED  " if cur == patched else ("unpatched" if cur == orig else "UNKNOWN  ")
            print("[%s] (%-5s) %-42s VA 0x%x file 0x%x" % (state, grp, name, va, off))
        return 0

    if cmd == "restore":
        if os.path.exists(bak):
            shutil.copy2(bak, exe); print("Restored SWGTCGGame.exe from %s" % bak); return 0
        print("No backup found at %s (nothing to restore)." % bak); return 1

    if cmd == "patch":
        # validate selected sites first
        plan = []
        for name, va, orig, patched, grp in sel():
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
            print("All selected patches already applied -- nothing to do."); return 0
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
