#!/usr/bin/env python3
"""
SWG TCG wire codec (recovered from SWGTCG.dll FUN_1008ccd0 / FUN_1008cdd0).

Integer (compact mode, DAT_10692208==0 — the default):
  byte0: low nibble = low 4 bits of magnitude
         high nibble = extraBytes*2 + signBit
  then `extraBytes` more bytes = (magnitude >> 4) little-endian
  magnitude = min(x, ~x); signBit set when x has its high bit set (negative).
  decode: value = magnitude if not sign else ~magnitude
String:  [varint length L][L bytes][0x00 null]   (writer emits c_str length+1)
Frame:   [uint32 big-endian payload length][payload]   (FUN_1015b0d0)
"""
import struct

def enc_int(x):
    x &= 0xFFFFFFFF
    inv = (~x) & 0xFFFFFFFF
    mag = min(x, inv)
    sign = 1 if inv < x else 0
    extra = []
    hi = mag >> 4
    while hi and len(extra) < 4:
        extra.append(hi & 0xFF)
        hi >>= 8
    b0 = (mag & 0x0F) | (((len(extra) * 2) + sign) << 4)
    return bytes([b0] + extra)

def dec_int(buf, i):
    b0 = buf[i]; i += 1
    low = b0 & 0x0F
    hi = b0 >> 4
    extra = hi >> 1
    sign = hi & 1
    mag = low
    for k in range(extra):
        mag |= buf[i] << (4 + 8 * k); i += 1
    val = mag if not sign else (~mag & 0xFFFFFFFF)
    return val, i

def enc_str(s):
    b = s.encode("latin1") if isinstance(s, str) else s
    return enc_int(len(b)) + b + b"\x00"

def dec_str(buf, i):
    n, i = dec_int(buf, i)
    s = buf[i:i+n]; i += n
    # skip trailing null
    if i < len(buf) and buf[i] == 0: i += 1
    return s.decode("latin1", "replace"), i

def frame(payload):
    return struct.pack(">I", len(payload)) + payload

def unframe(buf):
    """yield payloads from a buffer of [u32 BE len][payload]..."""
    out, i = [], 0
    while len(buf) - i >= 4:
        n = struct.unpack(">I", buf[i:i+4])[0]
        if len(buf) - i - 4 < n: break
        out.append(buf[i+4:i+4+n]); i += 4 + n
    return out, buf[i:]


if __name__ == "__main__":
    # The captured gateway request (conn3), full 46 bytes incl. frame.
    cap = bytes.fromhex("0000002a2e19012e19012e190120016465616462656566646561646265656600"
                        "06746573746572000000000027 05".replace(" ", ""))
    payloads, _ = unframe(cap)
    p = payloads[0]
    print("payload %d bytes: %s" % (len(p), p.hex()))
    # greedy decode attempt: try to read the structure as ints/strings.
    # We know fields end with: str(sessionID), str(username) ... then 3 empty str, int, byte, int(0x57)
    print("\n-- sanity: encode checks --")
    print("enc_int(6)  =", enc_int(6).hex(), "(expect 06)")
    print("enc_int(0x57)=", enc_int(0x57).hex(), "(expect 2705)")
    print("enc_int(16783)=", enc_int(16783).hex())
    print("enc_str('127.0.0.1') =", enc_str("127.0.0.1").hex())
    # decode the trailer 0x57
    v, _ = dec_int(bytes.fromhex("2705"), 0); print("dec_int(2705) =", hex(v))
