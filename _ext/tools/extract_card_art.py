#!/usr/bin/env python3
"""Extract card art from the client's own cards.rcc into the Collection Manager.

Images are NOT shipped -- this builds them on your PC from TCGStandalone\\cards.rcc.
Card faces live at  images/card/<catalog_id>.jpg  inside the Qt 'qres' pack, each
payload XOR-obfuscated with 0x73. Output -> collectionmanager\\art\\images\\card\\.

Run via the launcher menu ("Extract card art"), or directly:
    _ext\\python\\python.exe _ext\\tools\\extract_card_art.py
Stdlib only; no third-party deps.
"""
import os
import struct
import sys
import zlib

XOR = bytes(i ^ 0x73 for i in range(256))
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))            # ..\.. from _ext\tools -> bundle root
RCC = os.path.join(ROOT, "TCGStandalone", "cards.rcc")
OUT = os.path.join(ROOT, "collectionmanager", "art")


def _u16(b, o): return struct.unpack_from(">H", b, o)[0]
def _u32(b, o): return struct.unpack_from(">I", b, o)[0]


def main():
    if not os.path.exists(RCC):
        print("ERROR: cards.rcc not found at %s" % RCC)
        print("       (it ships with the game client under TCGStandalone\\)")
        return 1
    b = open(RCC, "rb").read()
    if b[:4] != b"qres":
        print("ERROR: %s is not a Qt .rcc file" % RCC)
        return 1
    ver, tree_off, data_off, name_off = struct.unpack(">IIII", b[4:20])
    node_size = 14 + (8 if ver >= 2 else 0)

    def name(nf):
        p = name_off + nf
        n = _u16(b, p)
        p += 6                                            # skip u16 len + u32 hash
        return b[p:p + n * 2].decode("utf-16-be", "replace")

    def node(i):
        o = tree_off + i * node_size
        flags = _u16(b, o + 4)
        d = {"name": name(_u32(b, o)), "flags": flags, "is_dir": bool(flags & 2)}
        if d["is_dir"]:
            d["cc"] = _u32(b, o + 6); d["fc"] = _u32(b, o + 10)
        else:
            d["df"] = _u32(b, o + 10)
        return d

    print("Extracting card art from %s ..." % RCC)
    written = 0
    stack, seen = [(0, "")], set()
    while stack:
        idx, prefix = stack.pop()
        if idx in seen:
            continue
        seen.add(idx)
        nd = node(idx)
        path = prefix if idx == 0 else (prefix + "/" + nd["name"] if prefix else nd["name"])
        if nd["is_dir"]:
            for c in range(nd["cc"]):
                stack.append((nd["fc"] + c, path))
        elif path.startswith("images/card/"):
            p = data_off + nd["df"]
            blen = _u32(b, p)
            raw = b[p + 4:p + 4 + blen]
            if nd["flags"] & 1:                           # zlib-compressed (u32 size + stream)
                try:
                    raw = zlib.decompress(raw[4:])
                except Exception:
                    pass
            raw = raw.translate(XOR)                       # de-obfuscate
            dest = os.path.join(OUT, *path.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(raw)
            written += 1
            if written % 500 == 0:
                print("  %d..." % written)
    print("Done: %d card images -> %s" % (written, os.path.join(OUT, "images", "card")))
    print("The Collection & Deck Manager's Boosters tab will now show card art.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
