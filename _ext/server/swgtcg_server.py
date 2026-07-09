#!/usr/bin/env python3
"""
SWG TCG emulator server -- Phase 1: gateway reply.

Listens on 16782 (gateway) and 16783 (lobby/connection). On the gateway port, when
the client sends GatewayCommand_GetConnectionServer (cmd id 0x57), reply with a
forged GetConnectionServer response pointing the client at our lobby (127.0.0.1:16783).

Reply layout (mirror of FUN_100fe9f0 serialize; consumed by FUN_100fe650 run):
  [base header][str1=""][str2=""][str3=HOST][int=PORT][byte=1 success][int=0x57]
FUN_100fe650: if byte(+0x5c)!=0 -> defaultHost=str3(+0x3c), defaultPort=int(+0x58),
reconnect. So str3/int/byte are the meaningful fields.

We REUSE the header bytes the client itself sent (a valid GatewayCommand envelope) --
empirically the simplest first guess; adjust if the client rejects it.
"""
import socket, threading, struct, time, os, zlib
from swgcodec import enc_int, enc_str, frame, unframe, dec_int, dec_str
from gameserialize import (serialize_minimal_game, build_2p_game, build_real_game_replay,
                           build_166_v48_from_capture, build_base_render_game, build_capture_remap,
                           build_capture_hybrid, build_capture_hybrid2, build_eq_80003_blob,
                           build_eq_prestart_blob, build_eq_prestart_capture, REPLAY_GAMEID,
                           BASE_GAMEOVER, BASE_READY_FOR_START, BASE_QUEUE_MODE)
import config, auth
import db as dbmod
import eqdeck_codec

LOBBY_HOST = "127.0.0.1"
LOBBY_PORT = 16783
GW_CMD_GETCONNSERVER = 0x57
OUTDIR = os.environ.get("SWGTCG_CAPTURE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
CAPTURE = os.environ.get("SWGTCG_CAPTURE", "1") != "0"   # write per-connection .bin captures (dev); set 0 in prod
if CAPTURE:
    os.makedirs(OUTDIR, exist_ok=True)

def hexdump(b):
    return " ".join("%02x" % c for c in b)

def parse_getconnserver(payload):
    """GatewayCommand_GetConnectionServer(414), no client header. Layout (confirmed vs capture):
    3 x env(414,ver) [9 bytes], then dec_str sessionID, dec_str username. Returns (session_id, username)."""
    i = 0
    for _ in range(3):
        _, i = dec_int(payload, i); _, i = dec_int(payload, i)   # env(414,ver) x3
    sid, i = dec_str(payload, i)
    user, i = dec_str(payload, i)
    return sid, user

def build_reply(req_payload, success=1):
    """GetConnectionServer reply: reuse the request's 3x(414,1) env header, then point the client
    at our lobby (config.SERVER_IP:LOBBY_PORT). FUN_100fe650: if byte(+0x5c)!=0 it reconnects to
    str3(+0x3c):int(+0x58); success=0 leaves the redirect off (invalid session -> no lobby)."""
    header = req_payload[:9]              # 3 x (414,1) env levels (9 bytes, confirmed)
    body = (enc_str("")                   # str1 (+4)  - unused in reply
            + enc_str("")                 # str2 (+0x20)
            + enc_str(config.SERVER_IP)   # str3 (+0x3c) = connection-server host
            + enc_int(LOBBY_PORT)         # int  (+0x58) = port
            + enc_int(1 if success else 0)  # byte (+0x5c) = success flag
            + enc_int(GW_CMD_GETCONNSERVER))  # int (+0x60) = cmd id 0x57
    return frame(header + body)

def parse_sendsessionid(payload):
    """LoginCommand_SendSessionID(411), after the 12-byte CLIENT_HDR. Layout (confirmed vs capture):
    3 x env(411,ver) [9 bytes], then dec_str sessionID (the first field). Returns session_id or None."""
    try:
        body = payload[CLIENT_HDR:]
        i = 0
        for _ in range(3):
            _, i = dec_int(body, i); _, i = dec_int(body, i)     # env(411,ver) x3
        sid, _ = dec_str(body, i)
        return sid
    except Exception:
        return None

# The FinalLive retail DLL connects to the connection server (16783) but never queues/sends its login frame
# (SendSessionID 411). We stash the sessionID validated on the gateway (16782) and, when SWGTCG_PROACTIVE_LOBBY is
# set, synthesize the lobby login for it so our lobby handler engages. Standalone frame layout: CLIENT_HDR + 3x
# env(411,1) + str(sessionID).
LAST_GW_SESSION = None
def build_fake_sendsessionid(sid):
    hdr = bytes([0x3b, 0x9a, 0xca, 0x00] + [0] * 8)   # the 12-byte client routing header
    payload = hdr + (enc_int(411) + enc_int(1)) * 3 + enc_str(sid)
    return frame(payload)

def log(port, n, msg):
    print("[:%d c%d] %s" % (port, n, msg));

# classid -> command name, loaded from ../CLASSID_MAP.txt (tab-separated: id<TAB>name<TAB>fn)
CLASSID_NAMES = {}
def _load_classids():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CLASSID_MAP.txt")
    try:
        for line in open(p):
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0].strip().isdigit():
                CLASSID_NAMES[int(parts[0])] = parts[1].strip()
    except Exception as e:
        print("[classid map load err] %s" % e)
_load_classids()
print("loaded %d classid names" % len(CLASSID_NAMES))

# Client->server lobby frames prepend a fixed 12-byte routing header (3b 9a ca 00 + 8x00);
# the command (classid varint + fields) follows. Server->client frames omit this header.
CLIENT_HDR = 12

# --- KEEPALIVE (workflow wf_bb3985f4, byte-verified) ---------------------------------------------------------
# The client's ClientApplication watchdog counter (ClientApplication+0x5c) increments +10 every 10s (ping timer
# FUN_1043da80) and pops "60 seconds have elapsed without a response from the server..." at 60 (DLL 0x1006b5a5).
# The ONLY reset is NetworkCommand_Ping(classid 112)::execute (FUN_10171a30 -> FUN_10068e20 zeroes +0x5c),
# fired on RECEIPT of a 112 -- unconditional, the client need not have pinged first. Reactive echo alone isn't
# enough on the board (the client stops emitting pings once the game session drives), so PROACTIVELY push a 112
# to every connected client every KEEPALIVE_IV (<60s). Replay the client's OWN captured 112 body (the exact
# envelope its deserializer accepts) -> falls back to classid+spoof-int if none captured yet.
KEEPALIVE    = os.environ.get("SWGTCG_KEEPALIVE", "1") != "0"
KEEPALIVE_IV = float(os.environ.get("SWGTCG_KEEPALIVE_IV", "10"))   # seconds; must stay < 60
LAST_PING    = {}   # acct -> last client-sent 112 body (header-stripped, ready to re-frame)
LAST_SCENARIO_STR = {}   # acct -> last scenario id string from a 487 (to correlate with the 415's nodeId)

def parse_client_cmd(payload):
    """Return (classid, body_after_header) for a client->server frame, or (None, b'')."""
    if len(payload) <= CLIENT_HDR:
        return None, b""
    body = payload[CLIENT_HDR:]
    try:
        cid, _ = dec_int(body, 0)
    except Exception:
        return None, body
    return cid, body

import re as _re
_CAMPAIGN_ID_LO, _CAMPAIGN_ID_HI = 0x15000, 0x16000

def _campaign_key(body):
    """Stable latest-wins key for a campaign/scenario progress frame (cid 415/487).
    Scenario reports (487) carry the scenario id as a length-prefixed ASCII string
    (e.g. 'COTF_Scenario06') -> key on that. SetCampaignStatus(415) is all ints ->
    key on the campaign id (the int in the 0x15xxx campaign-id range). Fallback
    'default' still means latest-wins per cid -- it never accumulates garbage rows."""
    runs = _re.findall(rb"[A-Za-z0-9_-]{4,}", body)  # '-' so 'Tutorial01_..-main' extracts whole
    if runs:
        return max(runs, key=len).decode("latin1")
    i = 0
    while i < len(body):
        try:
            v, i = dec_int(body, i)
        except Exception:
            break
        if _CAMPAIGN_ID_LO <= v <= _CAMPAIGN_ID_HI:
            return "campaign_%d" % v
    return "default"


# --- campaign completion: parse the 415 report + build account property 0x1054 ---------------------
# The campaign tree reads account property 0x1054 (an IntValueMap node->{archetype->IntegerList[diff]})
# from the GetAccountInfo(297) reply. On scenario completion the client sends SetCampaignStatus(415)
# carrying the nodeId (0x157xx), an archetype id (0x13886..9) and a difficulty (1/2/3). We classify the
# 415's ints by range (robust to the exact field/version layout) and store them; at login we rebuild
# 0x1054 from the stored completions. (Full RE: workflow decode of CID_415/454 + the 4 tree readers.)
_NODE_LO, _NODE_HI = 0x157D0, 0x15830     # campaign/scenario node ids (tutorials 0x157D2)
_CAMPAIGN_PARAM_KEY = 0x13888             # == hash("archetype"); the CONSTANT middle level in the 0x1054
                                          # nesting (live-captured from the exe writer FUN_006409d0).

def _parse_415(body):
    """-> (node_id, archetype, difficulty). 415 fields (positional, after the AccountCommand base int):
    [node, difficulty, archetype, paramKeyHash(0x13888), ""]. archetype is a small value (1/2), NOT the
    0x13888 hash (that was a mis-read). node/arch None if not present."""
    ints, i = [], 0
    while i < len(body):
        try:
            v, i = dec_int(body, i)
        except Exception:
            break
        ints.append(v)
    node = next((v for v in ints if _NODE_LO <= v <= _NODE_HI), None)
    arch = None
    diff = 1
    if node is not None:
        idx = ints.index(node)
        if idx + 1 < len(ints) and ints[idx + 1] in (1, 2, 3):
            diff = ints[idx + 1]      # difficultyValue = field right after the nodeId
        if idx + 2 < len(ints):
            arch = ints[idx + 2]      # archetype = the field after difficulty (1/2), not the 0x13888 hash
    return node, arch, diff

def _value_integerlist(ints):
    """ValueData IntegerList (mTypeID 6): classid22, ver1, mTypeID6, ownRef1, count, ints."""
    return (enc_int(22) + enc_int(1) + enc_int(6) + enc_int(1)
            + enc_int(len(ints)) + b"".join(enc_int(v) for v in ints))

def _value_intvaluemap(items):
    """ValueData IntValueMap (mTypeID 0xE): classid22, ver1, mTypeID0xE, ownRef1, count, then
    count*(enc_int(key) + <nested ValueData bytes>). items = list of (int_key, value_bytes)."""
    body = enc_int(len(items))
    for k, v in items:
        body += enc_int(k) + v
    return enc_int(22) + enc_int(1) + enc_int(0xE) + enc_int(1) + body

def _value_int2(v):
    """ValueData scalar int, mTypeID 2 (the type the unlock reader FUN_00882e50/FUN_005d81a0 expects
    for a per-scenario completion property): classid22, ver1, mTypeID2, ownRef1, value."""
    return enc_int(22) + enc_int(1) + enc_int(2) + enc_int(1) + enc_int(v)

def build_node_props(comp):
    """The REAL persistence the standalone's tree reads for unlock: a flat top-level account property
    keyed by each completed scenario's node id, value = a type-2 int (live-RE'd from FUN_00882e50 ->
    account.getProperty(node) -> mTypeID==2). Returns a list of (node_key, value_bytes) property entries.
    Value = the max difficulty cleared for that node (1 easy / 2 med / 3 hard)."""
    out = []
    for node, archs in comp.items():
        maxdiff = max((d for diffs in archs.values() for d in diffs), default=1)
        out.append((node, _value_int2(maxdiff)))
    return out

def build_prop_0x1054(comp):
    """comp = {node_id: {archetype: [difficulties]}} -> the property entry bytes for the 114 PropertySet.
    EXE structure (live-captured from the writer FUN_006409d0's create path):
        0x1054 (IntValueMap) [node] -> IntValueMap [0x13888] -> IntValueMap [archetype] -> IntegerList[difficulties]
    i.e. there is a CONSTANT 0x13888 ("archetype" param hash) map level between the node and the archetype."""
    outer = []
    for node, archs in comp.items():
        arch_map = [(arch, _value_integerlist(sorted(set(diffs)))) for arch, diffs in archs.items()]
        mid = [(_CAMPAIGN_PARAM_KEY, _value_intvaluemap(arch_map))]
        outer.append((node, _value_intvaluemap(mid)))
    return enc_int(0x1054) + _value_intvaluemap(outer)


def dispatch(conn, n, payload, acct=None, dbc=None):
    """Match CONTROLLER (replaces the broken 'relay everything' model). The server is the
    authority: client match commands are NEVER relayed raw. Relaying the client's own
    AddGroups(94) injected a FOREIGN Match into the peer's lobby model -- gid=0 collision
    (both clients invent gid=0) + creator-account/deck-owner state the receiver lacks --
    and the receiving standalone AV'd in its interactive match UI. Instead the server
    assigns a UNIQUE match gid, advertises a CLEAN joinable room to the other client(s),
    pairs them, and on BOTH-ready drives LaunchGame(116) -> SendSerializedGame(262).
    Match commands route BY ACCOUNT (CONN_MATCH), not by the client's gid, which sidesteps
    the gid=0 collision entirely."""
    cid, body = parse_client_cmd(payload)
    if cid is None:
        log(16783, n, "RECV (undecodable %dB): %s" % (len(payload), hexdump(payload[:24])))
        return
    if cid == 112:                         # Ping keepalive -- echo + remember the body for the proactive push.
        if acct is not None: LAST_PING[acct] = body
        conn.sendall(frame(body)); return
    if cid == 87:                          # SynchronizationCommand_Update -- client keepalive ack; drop.
        return
    name = CLASSID_NAMES.get(cid, "?")
    log(16783, n, "RECV %s(%d) acct=%s %dB: %s" % (name, cid, acct, len(body), hexdump(body[:40])))

    if cid == 117:                         # ★ GameCommand_ReadyForStartOfGame -- the client's OWN engine reports
        # readiness (real 2-client start-of-game handshake). RELAY it verbatim to the peer so BOTH engines see
        # both players ready -> the 2-seat deal gate releases -> the deal runs -> the board holds. The server's
        # synthetic 117s (DECK58 launch/pump) are not the client's authoritative form; this forwards the real one.
        mid = CONN_MATCH.get(acct); m = MATCHES.get(mid)
        if m and m.get("launched"):
            relayed = 0
            for peer in list(m["members"]):
                if peer == acct: continue
                pc = LOBBY.get(peer)
                if pc:
                    try: pc.sendall(frame(body)); relayed += 1
                    except Exception: pass
            log(16783, n, "-> [117-RELAY] acct=%s ready -> %d peer(s) mid=%s" % (acct, relayed, mid))
        return

    if cid in (60, 62):                    # ★ NATIVE StartOfGame drive (workflow wf_954d1364): the two cross-client
        # gates that park the mirrored EQStartOfGameState. CardSelected(60) = each player's phase-4 STARTING-MISSION
        # pick -> EQStartingLandscapeState.cardSelected records it (child+0xec map) -> child+0x118 fills -> child pops
        # -> SOG phase 5. ButtonPressed(62) = each player's phase-6 MULLIGAN Keep/Redraw -> SOG::buttonPressed records
        # SOG+0xFC[player] -> phase 6->7 finalize (deal-complete + seat reveal/activate + setActivePlayer). Both carry
        # the player id in the body + have no LOCAL-player guard on the relayed path (62's guard is on mGameTurn's
        # ACTIVE player, synced), so forward VERBATIM to the peer(s) like 117 -> each mirrored engine records BOTH
        # players' choices and advances 4->5->6->7 NATIVELY (replaces the host-force child-gate NOP). Deliver once per
        # distinct choice (62 Redraw re-shuffles on every apply; 60/62-Keep are idempotent).
        mid = CONN_MATCH.get(acct); m = MATCHES.get(mid)
        if m and m.get("launched"):
            relayed = 0
            for peer in list(m["members"]):
                if peer == acct: continue
                pc = LOBBY.get(peer)
                if pc:
                    try: pc.sendall(frame(body)); relayed += 1
                    except Exception: pass
            log(16783, n, "-> [SOG-RELAY cid=%d] acct=%s -> %d peer(s) mid=%s" % (cid, acct, relayed, mid))
        return

    if cid == 94:                          # Create: the client advertised its OWN new Match(104) locally.
        owner = acct                       # Echo the creator's real bytes back (so its model registers the
        try:                               # match at its invented gid -> its self-Join resolves), then
            adv = _walk_advertise(body)    # advertise a CLEAN, joinable Match(104) at a unique mid to peers.
        except Exception as e:
            adv = None
            log(16783, n, "   (advertise parse failed: %s)" % e)
        if adv is None:
            # The invented gid can't be located -> we can't bridge the room to peers without addressing the
            # creator at the wrong gid. Echo so the creator still sees its OWN room; do not advertise it.
            conn.sendall(frame(body))
            log(16783, n, "-> CREATE acct=%s advertise unparseable -> echoed to creator only (not bridged)" % owner)
            return
        mname = adv["name"]; owner_gid = adv["gid"]
        _evict_account(owner)              # a client occupies at most one match (sends teardown -> outside lock)
        with _clients_lock:                # ATOMIC: insert the match, store its advert, and capture the
            mid = _alloc_match_locked(owner, mname, owner_gid)   # current recipients in ONE critical section so
            adv_frame = _peer_advertise(mid, mname or ("Match %s" % owner), body, adv)  # a concurrent login
            MATCHES[mid]["advert"] = adv_frame                   # can't both snapshot AND get the live broadcast
            MATCHES[mid]["adv_body"] = body                      # creator's raw advertise (re-advertised at a
            MATCHES[mid]["adv_meta"] = adv                       # quick-joiner's invented gid in cid==92)
            others = [(a, c) for a, c in LOBBY.items() if a != owner]
        conn.sendall(frame(body))          # echo to the creator (registers its own gid=owner_gid)
        oname = NAMES.get(owner, "player%s" % owner)
        for a, c in others:
            try:
                c.sendall(build_introduce(owner, oname))
                c.sendall(adv_frame)
                # NOTE: do NOT push the owner's Join into viewers here -- live test showed it broke a peer's
                # subsequent join (the room got removed instead of joined). The casual-LIST player count is
                # deferred; the in-room (details) count is fixed via field3=PLAYER_ROLE on the roster replays.
            except Exception:
                pass
        log(16783, n, "-> CREATE mid=%s owner=%s owner_gid=%s '%s' advertised(%s) to %s"
            % (mid, owner, owner_gid, mname, ADVERTISE_MODE, [a for a, _ in others]))
        return

    if cid == 92:                          # Join: category-TAB navigation (field3==1), or a MATCH join
        try:                               # (field3==2): creator SELF-join (its own gid); LIST-join (grp == an
            _, grp, jf3 = _parse_join(body)  # advertised room's mid); or QUICK-JOIN (client INVENTS a gid ->
        except Exception:                  # server materializes+pairs it). field3 cleanly separates the two.
            grp, jf3 = None, None
        # CATEGORY-TAB / MAIN-MENU NAVIGATION: the Casual Games + Tournaments buttons send Join(92) to the category
        # gid with field3==1, and the client navigates ONLY when the server pushes ChangeLobbyDisplay(291) back
        # (RE a09f084a; the button does NOT navigate locally). Capture-proven (206 joins, perfect split): field3==1
        # is ALWAYS a category tab (gids 2/3/5); match joins are ALWAYS field3==2 -- so this never collides with
        # matchmaking.
        #
        # CASUAL button (grp=2): the earlier round-9 "wall" (ChangeLobbyDisplay(100) breaks the casual Create
        # dialog) is CRACKED via EXE RE (out/exe/RUN_540470 ChangeLobbyDisplay run + RUN_545290 Join run): the 291
        # run fires the switchScreen event 0x46 -- which REBUILDS the lobby screen using the CURRENT group -- and
        # only THEN sets the current group to findGroup(displayId). At LOGIN a self-Join(grp=100) sets the current
        # group to the casual lobby BEFORE the 291, so the rebuild is valid + Create works; the bare button had no
        # such preceding Join, so the rebuild used a stale/invalid current group -> Create wouldn't open. FIX: mimic
        # login -- send a self-Join(grp=100) (Join run: account==local && findGroup(100)!=0 -> set-current-group
        # FUN_00468d20) THEN ChangeLobbyDisplay(100). CASUAL_CAT=100 is registered at login so findGroup resolves.
        if PUSH_TOURNAMENTS and jf3 == 1 and grp == 2:
            conn.sendall(frame(_env(92, [1, 1, 1]) + enc_int(acct) + enc_int(CASUAL_CAT) + enc_int(0)))
            conn.sendall(build_changelobbydisplay(CASUAL_CAT))
            log(16783, n, "-> casual button: self-Join(grp=%d)+ChangeLobbyDisplay (mimic login, Create-safe)" % CASUAL_CAT)
            return
        # TOURNAMENT tab (grp=5): navigate to the hardcoded display-5 screen + push its gid=5 group/299/293 on
        # demand (DEFERRED off casual logins so casual Create keeps working).
        if PUSH_TOURNAMENTS and jf3 == 1 and grp == TOURNEY_LOBBYTYPE:
            _push_tourney_lobby(conn, dbc, acct, 16783, n)   # gid=5 group + 299/293 on demand
            conn.sendall(build_changelobbydisplay(TOURNEY_LOBBYTYPE))
            log(16783, n, "-> tab nav: Join(grp=%d field3=1) -> ChangeLobbyDisplay(display=%d)" % (grp, TOURNEY_LOBBYTYPE))
            return
        with _clients_lock:
            owned = MATCHES.get(CONN_MATCH.get(acct))
            self_join = bool(owned and owned["owner"] == acct and grp == owned["gids"].get(acct))
            explicit = (not self_join and grp in MATCHES and MATCHES[grp]["owner"] != acct)
            quick_target = None if (self_join or explicit) else _find_open_match_locked(acct)
        if self_join:
            # Creator entering its own room. The cid-94 echo already registered the group; we DON'T try to
            # auto-open the ready-up: the only screen-switch command (ChangeLobbyDisplay/event 0x46) BOUNCES the
            # creator out of its match REGARDLESS of gid (live-confirmed for gid 0 AND a real mid -> Leave+teardown),
            # and set-current-group (FUN_10068d20) doesn't switch the screen. Auto-open is a client-side nav of the
            # "join room" CLICK that the create button doesn't do -> no safe server fix; the creator clicks in once.
            conn.sendall(frame(body))
            _replay_ready(conn, owned, grp)   # restore opponents' ready checkboxes on (re-)entry
            log(16783, n, "-> echoed Join(self) acct=%s gid=%s (+ready snapshot)" % (acct, grp))
            return
        target_mid = grp if explicit else quick_target
        if target_mid is None:
            conn.sendall(frame(body))      # no open match to quick-join -> echo only, inject no state
            log(16783, n, "-> echoed Join acct=%s grp=%s (no open match)" % (acct, grp))
            return
        if CONN_MATCH.get(acct) not in (None, target_mid):
            _evict_account(acct)           # a client occupies at most one match
        advertise_first = not explicit     # quick-joiner lacks the room -> materialize it at ITS invented gid
        joiner_gid = grp                   # the gid THIS client will know the match under (== mid when explicit)
        with _clients_lock:
            m = MATCHES.get(target_mid)
            full = (m is None or m.get("launched") or m.get("state") not in ("open", "full")
                    or (acct not in m["members"] and len(m["members"]) >= 2))
            # A member of an ALREADY-LAUNCHED match that re-sends Join(92) (e.g. the client auto-joins the
            # game-group gid=launch created by the launch burst) must NOT be bounced: ChangeLobbyDisplay(291)
            # -> WALobbyManager eventFilter case 0x46 -> switchScreen(3)=lobby kicks it off the game screen
            # (FinalLive FUN_10433810; the documented joiner nav-bounce). Detect + suppress the 291 for it.
            member_of_launched = bool(m is not None and m.get("launched") and acct in m["members"])
            if not full:
                if acct not in m["members"]: m["members"].append(acct)
                CONN_MATCH[acct] = target_mid
                m["gids"][acct] = joiner_gid
                if len(m["members"]) >= 2: m["state"] = "full"
                existing = [x for x in m["members"] if x != acct]
                ownacct = m["owner"]; members = list(m["members"])
                adv_body = m.get("adv_body"); adv_meta = m.get("adv_meta"); mname = m.get("name", "")
                mem_conns = [(x, LOBBY.get(x), gid_for(x, m)) for x in existing]
        if full:
            if member_of_launched:
                # in-game member re-joined the game-group post-launch -> echo only, DO NOT bounce (no 291).
                conn.sendall(frame(body))
                log(16783, n, "-> Join(in-game member) acct=%s target=%s -- echoed, NO 291 (anti-bounce)" % (acct, target_mid))
                return
            conn.sendall(build_changelobbydisplay(CASUAL_CAT))      # bounce a late/over-cap joiner to casual
            log(16783, n, "-> Join REJECT (full/launched) acct=%s target=%s" % (acct, target_mid))
            return
        if advertise_first:                # materialize the match in the quick-joiner's OWN gid namespace so
            try:                           # its self-Join below resolves (else findGroup fails -> FATAL).
                conn.sendall(build_introduce(ownacct, NAMES.get(ownacct, "player%s" % ownacct)))
                conn.sendall(_peer_advertise(joiner_gid, mname or ("Match %s" % ownacct), adv_body, adv_meta))
            except Exception:
                pass
        conn.sendall(frame(body))          # echo -> joiner self-joins joiner_gid (now in its registry) -> enters
        for x in existing:                 # replay roster INTO the joiner (in the joiner's namespace gid)
            try:
                conn.sendall(build_introduce(x, NAMES.get(x, "player%s" % x)))
                conn.sendall(build_join(x, joiner_gid, NAMES.get(x, "player%s" % x), field3=PLAYER_ROLE))
            except Exception:
                pass
        for x, xc, xgid in mem_conns:      # tell existing members the joiner entered (each in ITS namespace)
            if xc is None:
                continue
            try:
                xc.sendall(build_introduce(acct, NAMES.get(acct, "player%s" % acct)))
                xc.sendall(build_join(acct, xgid, NAMES.get(acct, "player%s" % acct), field3=PLAYER_ROLE))
            except Exception:
                pass
        _replay_ready(conn, m, joiner_gid)                         # show the joiner any already-ready member
        _unadvertise_to_nonmembers(target_mid)                     # room full -> stop advertising it
        log(16783, n, "-> PAIR acct=%s joined %s (%s gid=%s) owner=%s members=%s"
            % (acct, target_mid, "list" if explicit else "quick", joiner_gid, ownacct, members))
        return

    if cid == 93:                          # Leave -> tear the match down (2-player: either leaver ends it)
        with _clients_lock:
            _mid = CONN_MATCH.get(acct); _m = MATCHES.get(_mid)
            _launched = bool(_m and _m.get("launched"))
        conn.sendall(frame(body))          # echo the Leave (client leaves the match-queue ROOM)
        if _launched:
            # POST-LAUNCH Leave = the client leaving the match-queue room to ENTER the game screen (BOTH clients
            # send it on LaunchGame). It is NOT an abandon. Do NOT tear down or call _remove_from_match: every
            # leave path sends build_changelobbydisplay(CASUAL_CAT), a 291 that hits WALobbyManager eventFilter
            # (FinalLive FUN_10433810) case 0x46 -> switchScreen(3)=lobby and BOUNCES the joiner off the board
            # (live-traced 2026-07-04: target=3 caller=10433bcc after target=11). Just echo, keep the game alive.
            log(16783, n, "-> Leave(93) acct=%s on LAUNCHED mid=%s -> enter-game transition (no teardown/291)" % (acct, _mid))
            return
        _leave_match(conn, n, acct)
        return

    if cid == 101:                         # MatchCommand_SelectDeck -- store per account + persist last_deck.
        with _clients_lock:
            mid = CONN_MATCH.get(acct)
            if mid in MATCHES: MATCHES[mid].setdefault("decks", {})[acct] = body
        if PERSIST_LAST_DECK and dbc is not None:
            _persist_last_deck(dbc, acct, body, n)
        _sync_ready_to(conn, acct)         # in the match dialog now -> resync opponent's ready checkbox
        log(16783, n, "-> stored SelectDeck acct=%s match=%s (%dB)" % (acct, mid, len(body)))
        return

    if cid == 361:                         # DeckCommand_AddOnlineDeck -- client saved/edited a deck; PERSIST.
        parsed = eqdeck_codec.parse_add_online_deck(body)
        if parsed and dbc is not None:
            try:
                dbmod.save_deck(dbc, acct, parsed["wire_deck_id"], parsed["name"],
                                parsed["main"], parsed["avatar"], parsed["quests"])
                log(16783, n, "-> SAVED deck acct=%s '%s' (%d main kinds, avatar %s, %d quests)"
                    % (acct, parsed["name"], len(parsed["main"]), parsed["avatar"], len(parsed["quests"])))
            except Exception as e:
                log(16783, n, "-> deck save FAILED acct=%s: %s" % (acct, e))
        else:
            log(16783, n, "-> AddOnlineDeck(361) acct=%s NOT saved (parsed=%s) %dB" % (acct, bool(parsed), len(body)))
        return

    if cid == 99:                          # MatchCommand_SetReady -- route by its ready bool so a lone
        try:                               # SetReady(0) un-readies (ChangeStatus 137==4/2 is the redundant
            rdy = _parse_setready_ready(body)   # primary ready signal; the capture's readying sends both).
        except Exception:
            rdy = 1
        (_mark_ready if rdy != 0 else _unready)(conn, n, acct)
        _sync_ready_to(conn, acct)         # acct is in the dialog -> also (re)send it the opponent's ready
        return

    if cid == 137:                         # LobbyCommand_ChangeStatus = a SCREEN/PHASE indicator only
        conn.sendall(frame(body))          # (2=lobby, 4=in-match/deck-select phase, 5=launching) -- NOT the
        cur = body[-1] if body else None   # ready toggle. Ready is driven SOLELY by SetReady(99)'s bool: the
        with _clients_lock:                # 2-client capture shows SetReady 0->1 is the real ready, and the
            if cur is not None: LAST_STATUS[acct] = cur; READY[acct] = cur   # client sends status=4 just on
        # DO NOT derive ready from status: status=4 fires on ENTERING the phase, before the user clicks ready,
        # so using it falsely auto-readied the opponent on the other client's view. (Re #ready-bug.)
        _sync_ready_to(conn, acct)         # acct IS in the dialog now -> resync the opponent's ACTUAL ready to
                                           # it (covers the creator's LOCAL room re-entry, which sends no Join).
        # NB: never auto-reset to casual on status churn (the old prev-in-(5,0xb) heuristic kicked the creator
        # out of its just-created match). Teardown is handled only on explicit Leave(93)/disconnect.
        return

    if cid == 291:                         # LobbyCommand_ChangeLobbyDisplay = the CLIENT's change-display
        # REQUEST (RE agent a09f084a): the Casual/Tournament buttons don't navigate locally -- they send this
        # and the client navigates ONLY when the server pushes ChangeLobbyDisplay(291) back. display=5 is a
        # hardcoded special case that builds the tournament lobby screen (tournamentlobby.ui, FUN_104154d0).
        req_disp = None
        try:
            i = 0
            for _ in range(3):             # 3 env levels (classid,ver)
                _, i = dec_int(body, i); _, i = dec_int(body, i)
            _, i = dec_int(body, i)        # base int
            req_disp, i = dec_int(body, i) # requested display / category
        except Exception:
            pass
        log(16783, n, "RECV ChangeLobbyDisplay(291) REQUEST acct=%s display=%s" % (acct, req_disp))
        # display=5 now renders (the gid=5 key fix); still gated behind ANSWER_TOURNAMENT_NAV so the answer
        # only fires when explicitly enabled. Casual (display=2 -> 100) is ungated (always safe).
        if ANSWER_TOURNAMENT_NAV and req_disp == 5:
            # answer the Tournaments button. ORDER MATTERS: the tournament STATE (SetTournament + the
            # container-allocating UpdateTournament) must reach the client BEFORE the ChangeLobbyDisplay that
            # triggers the screen build, else the model walks a null pairings list -> crash (workflow w6cvp1oil).
            try:
                for t in dbmod.list_tournaments(dbc, states=("open", "locked", "running")):
                    conn.sendall(build_settournament(t["id"], round=t.get("round", 0), timer=0, group_id=TOURNEY_LOBBYTYPE))
                    conn.sendall(build_updatetournament(TOURNEY_LOBBYTYPE, t["id"], pairings=(),
                                                        round=t.get("round", 0), substate=0))
            except Exception:
                pass
            time.sleep(0.05)
            conn.sendall(build_changelobbydisplay(5))
            log(16783, n, "-> answered Tournaments button: SetTournament(299)+UpdateTournament(293) then ChangeLobbyDisplay(291,5)")
        elif req_disp == 2:                # Casual button -> back to the casual category (gid 100)
            conn.sendall(build_changelobbydisplay(CASUAL_CAT))
            log(16783, n, "-> answered Casual button: ChangeLobbyDisplay(291, display=%d)" % CASUAL_CAT)
        return

    if cid == 415:                         # ★ SCENARIO COMPLETION report (AccountCommand_SetCampaignStatus).
        # Carries (nodeId 0x157xx, archetypeId 0x1388x, difficulty 1/2/3). We store it STRUCTURED and rebuild
        # account property 0x1054 in the 297 login reply -- the only thing that actually persists the campaign
        # tree (a replayed 415 is a client-side no-op: its apply handler is `return 1`). See _parse_415.
        if dbc is not None:
            try:
                node, arch, diff = _parse_415(body)
                dbmod.save_campaign_frame(dbc, acct, 415, "node_%s" % node, body)   # keep raw for debugging
                if node is not None and arch is not None:
                    dbmod.record_scenario_completion(dbc, acct, node, arch, diff)
                    sname = LAST_SCENARIO_STR.get(acct)                             # learn string<->node pairing
                    if sname:
                        dbmod.learn_scenario_node(dbc, sname, node, arch)
                    log(16783, n, "-> SAVED completion acct=%s node=0x%x arch=0x%x diff=%s name=%s"
                        % (acct, node, arch, diff, sname))
                else:
                    log(16783, n, "-> 415 acct=%s UNPARSED (node=%s arch=%s) raw=%s"
                        % (acct, node, arch, hexdump(body)))
            except Exception as e:
                log(16783, n, "-> 415 completion save FAILED acct=%s: %s" % (acct, e))
        return

    if cid == 487:                         # scenario report -- carries the scenario id STRING. Remember it so the
        # next 415 can be paired to its nodeId (learns scenario_nodemap), and archive the raw frame.
        if dbc is not None:
            try:
                key = _campaign_key(body)
                LAST_SCENARIO_STR[acct] = key
                dbmod.save_campaign_frame(dbc, acct, 487, key, body)
                log(16783, n, "-> scenario report acct=%s name=%s (remembered for nodemap)" % (acct, key))
            except Exception as e:
                log(16783, n, "-> 487 save FAILED acct=%s: %s" % (acct, e))
        return

    # Everything else (buddies/ignore/tournament/etc.): DROP. Never relay raw client state to the peer.
    return

def build_join(account, group, name="", field3=0):
    """LobbyCommand_Join(92) v4, server->client. Validated format (no 12-byte client header).
    account==local(1) -> the client SELF-JOINS the group (enters it); else a remote player joins."""
    present_ps = enc_int(0) + enc_int(23) + enc_int(1) + enc_int(0)  # PRESENT empty PropertySet
    env_join = (enc_int(92) + enc_int(4)) + (enc_int(92) + enc_int(1)) + (enc_int(92) + enc_int(1))
    return frame(env_join
        + enc_int(account) + enc_int(group) + enc_int(field3)   # v1
        + enc_str(name)                                          # v2 name
        + enc_int(0)                                             # v3 vector<int> (empty)
        + enc_str("") + present_ps + enc_int(0))                 # v4 str, PRESENT PropertySet, int

# --- Casual lobby Create/Join + re-entry support (RE agent aeada0e) ----------
# Navigator->Casual Games is a LOCAL screen switch: it reads ClientApplication+0xAC (the "current group")
# and the group's type at +0x24 (1=room-list lobby, 6/7/8=match). Create = LobbyCommand_Join(92) self-join
# to a client-INVENTED group id; the client's Join run looks that gid up in ITS lobby model and does nothing
# ("Join: couldn't get group") unless the server first materializes the group (AddGroups) and echoes the Join.
# Leave(93) never nulls +0xAC, so after a match it points at a now-dead match group -> re-entering Casual is
# dead until the server re-asserts ChangeLobbyDisplay(100) to pop +0xAC back to the type-1 lobby.
_present_propset = enc_int(0) + enc_int(23) + enc_int(1) + enc_int(0)   # PRESENT empty PropertySet(23)
CASUAL_CAT = 100               # the type-1 lobby category created at login (AddGroups gids 100/101)
KNOWN_GROUPS = {100, 101}      # group ids already in the client's model (server registry)
IN_MATCH = {}                  # acct -> match group id it created/joined
LAST_STATUS = {}              # acct -> last ChangeStatus(137) status byte (to detect leaving the match)

# === MATCH CONTROLLER STATE + BUILDERS (the 2-player pairing authority) ===
# Redesign (M-B part 2): SPLIT, server-bridged gid namespaces (RE of FUN_10145290 Join +
# FUN_10153800 findGroup + the real 2-client capture srv_005847). The retail client keeps a
# purely client-side group registry populated ONLY by the AddGroups(94) frames IT received; a
# Join(92){group=N} resolves N in the SENDER's own registry, and a SELF-join to an unknown gid is
# FATAL (FUN_10097ed0) while a REMOTE-join to an unknown gid is a non-fatal warning (FUN_10097e90 =
# "only sees itself"). So every gid we ever put in a frame to a client MUST be one that client has
# already AddGroups'd. The creator only ever knows its match under the gid IT invented (gid=0 in the
# capture); every OTHER client only ever knows it under the unique server `mid` we advertised. We
# store BOTH ids on the record and translate per recipient with gid_for(). Routing is by account_id
# (CONN_MATCH), never by the client's gid.
MATCHES = {}            # mid -> dict(owner, mid, owner_gid, name, members=[acct], ready=set(),
                        #             launched, state in {'open','full','ready','launched'}, decks)
CONN_MATCH = {}         # acct -> mid the account is currently in
TOURNEY_GROUP_SENT = set()  # accts we've pushed the tournament-lobby group (gid=5) to this session -- DEFERRED
                        # off casual logins (the type-5 group + SetTournament manager state break the casual
                        # Create dialog), pushed only at a tournaments login or on the Tournaments-tab click.
_next_match = 6000      # server-assigned UNIQUE mids, high range no client invents (capture: client uses gid=0)
GAME_ID = 200           # client-side Game id LaunchGame creates / SendSerializedGame(262) reconstructs

# --- controller config (all default to the SAFE, lobby-stable behaviour) ---------------------------
# Board entry is GATED OFF by default: a static SendSerializedGame(262) cannot render the board (the
# board is event-driven; the renderable EQGame class crashes the base-166 deserializer -- documented
# wall). On both-ready the controller therefore LANDS in a stable both-ready state and does NOT enter
# the board, unless explicitly opted in. Re-enable when the board render is solved.
LAUNCH_BOARD = os.environ.get("SWGTCG_LAUNCH_BOARD", "0") != "0"   # master gate (default OFF)
LAUNCH_MODE  = os.environ.get("SWGTCG_LAUNCH_MODE", "ready")        # ready | launch_only | full
# RENDER PATH (2026-07-02): launch the CLIENT-VISIBLE match via the proven render sequence so the client
# navigates INTO the game screen with a render-capable game attached: game-group AddGroups(contain=gid=game
# id) [-> setGame writes screen+0x28] + EQMatchCommand_LaunchGame(80008) [-> renderable EQGame] + LaunchGame
# (116) [-> build+show the game screen] + SendSerializedGame(262, EQ blob). Default OFF.
LAUNCH_EQ   = os.environ.get("SWGTCG_LAUNCH_EQ", "0") != "0"
# ★ SERVER-DRIVEN BOARD (2026-07-03): instead of the static mid-game capture, send a PRE-START EQ game
# (build_eq_prestart_blob: GameStarted=0, GameIsSetup=0, ReadyForStart=1, empty SM stack, 2 players + full
# 60-card draw decks) then SetupGame(67) -> the CLIENT'S OWN advanceTurn runs EQStartOfGameState -> deals ->
# board burst, no cdb force. Requires LAUNCH_EQ. Live-sweep knobs: SWGTCG_PRESTART_DECK (deck size),
# SWGTCG_EQ_INNERCID (inner base classid). See gameserialize.build_eq_prestart_blob.
PRESTART = os.environ.get("SWGTCG_PRESTART", "0") != "0"
PRESTART_DECK = int(os.environ.get("SWGTCG_PRESTART_DECK", "50"))
EQ_INNERCID = int(os.environ.get("SWGTCG_EQ_INNERCID", "80003"))
# ★ OPTION-2 MIRRORED-ENGINE (2026-07-05, workflow wf_685c3e53): NO serialized 262 blob. Each client CREATES a
# fresh EQGame (80008) and its OWN engine materializes + deals. Sequence per client: addgroups -> 80008 -> 116 ->
# SetupGame(67) -> SelectDeckForPlayer(58) x2 carrying a PRESENT EQDeck(80002) -> ReadyForStartOfGame(117) x2.
# 58 execute FUN_509f30 -> Game vt+0x34 FUN_62c5e0 -> FUN_4f4360 -> (game+0x139==0) EQPlayer::setupDeck FUN_644460
# = the REAL materializer (operator_new(0x168) Card shells -> getDrawDeck FUN_57dbc0, emits 0x67/0x73). Both 117s
# relayed to BOTH clients release the 2-seat deal gate FUN_65bf20. This breaks the "empty piles" CONVERGED WALL:
# the engine builds the pile from the delivered deck instead of a static blob that never materializes. Requires
# LAUNCH_BOARD. Deck ids MUST be client-known (sourced from the DB, same as the 363 push).
DECK58 = os.environ.get("SWGTCG_DECK58", "0") != "0"
# SOLO test: a single client's ready triggers the launch (synthetic opponent) so a real board can be reached
# without a 2nd GUI client. Default OFF (normal 2-player both-ready).
SOLO_LAUNCH = os.environ.get("SWGTCG_SOLO_LAUNCH", "0") != "0"
PERSIST_LAST_DECK = os.environ.get("SWGTCG_PERSIST_LAST_DECK", "1") != "0"
# Home-screen content pushes (RE agent ad71061b; no on-wire captures exist for 309/458 so both are
# risk-managed): MOTD(309) is pushed ONLY when an admin has set one (empty -> no push -> login flow
# byte-identical to today). Leaderboard(458) is OFF by default (unproven at login; the client may only
# want it on-demand) -- flip SWGTCG_PUSH_LEADERBOARD=1 for a live test.
PUSH_MOTD = os.environ.get("SWGTCG_PUSH_MOTD", "1") != "0"
PUSH_LEADERBOARD = os.environ.get("SWGTCG_PUSH_LEADERBOARD", "0") != "0"
# Tournament lobby (RE agent ad4dbec9): the Tournaments button is a local screen-switch backed by the
# lobby model, so it only lists rooms the server pushed. Casual works because we push AddGroups(94)+
# ChangeLobbyDisplay(291); tournaments need the same with a tournament eLobbyTypeID + SetTournament(299).
# The exact eLobbyTypeID that files a group under the "Tournaments" tab is UNCONFIRMED (candidate 5 from
# SWLobbyTranslator) -- OFF by default, sweep SWGTCG_TOURNEY_LOBBYTYPE live.
PUSH_TOURNAMENTS = os.environ.get("SWGTCG_PUSH_TOURNAMENTS", "0") != "0"
TOURNEY_LOBBYTYPE = int(os.environ.get("SWGTCG_TOURNEY_LOBBYTYPE", "5"))   # candidate; confirm live
# Which lobby screen the client lands on at login: "casual" (display=100, default) or "tournaments"
# (display=5 -> the tournament lobby screen, RE agent a09f084a). The Casual/Tournament BUTTONS send a
# ChangeLobbyDisplay(291) request the server answers (dispatch cid==291); this flag is the login default +
# a guaranteed proof the tournament screen renders without depending on the button gate.
LOGIN_SCREEN = os.environ.get("SWGTCG_LOGIN_SCREEN", "casual")
# EXPERIMENTAL + currently UNSAFE: answering a Tournaments-button request with ChangeLobbyDisplay(291,
# display=5) CRASHES the client -- the tournament lobby screen build needs data we don't yet supply (RE in
# progress). OFF by default; SWGTCG_LOGIN_SCREEN=tournaments has the SAME crash (both build display=5).
ANSWER_TOURNAMENT_NAV = os.environ.get("SWGTCG_ANSWER_TOURNAMENT_NAV", "0") != "0"
# How to advertise a creator's match to OTHER clients. The creator's own AddGroups(94) embeds its FULL
# selected deck (~825 B in the capture). 'raw' (DEFAULT) forwards the creator's PROVEN Match(104) bytes with
# only the gid spliced -- this is the approach the prior 2-player handshake confirmed working (commits
# 3f79732/89515d2). 'strip_deck' additionally NULLs the embedded deck (lower bytes, but the room may not
# display); 'clean' builds a minimal Match(104) from scratch. build_introduce(owner) is sent BEFORE the
# advertise in all modes so the peer's account lookup resolves. Validate live with launcher --pair.
ADVERTISE_MODE = os.environ.get("SWGTCG_ADVERTISE_MODE", "raw")  # raw | strip_deck | clean
SEND_REMOVEGROUPS = os.environ.get("SWGTCG_REMOVEGROUPS", "1") != "0"   # un-advertise rooms on teardown
# Join field3/role the client uses for itself (capture: BOTH players send field3=2). The room "Cur #" player
# count reads THIS role bucket, so all server roster replays must seat members with this role or the count
# (and the opponent in the roster) stays at 1.
PLAYER_ROLE = int(os.environ.get("SWGTCG_PLAYER_ROLE", "2"))
# ISSUE 1 (auto-open the ready-up on CREATE) has NO safe server fix and is NOT attempted: live testing proved the
# only screen-switch command (ChangeLobbyDisplay/event 0x46) BOUNCES the creator out of its own match for ANY gid
# (gid 0 -> root sentinel; a real mid -> still Leave+teardown), and set-current-group (FUN_10068d20) doesn't switch
# the screen. The ready-up auto-opens for a JOINER only because clicking a room is a client-side navigation the
# create button doesn't perform. So the creator manually clicks into its room once; everything else works.

def gid_for(acct, m):
    """Translate a match's gid into the namespace of recipient `acct`. Each client only ever knows the match
    under the gid the SERVER advertised it to that client under (stored in m['gids']): the creator under its
    own invented owner_gid, a list-joiner under the mid, a quick-joiner under the gid IT invented. Anyone the
    server hasn't advertised to (a not-yet-joined lobby peer) defaults to the mid. THIS prevents 'couldn't get
    group' -- we only ever hand a client a gid it previously AddGroups'd."""
    g = m.get("gids", {}).get(acct)
    return g if g is not None else m["mid"]

def _alloc_match_locked(owner, name, owner_gid=0):
    """Create (or reuse) the owner's OPEN match. CALLER MUST HOLD _clients_lock (the create path keeps the
    insert + recipient capture + advert store in one critical section). owner_gid = the gid the creator's
    client invented (parsed from its advertise; 0 in the capture), so the raw-body echo and gid_for() agree.
    Returns the unique server mid."""
    global _next_match
    mid = CONN_MATCH.get(owner)
    m = MATCHES.get(mid)
    if m is None or m.get("launched") or m.get("state") not in ("open", "full"):
        _next_match += 1; mid = _next_match
        MATCHES[mid] = {"owner": owner, "mid": mid, "owner_gid": owner_gid,
                        "name": name or ("Match %s" % owner), "members": [owner],
                        "ready": set(), "launched": False, "state": "open", "decks": {},
                        "gids": {owner: owner_gid}}        # acct -> the gid that acct knows this match under
        CONN_MATCH[owner] = mid
    else:
        if name: m["name"] = name
        m["owner_gid"] = owner_gid; m["gids"][owner] = owner_gid
    return mid

def _find_open_match_locked(exclude_acct):
    """CALLER MUST HOLD _clients_lock. Oldest open match (mid is monotonic) with a free seat, not owned/
    occupied by exclude_acct -- the target for a Quick-Join (the joiner invented its own gid, so it can't
    name a specific room). Returns a mid or None."""
    for mid in sorted(MATCHES):
        m = MATCHES[mid]
        if (m["owner"] != exclude_acct and exclude_acct not in m["members"]
                and not m.get("launched") and m.get("state") in ("open", "full")
                and len(m["members"]) < 2):
            return mid
    return None

def _walk_advertise(body):
    """Walk a client AddGroups(94) Match advertise fully (validated byte-for-byte vs the real create
    capture srv_005847). Returns a dict with the parsed name/gid plus the splice offsets so the caller
    can both remap the gid AND strip the embedded deck. Wire: env(94)x3, baseInt, count=1, Match(104,3)x2
    begins, 5 propsets, 5 role-vecs<int>, contain, gid, type, then a PRESENT embedded EQDeck sub-object.
    keys: owner, name, contain, gid, type, gid_start, gid_end (around the gid), deck_start (offset of the
    PRESENT deck flag, i.e. just after `type`)."""
    i = 0
    for _ in range(3):
        _, i = dec_int(body, i); _, i = dec_int(body, i)            # env(94,v)x3
    owner, i = dec_int(body, i); _, i = dec_int(body, i)            # baseInt, group count
    _, i = dec_int(body, i); _, i = dec_int(body, i)               # Match begin (104,v)
    _, i = dec_int(body, i); _, i = dec_int(body, i)               # Match base begin (104,v)
    name = ""
    for _ps in range(5):                                            # 5 PropertySets (NULL or PRESENT)
        present, i = dec_int(body, i)
        if present != 0:
            continue
        _, i = dec_int(body, i); _, i = dec_int(body, i)            # propset classid, ver
        cnt, i = dec_int(body, i)
        for _ in range(cnt):
            attr, i = dec_int(body, i)
            _, i = dec_int(body, i); _, i = dec_int(body, i)        # ValueData classid, ver
            mtype, i = dec_int(body, i); _, i = dec_int(body, i)    # mTypeID, ownRef
            if mtype == 3:
                s, i = dec_str(body, i)
                if attr == 0xfa6: name = s
            elif mtype in (1, 2):
                _, i = dec_int(body, i)
            elif mtype == 6:
                c, i = dec_int(body, i)
                for _ in range(c): _, i = dec_int(body, i)
            elif mtype == 7:
                c, i = dec_int(body, i)
                for _ in range(c): _, i = dec_str(body, i)
            else:
                raise ValueError("unknown ValueData mTypeID %d" % mtype)
    for _ in range(5):                                              # 5 role vectors vec<int>
        c, i = dec_int(body, i)
        for _ in range(c): _, i = dec_int(body, i)
    contain, i = dec_int(body, i)
    gid_start = i
    gid, i = dec_int(body, i)
    gid_end = i
    typ, i = dec_int(body, i)                                       # type field (2 in the capture)
    deck_start = i                                                  # offset of the PRESENT embedded deck
    return {"owner": owner, "name": name, "contain": contain, "gid": gid, "type": typ,
            "gid_start": gid_start, "gid_end": gid_end, "deck_start": deck_start}

def build_introduce(acct, name="player"):
    """AccountCommand_IntroduceAccount(114) for a REMOTE account so the receiver's lobby model
    knows it -> the Join/AddGroups account lookup (FUN_10018d70) resolves (no soft-assert)."""
    return frame(_env(114, [1, 1]) + enc_int(acct) + enc_str(name) + _present_propset + enc_int(acct))

NULL_SUBOBJ = enc_int(1)            # sub-object NULL marker (reader FUN_1000b460: leading int != 0 => NULL)

def _vd(mtype, payload):            # ValueData(classid22, ver4, mTypeID, ownRef1, payload) -- client wire shape
    return enc_int(22) + enc_int(4) + enc_int(mtype) + enc_int(1) + payload

def _value_string(s):               # mTypeID 3 = string
    return _vd(3, enc_str(s))

def build_match_advertise_clean(gid, name, contain=CASUAL_CAT, typ=2):
    """A minimal from-scratch joinable Match(104) advertise (ADVERTISE_MODE='clean' or parse-failure
    fallback). Mirrors the real create capture's propset0 attribute set (name 0xfa6 + the structural
    ints) with classid 104 written TWICE (Match depth 2, ver 3) so the FUN_10153870 RTTI join-gate
    passes, and a NULL embedded deck (no foreign deck bytes). NOTE: 'strip_deck' (default) is preferred
    -- it reuses the creator's byte-exact propsets; this from-scratch variant is the diagnostic fallback."""
    attrs = [
        (0x273, _vd(2, enc_int(0))),
        (0xfa5, _vd(6, enc_int(1) + enc_int(3))),     # intlist [3]
        (0xfa6, _vd(3, enc_str(name))),               # match name
        (0xfa7, _vd(2, enc_int(1))),
        (0xfa8, _vd(2, enc_int(2))),                  # max players = 2
        (0xfa9, _vd(2, enc_int(1))),
        (0xfaa, _vd(2, enc_int(1))),
        (0xfac, _vd(1, enc_int(1))),
        (0xfad, _vd(3, enc_str(""))),
        (0xfae, _vd(1, enc_int(0))),
        (0xfb0, _vd(3, enc_str(""))),                 # deck id (empty for an advertise)
        (0xfb1, _vd(1, enc_int(0))),
        (0x1067, _vd(1, enc_int(0))),
    ]
    ps0 = (enc_int(0) + enc_int(23) + enc_int(1) + enc_int(len(attrs))
           + b"".join(enc_int(a) + v for a, v in attrs))
    match = (enc_int(104) + enc_int(3) + enc_int(104) + enc_int(3)     # Match begin x2 (ver 3)
             + ps0 + NULL_SUBOBJ * 4 + enc_int(0) * 5                  # 4 NULL propsets + 5 empty role-vecs
             + enc_int(contain) + enc_int(gid) + enc_int(typ)
             + NULL_SUBOBJ)                                            # NULL embedded deck
    return frame(_env(94, [1, 1, 1]) + enc_int(0) + enc_int(1) + match)

def _peer_advertise(mid, name, body, adv):
    """Build the AddGroups(94) advertise of a creator's match for OTHER clients, at the unique mid.
    Default 'strip_deck' = the creator's PROVEN Match bytes with gid->mid and the embedded PRESENT deck
    replaced by a NULL marker (lowest risk: only gid + deck differ from what the client itself serialized).
    'raw' keeps the full body (foreign deck) for bisecting; 'clean' builds from scratch."""
    if adv is not None and ADVERTISE_MODE == "raw":
        return frame(body[:adv["gid_start"]] + enc_int(mid) + body[adv["gid_end"]:])
    if adv is None or ADVERTISE_MODE == "clean":
        return build_match_advertise_clean(mid, name)
    return frame(body[:adv["gid_start"]] + enc_int(mid)
                 + body[adv["gid_end"]:adv["deck_start"]] + NULL_SUBOBJ)

def build_setready(account, matchid, ready=1):
    """MatchCommand_SetReady(99) server->client, DEPTH 4 (env(99,1)x4 + account + matchid + ready).
    Broadcast on each ready/un-ready so the OTHER player's checkbox flips (run @101621e0 fires event 0x7b).
    matchid MUST be in the recipient's namespace (gid_for) -- the run RTTI-casts it to a local Match(104)."""
    return frame(_env(99, [1, 1, 1, 1]) + enc_int(account) + enc_int(matchid) + enc_int(1 if ready else 0))

def build_removegroups(gids):
    """LobbyCommand_RemoveGroups(95) server->client: env(95,3) + baseInt + vec<int> gids. deser
    FUN_10148520 = base begins + baseInt + FUN_1000b2a0 vector<int>; run removes the rows from the model."""
    return frame(_env(95, [1, 1, 1]) + enc_int(0) + enc_int(len(gids)) + b"".join(enc_int(g) for g in gids))

def build_leave(account, gid, field3=0):
    """LobbyCommand_Leave(93) server->client, VERSION 3 (env(93,3)x3 + account + gid + field3 + empty vec<int>).
    Run FUN_101462d0: findGroup(gid); with an empty account-vector it removes the `account` field from the
    group's roster (FUN_10131950); if `account` == the recipient's LOCAL player it also CLOSES the match dialog
    (FUN_10136fc0). So: drop a peer from another's roster (account=peer, gid=recipient's namespace), or close a
    player's own match dialog (account=that player's own id, gid=its namespace). Byte format validated vs the
    client's own Leave capture (account+gid+field3+count, empty vector)."""
    return frame(_env(93, [3, 3, 3]) + enc_int(account) + enc_int(gid) + enc_int(field3) + enc_int(0))

def build_launchgame(game_id=GAME_ID):
    """MatchCommand_LaunchGame(116) server->client. DEPTH 4. Run @10160100 creates the client Game
    + fires UI event 0x11 -> WAMatchViewController builds the game screen."""
    return frame(_env(116, [1, 1, 1, 1]) + enc_int(1) + enc_int(game_id))

def build_eq_launchgame(game_id=GAME_ID, depth=4):
    """EQMatchCommand_LaunchGame(80008 = 0x13888) — the EQ variant that builds a RENDERABLE EQGame(0x408)
    client-side (handler FUN_00642020 -> new(0x408) -> FUN_0063a5e0 self-installs getClientSideInstance).
    ROUTE A (workflow wokhjv05u): send this INSTEAD of 116 -> the client's own factory makes the render-capable
    game with zero patching. Body mirrors 116 (baseInt + gameID); DEPTH may be 4 (same as MatchCommand) or 5
    (if EQMatchCommand adds a class level) -- SWGTCG_EQLAUNCH_DEPTH sweeps it."""
    return frame(_env(80008, [1] * depth) + enc_int(1) + enc_int(game_id))

def _build_2p_board(game_id, members):
    """The 2-player board blob for SendSerializedGame(262), templated on the captured REAL game
    (E:\\SWGTCG\\re\\out\\capture_game.log): a *running* board (mGameStarted=1, GameIsSetup=1,
    PlayerCount=2), GameTurn current-player set to player 1, and two PlayerPlayArea(174) roots each
    with 3 piles(173), an avatar Card(168) + a couple starter-deck cards in Hand. See
    gameserialize.build_2p_game for the field-by-field derivation + the remaining state-buffer RE item.

    NOTE: this is the OLD base-Game(166)/v1 minimal builder. The captured real wire proved the
    renderable game is EQGame **classid 80003, version 48** (see gameserialize + decode_wire_game.py),
    so 166/v1 is the WRONG class and bounces. _board_blob_and_gameid() selects the format actually sent."""
    return build_2p_game(game_id, accounts=(1, 2), deck_id=111)

# Board format for the 2-player launch. The 262 RECEIVER runs the BASE Game::deserialize FUN_100fca50,
# which asserts BASE classids at every direct-read layer (166 Game @Game.cpp:9007, 232 GameTurn
# @GameTurn.cpp:789, 233 StateMachine, ...). The captured wire is the EQ runtime game (EQ subclasses at
# every level: 80003 Game / 80004 GameTurn / 80005 EQPlayer / 80001 EQCard / 80006-80060 states+nodes),
# so it CRASHES the base reader one layer at a time.
# DECISIVE RE (agent a6445e07): the EQGame(80003) path is UNREACHABLE via 262 -- MatchCommand_LaunchGame
# (FUN_10160100) always operator_new(0x380)+base-ctor a base Game(166); 262 (FUN_0050a230) reuses THAT object
# and calls virtual base Game::deserialize (FUN_100fca50), whose begin requires classid 166. So "base" is the
# ONLY correct path; "v48"/"replay" are confirmed DEAD ENDS (kept for analysis only). The gameScreen bounce
# predicate (vt55 FUN_103f49a0) closes the board unless screen+0xed (runtime, SM-driven) OR game+0x1C4
# (mGameOver, BLOB-settable via SWGTCG_BOARD_GAMEOVER) OR game+0x121 (winner) is set.
#   "base" (DEFAULT, the FROM-SCRATCH renderable build): build_base_render_game = base Game(166) @v48 with BASE
#          classids at EVERY layer (166/232/233/261/174/173/168), 2 Players, GameTurn current-player, a separate
#          StateMachine with a current state, the real 8-zones/player skeleton, start flags set. Passes the full
#          offline base-walk (no classid mismatch) so deserialize will NOT crash. RENDER: set SWGTCG_BOARD_GAMEOVER=1
#          for the static (game-over) render shortcut that escapes the bounce; the clean playable path (screen+0xed)
#          is a follow-up. Launch gameid = GAME_ID; accounts (1/2). Sweep SWGTCG_GAME_VER / SWGTCG_STATE_CLASSID live.
#   "v1"   (legacy conservative): base-166/v1 minimal shell (fewer zones). Parses; bounces; does NOT crash.
#   "v48"  DEAD END -- captured-wire transform (embeds EQ 80004); base reader asserts. Analysis only.
#   "replay": raw EQ(80003) wire -- base reader asserts at the Game envelope. Decode-only.
BOARD_MODE = os.environ.get("SWGTCG_BOARD_MODE", "base")

def _board_blob_and_gameid(members):
    """Return (serialized_game_bytes, launch_gameid) for the 2-player launch, per BOARD_MODE.
    SAFE DEFAULT: 'base' and 'v1' are non-crashing (full base deserialize); 'v48'/'replay' crash and are
    gated behind explicit opt-in + a loud warning. Anything else (incl. typos) -> safe v1."""
    if BOARD_MODE == "base":
        try:
            return build_base_render_game(accounts=(1, 2), game_id=GAME_ID), GAME_ID
        except Exception as e:
            log(16783, 0, "-> base build failed (%s); using v1 builder" % e)
    elif BOARD_MODE == "replay":
        log(16783, 0, "-> WARNING: BOARD_MODE=replay sends EQGame(80003) which CRASHES the client (Game classID assert).")
        try:
            return build_real_game_replay(), REPLAY_GAMEID
        except Exception as e:
            log(16783, 0, "-> replay wire unavailable (%s); using v1 builder" % e)
    elif BOARD_MODE == "v48":
        log(16783, 0, "-> WARNING: BOARD_MODE=v48 is NOT READY -- CRASHES at GameTurn (assert 232!=80004). Use only for analysis.")
        try:
            return build_166_v48_from_capture(), REPLAY_GAMEID
        except Exception as e:
            log(16783, 0, "-> v48 transform unavailable (%s); using v1 builder" % e)
    return build_2p_game(GAME_ID, accounts=(1, 2), deck_id=111), GAME_ID

# --- match lifecycle helpers (all SEND outside _clients_lock; the lock is non-reentrant) ------------
def _broadcast_setready(m, acct, ready):
    """Tell BOTH members that `acct` is (un)ready -- each in its OWN gid namespace -- so the opponent's
    ready checkbox flips (the run RTTI-casts the MatchID to a local Match, and fires event 0x7b)."""
    with _clients_lock:
        targets = [(a, LOBBY.get(a), gid_for(a, m)) for a in m["members"]]
    for a, c, g in targets:
        if c is None:
            continue
        try: c.sendall(build_setready(acct, g, 1 if ready else 0))
        except Exception: pass

def _replay_ready(conn, m, recipient_gid):
    """Send `conn` the CURRENT ready state of every ready member (in the recipient's gid namespace), so a
    client (re-)entering the room sees existing ready checkboxes -- ready is otherwise only sent as future
    deltas, so a player who readied BEFORE the recipient (re)entered would never show as ready until they
    toggle again. SetReady(ready=1) is idempotent, so replaying is safe."""
    with _clients_lock:
        ready_members = list(m.get("ready", ()))
    for a in ready_members:
        try: conn.sendall(build_setready(a, recipient_gid, 1))
        except Exception: pass

def _sync_ready_to(conn, acct):
    """Resync `acct`'s view of its match's ready checkboxes. Called after EVERY match interaction acct makes
    (ready/unready/select-deck/status) -- the creator re-enters its room via a LOCAL screen switch that sends
    NO Join(92), so the server can't snapshot on re-entry; instead we resync whenever acct proves (by acting)
    that it is in the match dialog. This is what makes the opponent's ready show without a manual re-toggle."""
    with _clients_lock:
        m = MATCHES.get(CONN_MATCH.get(acct))
        g = gid_for(acct, m) if m else None
    if m is not None:
        _replay_ready(conn, m, g)

def _teardown_match(mid):
    """Destroy a match (the OWNER left/disconnected). CLOSE every member's match dialog via Leave(93) of
    their OWN account (account==local -> FUN_10136fc0 closes the dialog + returns), pop them to casual, and
    remove the room row from EVERY lobby client's browser (RemoveGroups). All gids per-recipient namespace."""
    with _clients_lock:
        m = MATCHES.pop(mid, None)
        if m is None:
            return None
        owner = m["owner"]; owner_gid = m.get("owner_gid"); members = set(m["members"])
        gids = dict(m.get("gids", {}))
        for a in list(members):
            if CONN_MATCH.get(a) == mid:
                CONN_MATCH.pop(a, None)
        targets = list(LOBBY.items())
    for a, c in targets:
        if c is None:
            continue
        g = gids.get(a, (owner_gid if a == owner else mid))
        try:
            if a in members:
                c.sendall(build_leave(a, g))                      # close this member's match dialog (self-leave)
                c.sendall(build_changelobbydisplay(CASUAL_CAT))   # and ensure it's back on the browser
            if SEND_REMOVEGROUPS:
                c.sendall(build_removegroups([g]))                # drop the now-dead room row from the browser
        except Exception: pass
    return m

def _remove_from_match(acct, leaver_conn=None):
    """Handle `acct` leaving/disconnecting from its match. OWNER leaving -> tear the whole match down (room
    gone, joiner's dialog closed). NON-OWNER leaving -> drop ONLY them (Leave(93) to the owner removes them
    from its roster; the owner STAYS in its room), re-open the room and re-advertise it as joinable again."""
    with _clients_lock:
        mid = CONN_MATCH.get(acct)
        m = MATCHES.get(mid)
        is_owner = bool(m and m["owner"] == acct)
    if m is None:
        if leaver_conn is not None:
            try: leaver_conn.sendall(build_changelobbydisplay(CASUAL_CAT))
            except Exception: pass
        return "none"
    if is_owner:
        _teardown_match(mid)
        return "owner"
    # non-owner left: remove ONLY acct; keep the owner's room alive + re-open + re-advertise
    with _clients_lock:
        m = MATCHES.get(mid)
        if m is None:
            return "none"
        if acct in m["members"]: m["members"].remove(acct)
        m["ready"].discard(acct); m["gids"].pop(acct, None); CONN_MATCH.pop(acct, None)
        m["state"] = "open" if len(m["members"]) < 2 else "full"
        owner = m["owner"]; owner_conn = LOBBY.get(owner); owner_gid = gid_for(owner, m)
        adv_body = m.get("adv_body"); adv_meta = m.get("adv_meta"); mname = m.get("name", "")
        nonmembers = [(a, cc) for a, cc in LOBBY.items() if a not in m["members"]]
    if owner_conn is not None:                                    # drop the leaver from the owner's roster
        try: owner_conn.sendall(build_leave(acct, owner_gid))     # (owner stays in its room, now re-open)
        except Exception: pass
    adv_frame = _peer_advertise(mid, mname or ("Match %s" % owner), adv_body, adv_meta)
    oname = NAMES.get(owner, "player%s" % owner)
    for a, cc in nonmembers:                                      # re-advertise the now-open room to others
        if cc is None or a == owner:
            continue
        try:
            cc.sendall(build_introduce(owner, oname))
            cc.sendall(adv_frame)                                 # (viewer-roster Join reverted -- see cid==94)
        except Exception: pass
    if leaver_conn is not None:                                   # the leaver: ensure it's back on the browser
        try: leaver_conn.sendall(build_changelobbydisplay(CASUAL_CAT))
        except Exception: pass
    return "member"

def _unadvertise_to_nonmembers(mid):
    """When a room fills, remove its row from clients who did NOT join so a 3rd can't double-join it."""
    if not SEND_REMOVEGROUPS:
        return
    with _clients_lock:
        m = MATCHES.get(mid)
        if m is None:
            return
        members = set(m["members"])
        targets = [(a, c) for a, c in LOBBY.items() if a not in members]
    for a, c in targets:
        try: c.sendall(build_removegroups([mid]))   # non-members all see it at mid
        except Exception: pass

def _evict_account(acct):
    """A client may occupy at most ONE match: if acct already owns/sits in a match, leave it (owner -> the
    match tears down; non-owner -> just drop acct, the owner's room survives)."""
    if CONN_MATCH.get(acct) in MATCHES:
        _remove_from_match(acct)

def _parse_setready_ready(body):
    """The trailing ready bool of a MatchCommand_SetReady(99): env(99)x4 + account + matchid + ready."""
    i = 0
    for _ in range(4):
        _, i = dec_int(body, i); _, i = dec_int(body, i)
    _, i = dec_int(body, i)               # account
    _, i = dec_int(body, i)               # matchid
    rdy, _ = dec_int(body, i)             # ready
    return rdy

def _leave_match(conn, n, acct):
    READY.pop(acct, None)
    mid = CONN_MATCH.get(acct)
    kind = _remove_from_match(acct, leaver_conn=conn)
    if kind == "owner":
        log(16783, n, "-> LEAVE acct=%s (owner) -> tore down mid=%s; joiner kicked to browser" % (acct, mid))
    elif kind == "member":
        log(16783, n, "-> LEAVE acct=%s (joiner) -> dropped from mid=%s; room re-opened + re-advertised" % (acct, mid))
    else:
        log(16783, n, "-> LEAVE acct=%s (no tracked match) -> reset to casual" % acct)

def _unready(conn, n, acct):
    with _clients_lock:
        mid = CONN_MATCH.get(acct)
        m = MATCHES.get(mid)
        if m is None or acct not in m.get("ready", ()):
            return
        m["ready"].discard(acct)
        if not m.get("launched") and m.get("state") == "ready":
            m["state"] = "full" if len(m["members"]) >= 2 else "open"
    _broadcast_setready(m, acct, False)
    log(16783, n, "-> UNREADY acct=%s mid=%s" % (acct, mid))

def _persist_last_deck(dbc, acct, body, n):
    """Persist accounts.last_deck_id from a MatchCommand_SelectDeck(101). The body embeds the selected
    deck's wire id (e.g. 'deck_1'); match it (as enc_str chars+null) against the account's DB decks.
    Byte-scan is robust to the exact SelectDeck field layout; any miss/parse-error just logs."""
    try:
        for d in dbmod.load_decks(dbc, acct):
            for key in (d["wire_deck_id"], d["name"]):   # the picker/SelectDeck now echoes the NAME as the id
                if key and (key.encode("utf-8") + b"\x00") in body:
                    dbmod.set_last_deck(dbc, acct, d["id"])
                    log(16783, n, "-> last_deck acct=%s -> deck %s (%r)" % (acct, d["id"], key))
                    return
        log(16783, n, "-> last_deck acct=%s: no DB deck id/name matched the SelectDeck body" % acct)
    except Exception as e:
        log(16783, n, "-> last_deck persist failed acct=%s: %s" % (acct, e))

def _mark_ready(conn, n, acct):
    """Mark acct ready, broadcast it to the opponent, and on BOTH-ready take the gated action.
    Guards (bug 2): ignore if the match is missing, acct isn't a member, or it already launched."""
    with _clients_lock:
        mid = CONN_MATCH.get(acct)
        m = MATCHES.get(mid)
        if m is None or acct not in m["members"] or m.get("launched"):
            return
        m["ready"].add(acct)
        members = list(m["members"]); nready = len(m["ready"])
        both = len(members) >= 2 and all(a in m["ready"] for a in members)
        if SOLO_LAUNCH and len(members) >= 1 and all(a in m["ready"] for a in members):
            both = True                         # single-client render test: this client's ready == go
    _broadcast_setready(m, acct, True)
    log(16783, n, "-> READY acct=%s mid=%s ready=%s/%s" % (acct, mid, nready, len(members)))
    if not both:
        return
    with _clients_lock:
        if m.get("launched") or m.get("state") == "ready":
            return                                  # another thread / re-fire already handled it
        m["state"] = "ready"
    if not LAUNCH_BOARD or (LAUNCH_MODE == "ready" and not LAUNCH_EQ and not DECK58):
        log(16783, n, "-> BOTH READY mid=%s members=%s -- board GATED OFF (LAUNCH_BOARD=%s MODE=%s); stable hold" % (mid, members, LAUNCH_BOARD, LAUNCH_MODE))
        return
    _launch_board(n, mid)

def _eqdeck_for_account(acct):
    """Load `acct`'s primary deck from the DB and build its PRESENT EQDeck(80002) sub-object bytes for a
    SelectDeckForPlayer(58). Returns (deck_name, eqdeck_bytes) or (None, None). Prefers the account's
    last-selected deck, else a starter, else the first. EQDeck(80002) is the registered class (base Deck 46
    crashes 'Couldn't get class 46'); ids come from the DB so the client already knows them."""
    try:
        dbc = dbmod.connect()
        try:
            decks = dbmod.load_decks(dbc, acct)
            if not decks:
                return None, None
            last = None
            try:
                row = dbc.execute("SELECT last_deck_id FROM accounts WHERE id=?", (acct,)).fetchone()
                last = row[0] if row else None
            except Exception:
                last = None
            pick = None
            if last is not None:
                pick = next((d for d in decks if last in (d["id"], d["wire_deck_id"], d["name"])), None)
            if pick is None:
                pick = next((d for d in decks if d.get("is_starter")), decks[0])
            avsub = load_avatar_subobj(pick["avatar"]) if pick["avatar"] else b""
            blob = build_eqdeck_subobject(pick["name"], pick["name"], pick["main"],
                                          pick["avatar"], pick["quests"], avsub)
            return pick["name"], blob
        finally:
            try: dbc.close()
            except Exception: pass
    except Exception as e:
        log(16783, 0, "-> [DECK58] deck load FAILED acct=%s: %s" % (acct, e))
        return None, None


def _deck58_launch(n, mid, members, conns):
    """OPTION-2 mirrored-engine launch (SWGTCG_DECK58): NO 262 blob. Each client creates a fresh EQGame(80008)
    and its own engine materializes + deals from decks delivered via SelectDeckForPlayer(58)+EQDeck(80002).
    Both clients' 117s are relayed to BOTH so the 2-seat deal gate FUN_65bf20 releases. Sequence per client
    (workflow wf_685c3e53 synthesis): addgroups -> 80008 -> 116 -> [65x2] -> 67 -> 58x2(EQDeck) -> 117x2.
    Game player ids 1,2 = internal seats; members[0]->seat 1, members[1]->seat 2 for deck data."""
    # gid != 1: REPLAY_GAMEID=1 hits the group-1 special-case (FUN_831ae0 if(gid==1) switchScreen(2)=lobby)
    # -> bounce to menu. A fresh (no-capture) game can use any gid, so default off 1. SWGTCG_DECK58_GID sweeps it.
    launch_gid = int(os.environ.get("SWGTCG_DECK58_GID", "200"))
    depth = int(os.environ.get("SWGTCG_EQLAUNCH_DEPTH", "5"))
    seat_acct = {1: (members[0] if len(members) > 0 else None),
                 2: (members[1] if len(members) > 1 else None)}
    # build each seat's EQDeck once (identical bytes delivered to both clients -> mirrored materialization)
    pdeck = {}
    for pid, acct in seat_acct.items():
        if acct is None: continue
        name, blob = _eqdeck_for_account(acct)
        if blob is None:
            log(16783, n, "-> [DECK58] no deck for seat %d (acct=%s) -- ABORT (deal needs both decks)" % (pid, acct))
            return
        pdeck[pid] = (name, blob)
    send117 = os.environ.get("SWGTCG_DECK58_117", "1") != "0"
    setplayer_first = os.environ.get("SWGTCG_SETPLAYER_FIRST", "1") != "0"
    # 116 placement: "late" (DEFAULT) sends 116 AFTER 67+58 so setGame (FUN_7f4240) binds a POPULATED game
    # (screen+0x28) + the board-build FUN_7f3f70 can fire -- mirrors the working 262 path (state before 116).
    # "early" = 116 right after 80008 (old order; left blank-starscape). SWGTCG_DECK58_116 sweeps it.
    late116 = os.environ.get("SWGTCG_DECK58_116", "late") != "early"
    for a, c in conns:
        if not c: continue
        try:
            _mypid = 1 if a == seat_acct.get(1) else (2 if a == seat_acct.get(2) else 1)  # perspective seat
            c.sendall(build_addgroups([(launch_gid, launch_gid, 6)]))   # 1. game-group (setGame resolve key)
            c.sendall(build_eq_launchgame(launch_gid, depth=depth))     # 2. 80008 fresh renderable EQGame
            time.sleep(0.2)
            # OPPONENT-SEAT blocker 3 (NAME): register both seat accounts (keys 1,2 = the ids 67 seats with) in the
            # process account-name registry BEFORE 67 -- EQGame::setup bakes each seat label @0x10230ebb via
            # getName/FUN_100cefc0, which misses -> "Unknown Account"/"Player %d" without this. Both clients register
            # both names so each shows the real opponent label. Names resolved from the joined members.
            _nm1 = NAMES.get(seat_acct.get(1), "Player 1")
            _nm2 = NAMES.get(seat_acct.get(2), "Player 2")
            c.sendall(build_introduce(1, _nm1))
            c.sendall(build_introduce(2, _nm2))
            time.sleep(0.05)
            if not late116:
                c.sendall(build_launchgame(launch_gid)); time.sleep(0.15)   # (early) 116 before setup
            if setplayer_first:
                c.sendall(build_gamecommand_setplayer(player_id=1, game_id=launch_gid))    # [65x2] fill slots
                c.sendall(build_gamecommand_setplayer(player_id=2, game_id=launch_gid))
                time.sleep(0.1)
            def _send_58s():                                           # deliver both seats' decks -> materialize
                for pid in (1, 2):
                    if pid not in pdeck: continue
                    name, blob = pdeck[pid]
                    c.sendall(build_gamecommand_selectdeckforplayer(player_id=pid, game_id=launch_gid,
                        deck_id=name, deck=blob, version=1))
                    time.sleep(0.15)
            # ★ 58 BEFORE 67 (default): 67 kicks advanceTurn->StartOfGame and, with the forced deal gate, the deal
            # runs IMMEDIATELY -- so the decks (58) must arrive FIRST or the deal iterates empty piles -> AV
            # @+0x25F25E (DLL live: 58 arrived=0, deal crashed). SWGTCG_DECK58_58_FIRST=0 reverts to 58-after-67.
            _58_first = os.environ.get("SWGTCG_DECK58_58_FIRST", "1") != "0"
            if _58_first: _send_58s()
            c.sendall(build_gamecommand_setupgame(game_id=launch_gid, match_id=launch_gid,   # 3. 67 seat + kick advanceTurn
                player_count=2, account_ids=(1, 2), player_order=(1, 2), my_player_id=_mypid))
            time.sleep(0.2)
            if not _58_first: _send_58s()
            if late116:
                time.sleep(0.1)
                c.sendall(build_launchgame(launch_gid))                 # 6. (late, DEFAULT) 116 shows screen on a POPULATED game
                time.sleep(0.15)
            if send117:                                                # 7-8. 117 both seats, relayed to THIS client too
                time.sleep(0.2)
                for pid in (1, 2):
                    c.sendall(build_gamecommand_readyforstartofgame(player_account=pid, game_id=launch_gid))
            log(16783, n, "-> [DECK58] acct=%s mypid=%d: addgroups+80008+116+67+58x2(EQDeck)+117x2 gid=%d decks=%s"
                % (a, _mypid, launch_gid, {p: d[0] for p, d in pdeck.items()}))
        except Exception as e:
            log(16783, n, "-> [DECK58] send FAILED acct=%s: %s" % (a, e))
    # ★ SM PUMP: the StartOfGame updateState (FUN_65cc70) only ticks when a command arrives (each triggers
    # processEvents). After the launch burst it idles at substate 4 (case 4 created the child draw machine but
    # the SM never re-ticks -> case 5/6/7 + the draw never run). Resend 117x2 repeatedly to keep ticking the SM
    # through the deal. SWGTCG_DECK58_PUMP (default on), _PUMP_N iterations, _PUMP_IV interval.
    if os.environ.get("SWGTCG_DECK58_PUMP", "1") != "0":
        _rep = int(os.environ.get("SWGTCG_DECK58_PUMP_N", "40"))
        _iv = float(os.environ.get("SWGTCG_DECK58_PUMP_IV", "0.25"))
        def _pump(_conns=conns, _gid=launch_gid, _rep=_rep, _iv=_iv):
            for _ in range(_rep):
                for _a, _c in _conns:
                    if not _c: continue
                    try:
                        for _pid in (1, 2):
                            _c.sendall(build_gamecommand_readyforstartofgame(player_account=_pid, game_id=_gid))
                    except Exception:
                        return
                time.sleep(_iv)
        threading.Thread(target=_pump, daemon=True).start()
        log(16783, n, "-> [DECK58] SM pump started (%dx 117x2 @ %ss) to tick StartOfGame through the deal" % (_rep, _iv))
    log(16783, n, "-> LAUNCH(deck58) mid=%s members=%s gid=%d: option-2 mirrored-engine, NO 262 blob" % (mid, members, launch_gid))


def _launch_board(n, mid):
    """Enter the board on both-ready (only when SWGTCG_LAUNCH_BOARD is on). launch_only = LaunchGame(116)
    only (empty 'waiting for server' board, no 262); full = LaunchGame + SendSerializedGame(262), the
    documented board-render wall (crash risk). launched is set HERE so a ready-toggle never re-fires it."""
    with _clients_lock:
        m = MATCHES.get(mid)
        if m is None or m.get("launched"):
            return
        m["launched"] = True; m["state"] = "launched"
        members = list(m["members"]); conns = [(a, LOBBY.get(a)) for a in members]
    # ★ OPTION-2 mirrored-engine path (no 262 blob): let each client's engine materialize + deal via 58+EQDeck.
    if DECK58:
        _deck58_launch(n, mid, members, conns)
        return
    # RENDER PATH: launch into the game screen with a render-capable EQ game attached (the proven sequence).
    if LAUNCH_EQ:
        launch_gid = REPLAY_GAMEID              # the EQ capture's internal gameid (setGame key)
        try:
            if PRESTART:
                # ★ pre-start server-driven board: PATCH the proven EQ capture's flags to pre-start (keeps EQ
                # sub-object formats + real decks) -> client boots StartOfGame itself off SetupGame(67).
                # SWGTCG_PRESTART_SCRATCH=1 uses the from-scratch base body instead (format-incompatible; kept
                # for RE reference -- it hits eqgameturn.cpp:461).
                if os.environ.get("SWGTCG_PRESTART_SCRATCH", "0") != "0":
                    eq_blob = build_eq_prestart_blob(accounts=(1, 2), game_id=launch_gid,
                                                     deck_cards=PRESTART_DECK, inner_classid=EQ_INNERCID)
                else:
                    eq_blob = build_eq_prestart_capture(ai=int(os.environ.get("SWGTCG_PRESTART_AI", "1")))
            else:
                eq_blob = build_eq_80003_blob()  # static mid-game capture (renders; needs cdb card inject)
        except Exception as e:
            log(16783, n, "-> LAUNCH(eq) blob build FAILED: %s -- falling back to base" % e); eq_blob = None
        if eq_blob is not None:
            depth = int(os.environ.get("SWGTCG_EQLAUNCH_DEPTH", "5"))
            for a, c in conns:
                if not c: continue
                try:
                    # ORDER MATTERS (workflow nav-bounce-rootcause, 2026-07-02): send 262 (game STATE) BEFORE 116
                    # (show screen). setGame FUN_7f4240 binds screen+0x28 only if the game's state resolves at
                    # show-time; if 116 shows the screen before 262 attaches the state, screen+0x28 stays 0 ->
                    # the board-build FUN_7f3f70 defers returnToMenu -> switchScreen(2) = the casual-lobby BOUNCE.
                    c.sendall(build_addgroups([(launch_gid, launch_gid, 6)]))  # game-group -> setGame resolve key
                    c.sendall(build_eq_launchgame(launch_gid, depth=depth))    # 80008 -> renderable EQGame object
                    time.sleep(0.2)
                    # ★ SETUP-FIRST (2026-07-03, Ghidra: EQGame::setup FUN_62f260 SETS Game+0x38c = the element
                    # POPULATE FUN_62a950 needs during 262 deserialize). +0x38c is NOT serialized, so the normal
                    # protocol runs SetupGame(67) BEFORE the game state. Our old order (67 after 262) -> POPULATE
                    # crashes (+0x38c=0). Send 67 (and SetPlayer 65s) BEFORE 262 so setup primes +0x38c, then the
                    # 262 deserialize's POPULATE resolves it -> no cdb seed needed. Live-experiment SWGTCG_SETUP_FIRST=1.
                    if os.environ.get("SWGTCG_SETUP_FIRST", "0") != "0":
                        if os.environ.get("SWGTCG_SETPLAYER_FIRST", "1") != "0":
                            c.sendall(build_gamecommand_setplayer(player_id=1, game_id=launch_gid))
                            c.sendall(build_gamecommand_setplayer(player_id=2, game_id=launch_gid))
                            time.sleep(0.1)
                        # player_order = ACCOUNT IDS (1,2), not seat indices (0,1): EQGame::setup resolves each
                        # mPlayerOrderData value via FUN_4d9400 (player registry keyed by account id); a 0 -> NULL
                        # player -> AV @0x5860a8 (LIVE-confirmed). SWGTCG_SETUP_ORDER overrides for sweeping.
                        _ord = os.environ.get("SWGTCG_SETUP_ORDER", "1,2")
                        _order = tuple(int(x) for x in _ord.split(","))
                        c.sendall(build_gamecommand_setupgame(game_id=launch_gid, match_id=launch_gid,
                            player_count=2, account_ids=(1, 2), player_order=_order, my_player_id=1))
                        time.sleep(0.2)
                        log(16783, n, "-> [SETUP_FIRST] SetPlayer(65)x2 + SetupGame(67, order=%s) BEFORE 262 (prime Game+0x38c)" % (_order,))
                    c.sendall(build_sendserializedgame(eq_blob, game_id=launch_gid))  # 262 -> load the game STATE first
                    time.sleep(0.3)
                    c.sendall(build_launchgame(launch_gid))                    # 116 -> SHOW the screen; setGame binds +0x28
                    # RENDER (2026-07-02): the board renders when the add-players display handler FUN_7f9420 fires,
                    # which is the setMyPlayerID handler -> triggered by GameCommand_SetupGame(67). Send it so the
                    # client populates the game screen's seat list (screen+0x108) -> addPlayer per player -> render.
                    # ★ CONVERGED PLAN (2026-07-03, 3-agent + Ghidra): all cmds share ONE global game DAT_00b54ed8;
                    # 262 deserializes INTO it (no new); so 67-AFTER-262 runs EQGame::setup on the fully-populated
                    # deserialized game -> advanceTurn (vt+0xa0) on the REAL game. player_order MUST be ACCOUNT IDS
                    # (1,2), NOT seat indices (0,1): EQGame::setup resolves each mPlayerOrderData value via the
                    # player registry FUN_4d9400 (keyed by account id); a seat index 0 -> NULL player -> AV.
                    # Invariant: the 262 blob must carry GameIsSetup(+0x168)=0 (build_eq_prestart_capture does) or
                    # setup(67) early-outs and skips advanceTurn. Observe live with probe_prestart_observe.cdb.
                    if os.environ.get("SWGTCG_SETUPGAME", "1") != "0":
                        time.sleep(0.3)
                        _po = os.environ.get("SWGTCG_SETUP_ORDER", "1,2")
                        _porder = tuple(int(x) for x in _po.split(","))
                        # ★ SEAT MAP (SWGTCG_EQ_SETUP=1): the position/team maps that seat players live ONLY on the
                        # EQ variant EQGameCommand_SetupGame (classid 80007); base 67 -> base execute ignores them.
                        # LIVE 2026-07-03: sending classid 80007 gets DROPPED by the client's command receive path
                        # (setup never runs -> stuck on starscape) -- 80007 is not (trivially) a receivable command
                        # classid. So default OFF = base 67 (board renders; mPosition=-100 needs the cdb assert-
                        # suppress). Delivering the EQ command is the open sub-problem (command factory / EQ
                        # polymorphism). Keys = account ids the client stamps on each fresh EQPlayer +0x34 (obs 3,4).
                        _posmap = _teammap = None
                        if os.environ.get("SWGTCG_EQ_SETUP", "0") != "0":
                            _sa = tuple(int(x) for x in os.environ.get("SWGTCG_SEAT_ACCTS", "3,4").split(","))
                            _posmap = {_sa[0]: 0, _sa[1]: 1}     # accountId -> seat position
                            _teammap = {_sa[0]: 0, _sa[1]: 1}    # accountId -> team
                        # PER-CLIENT PERSPECTIVE (2026-07-04): my_player_id = setMyPlayerID = which seat is "you".
                        # Map this conn's account -> player id via SEAT_ACCTS (acct 3=u1->player 1, acct 4=u2->player 2)
                        # so each client sees ITSELF at the bottom seat (opponent at top) -- per-player boards.
                        _sa2 = tuple(int(x) for x in os.environ.get("SWGTCG_SEAT_ACCTS", "3,4").split(","))
                        _mypid = 1 if a == _sa2[0] else (2 if a == _sa2[1] else 1)
                        c.sendall(build_gamecommand_setupgame(game_id=launch_gid, match_id=launch_gid,
                            player_count=2, account_ids=(1, 2), player_order=_porder, my_player_id=_mypid,
                            position_map=_posmap, team_map=_teammap))
                        log(16783, n, "-> [PERSPECTIVE] SetupGame(67) acct=%s my_player_id=%d" % (a, _mypid))
                    # ★ READY-FOR-START (SWGTCG_SEND_117, default ON 2026-07-04d): the EQStartOfGameState phased
                    # update (FUN_1025e3f0) STALLS in phase 2 on the gate FUN_1025d6a0 = "all players ReadyForStart"
                    # (iterates game+0xf4, needs each player's +4 flag + FUN_10186190). Without it the deal (phase 5,
                    # DrawCard) never runs -> 0 cards / no mulligan / empty seats. Deliver ReadyForStartOfGame(117)
                    # for BOTH players so the gate passes -> phase advances -> deal. (Was only wired in the old
                    # single-client TEST_CAPTURE path.) Player ids = 1,2 (the game's internal player numbers).
                    if os.environ.get("SWGTCG_SEND_117", "1") != "0":
                        time.sleep(0.3)
                        # RE finding (2026-07-05): the readiness list (SOGstate+0xF4) is EMPTY until the client
                        # reaches phase 3; a single 117 at launch sets nothing. RESEND repeatedly so one 117 lands
                        # when the node exists (player 1 enrolls at phase 3) -> sets the flag + re-enters the SM
                        # via processEvents -> the child draw sub-machine advances -> full hand deal. Background
                        # thread so it doesn't block. SWGTCG_117_REPEAT (count) / SWGTCG_117_INTERVAL (secs).
                        for _pid in (1, 2):
                            c.sendall(build_gamecommand_readyforstartofgame(player_account=_pid, game_id=launch_gid))
                        log(16783, n, "-> [SEND_117] ReadyForStartOfGame(117) players 1,2 (unstall StartOfGame deal gate)")
                        # EXPERIMENTAL opt-in pump (SWGTCG_117_PUMP=1): resend 117 repeatedly to try to land one
                        # when the readiness list populates (player 1 enrolls at phase 3). Tested 2026-07-05: alone
                        # it did NOT unstick the deal (a phase-1 stall precedes the phase-2/3 readiness; and the 117
                        # handler addr is uncertain vs the loaded DLL) -- kept off by default, on for experiments.
                        if os.environ.get("SWGTCG_117_PUMP", "0") != "0":
                            _rep = int(os.environ.get("SWGTCG_117_REPEAT", "30"))
                            _iv = float(os.environ.get("SWGTCG_117_INTERVAL", "0.5"))
                            _lg = launch_gid
                            def _pump_117(_c=c, _n=n, _gid=_lg, _rep=_rep, _iv=_iv):
                                for _i in range(_rep):
                                    try:
                                        for _pid in (1, 2):
                                            _c.sendall(build_gamecommand_readyforstartofgame(player_account=_pid, game_id=_gid))
                                    except Exception:
                                        return
                                    time.sleep(_iv)
                            threading.Thread(target=_pump_117, daemon=True).start()
                            log(16783, n, "-> [SEND_117] PUMP started (%dx @ %ss)" % (_rep, _iv))
                    # SERVER-DRIVEN CARD TEST (SWGTCG_INTRO): fire GameCommand_IntroduceCard(56) for real
                    # existing element ids -> notifyCardIntroduced -> SM->vt[14](msg 2) -> current state should
                    # emit DA 0x73 -> card draws WITHOUT a cdb inject. If no 0x73 fires, the current state is a
                    # return-0 shell (needs the per-state-buffer serializer). Ids = p1 quest 1000061-63 + hand.
                    # REAL EQCard element ids extracted from the 262 capture (decode_wire_game element_shells,
                    # classid 80001 = EQCard; 119 total). Send a batch so if the current state processes 56 ->
                    # DA 0x73, a card draws. SWGTCG_INTRO_IDS overrides the batch.
                    if os.environ.get("SWGTCG_INTRO", "0") != "0":
                        time.sleep(0.6)
                        _idsenv = os.environ.get("SWGTCG_INTRO_IDS", "")
                        if _idsenv:
                            _ids = [int(x) for x in _idsenv.split(",")]
                        else:
                            _ids = list(range(1000010, 1000030))   # real capture EQCard ids (a hand/deck batch)
                        for _cid in _ids:
                            c.sendall(build_gamecommand_introducecard(player_id=1, game_id=launch_gid, card_id=_cid))
                            time.sleep(0.08)
                        log(16783, n, "-> [SWGTCG_INTRO] IntroduceCard(56) x%d REAL capture element ids %s.. -- watch board" % (len(_ids), _ids[:3]))
                except Exception:
                    pass
            log(16783, n, "-> LAUNCH(%s) mid=%s members=%s gid=%d: game-group + 80008 + 116 + 262(EQ %dB)%s" % (
                "eq-prestart" if PRESTART else "eq-render", mid, members, launch_gid, len(eq_blob),
                " + SetupGame(67) [client boots StartOfGame]" if PRESTART else ""))
            return
    blob, launch_gid = _board_blob_and_gameid(members)
    for a, c in conns:
        if c:
            try: c.sendall(build_launchgame(launch_gid))
            except Exception: pass
    if LAUNCH_MODE == "launch_only":
        log(16783, n, "-> LAUNCH(launch_only) mid=%s members=%s: LaunchGame(116) only (no 262)" % (mid, members))
        return
    time.sleep(0.2)
    for a, c in conns:
        if c:
            try: c.sendall(build_sendserializedgame(blob, game_id=launch_gid))
            except Exception: pass
    log(16783, n, "-> LAUNCH(full) mid=%s members=%s mode=%s gid=%d blob=%dB: LaunchGame(116)+SendSerializedGame(262)" % (
        mid, members, BOARD_MODE, launch_gid, len(blob)))

def _lobbygroup(contain, gid, typ):
    # Lobby(103) v1 element: classid-once + 5 PRESENT PropertySets + 5 empty role vecs + contain/gid/type.
    return (enc_int(103) + enc_int(1) + _present_propset * 5 + enc_int(0) * 5
            + enc_int(contain) + enc_int(gid) + enc_int(typ))

def build_addgroups(groups):   # groups = [(contain, gid, typ), ...]
    return frame(_env(94, [1, 1, 1]) + enc_int(0) + enc_int(len(groups))
                 + b"".join(_lobbygroup(c, g, t) for c, g, t in groups))

def build_changelobbydisplay(display_id, base=0):
    return frame(_env(291, [1, 1, 1]) + enc_int(base) + enc_int(display_id))

def build_news(items):
    """NetworkCommand_News(457). deserialize FUN_10157980: begin(457,ver) -> vec<string>(headline)
    -> vec<string>(body) -> vec<int>(id) -> end. DEPTH=1. run FUN_10157ab0 iterates the 3 PARALLEL
    arrays, so all three MUST be the same length. items = [(id:int, headline:str, body:str), ...].
    Empty items -> the known-safe empty push (three zero-count vectors)."""
    heads = b"".join(enc_str(h) for _, h, _ in items)
    bodies = b"".join(enc_str(b) for _, _, b in items)
    ids = b"".join(enc_int(i) for i, _, _ in items)
    n = len(items)
    return frame(_env(457, [1])          # DEPTH=1: single (classid,version) level
                 + enc_int(n) + heads
                 + enc_int(n) + bodies
                 + enc_int(n) + ids)

def build_displaymotd(text, i1=0, i2=0, text2=""):
    """LoginCommand_DisplayMOTD(309). deserialize FUN_10157340 (vt9): begin(309) -> LoginCommand(77)
    base (begin -> root base begin/end -> end) -> string(+0x04) -> int(+0x20) -> int(+0x24) ->
    string(+0x28) -> end. DEPTH=3 (three begins, leaf classid 309 at each), fixed 4 fields str/int/int/str.
    The two ints are semantically unresolved (likely id/version by EULA(456) analogy); 0,0 parses safe.
    (RE agent ad71061b, capture-grounded on the client vt9 deserializer -- no on-wire retail capture.)"""
    return frame(_env(309, [1, 1, 1]) + enc_str(text) + enc_int(i1) + enc_int(i2) + enc_str(text2))

def build_leaderboard(primary, secondary=None):
    """NetworkCommand_LeaderBoardData(458). deserialize FUN_10170230 (vt9): begin(458) -> then TWO groups,
    each = vec<int> + vec<string> + vec<int>, read in this (deserialize) order: group@+0x34/+0x44/+0x54
    FIRST, then group@+0x04/+0x14/+0x24. DEPTH=1. Each row = (int, name, int) ~ (rank, name, rating).
    primary/secondary = [(int1, name, int2), ...]; secondary None -> empty second board.
    Safe empty stub = build_leaderboard([]). (RE agent ad71061b -- grounded on the client vt9; no capture.)"""
    def _group(rows):
        rows = rows or []
        n = len(rows)
        c1 = enc_int(n) + b"".join(enc_int(int(r[0])) for r in rows)
        nm = enc_int(n) + b"".join(enc_str(str(r[1])) for r in rows)
        c2 = enc_int(n) + b"".join(enc_int(int(r[2])) for r in rows)
        return c1 + nm + c2
    # emit in deserialize order: the +0x34 group is read first, so it goes first on the wire.
    return frame(_env(458, [1]) + _group(primary) + _group(secondary))

# ---- Tournament lobby commands (RE agent ad4dbec9) --------------------------------------------------
# Every LobbyCommand_X is depth 3: _env(cid,[V,1,1]) + enc_int(baseInt=0) + <leaf fields>, exactly like
# the working ChangeLobbyDisplay(291)/AddGroups(94). The Tournaments TAB is filled the same way Casual is:
# push a Lobby(103) group with a tournament eLobbyTypeID (build_addgroups), a SetTournament(299), then a
# ChangeLobbyDisplay(291). NO on-wire captures exist for these -- grounded on the client vt9 deserializers.
def build_settournament(tournament_id, round=0, timer=0, group_id=None):
    """LobbyCommand_SetTournament(299): baseInt + int tournamentID + int round + int timer
    + [int groupID only if leaf version >= 2]. Passing group_id bumps the leaf version to 2."""
    ver = 2 if group_id is not None else 1
    body = _env(299, [ver, 1, 1]) + enc_int(0) + enc_int(tournament_id) + enc_int(round) + enc_int(timer)
    if group_id is not None:
        body += enc_int(group_id)
    return frame(body)

def build_updatetournament(group_id, tournament_id, pairings=(), round=0, substate=0):
    """LobbyCommand_UpdateTournament(293): baseInt + int GroupID + int TournamentID
    + vec pairings [int N + N*(int,int,int)] + int Round + int SubState. pairings = [(a,b,c), ...]."""
    pv = enc_int(len(pairings)) + b"".join(enc_int(a) + enc_int(b) + enc_int(c) for a, b, c in pairings)
    return frame(_env(293, [1, 1, 1]) + enc_int(0) + enc_int(group_id) + enc_int(tournament_id)
                 + pv + enc_int(round) + enc_int(substate))

def _push_tourney_lobby(conn, dbc, acct, port=16783, n=0):
    """Push the tournament-lobby GROUP (gid=5, type=5) + SetTournament(299)/UpdateTournament(293) for each
    open tournament, so the display=5 screen resolves its group + populates. DEFERRED (NOT at a casual login):
    the type-5 group in the lobby model + SetTournament's active-tournament manager state break the casual
    Create dialog (RE'd), so casual logins stay clean and this runs only at a tournaments login or on the
    Tournaments-tab click. The AddGroups(gid=5) is sent once per session (idempotent); 299/293 refresh each
    call. The caller sends the ChangeLobbyDisplay(5) itself, so an empty/failed tournament list is harmless."""
    try:
        tourneys = dbmod.list_tournaments(dbc, states=("open", "locked", "running"))
    except Exception as e:
        log(port, n, "-> tournament load FAILED: %s" % e)
        return
    if not tourneys:
        return
    if acct not in TOURNEY_GROUP_SENT:
        conn.sendall(build_addgroups([(0, TOURNEY_LOBBYTYPE, TOURNEY_LOBBYTYPE)]))
        TOURNEY_GROUP_SENT.add(acct)
        time.sleep(0.05)   # let the client register the group before 299/293 reference it
        log(port, n, "-> AddGroups(94) tournament-lobby group gid=%d type=%d" % (TOURNEY_LOBBYTYPE, TOURNEY_LOBBYTYPE))
    for t in tourneys:
        conn.sendall(build_settournament(t["id"], round=t.get("round", 0), timer=0, group_id=TOURNEY_LOBBYTYPE))
        conn.sendall(build_updatetournament(TOURNEY_LOBBYTYPE, t["id"], pairings=(), round=t.get("round", 0), substate=0))
    log(port, n, "-> SetTournament(299)+UpdateTournament(293) gid=%d x%d: %s"
        % (TOURNEY_LOBBYTYPE, len(tourneys), [t["name"] for t in tourneys]))

def build_starttournamentround(tournament_id, round):
    """LobbyCommand_StartTournamentRound(300): baseInt + int tournamentID + int round."""
    return frame(_env(300, [1, 1, 1]) + enc_int(0) + enc_int(tournament_id) + enc_int(round))

def build_starttournamentmsg(field1, field2=0):
    """LobbyCommand_StartTournamentMsg(460): baseInt + int + int (names unresolved; ~tournamentID + msg id)."""
    return frame(_env(460, [1, 1, 1]) + enc_int(0) + enc_int(field1) + enc_int(field2))

def build_infoenummessage(message, arg1=None, arg2=None):
    """LobbyCommand_InfoEnumMessage(296): baseInt + int Message + subobj Arg1 + subobj Arg2.
    NULL args (arg=None) are the safe form (enc_int(1)); PRESENT ValueData wire is unresolved."""
    a1 = NULL_SUBOBJ if arg1 is None else arg1
    a2 = NULL_SUBOBJ if arg2 is None else arg2
    return frame(_env(296, [1, 1, 1]) + enc_int(0) + enc_int(message) + a1 + a2)

def _parse_join(body):         # body = client->server payload (after the 12-byte routing header)
    i = 0
    for _ in range(3):         # 3 envelope levels: (classid, version) each
        _, i = dec_int(body, i); _, i = dec_int(body, i)
    jacct, i = dec_int(body, i); grp, i = dec_int(body, i); f3, i = dec_int(body, i)
    return jacct, grp, f3

# ============================================================================
# GAME-BOARD BUILDERS (GameCommand_*) — from the SWGTCGGame.exe Rosetta Stone
# (instrumented dump methods labeled every field) + the client deserializers.
# Every GameCommand chain is GameCommand_X -> GameCommand(base) -> Command(base),
# so envelope DEPTH = 3 (mirrors build_join's triple-classid): the LEAF classid is
# repeated at all three begin() levels. begin#1's version GATES the leaf's optional
# fields; begin#2 (GameCommand base) v1 keeps its v2 TimeStamp+vector readers OFF.
# Run prerequisite for ALL of these: a client-side Game with id 200 must already
# exist (MatchCommand_LaunchGame(116) created it) or the deser/run asserts on null mGame.
# ============================================================================

def build_gamecommand_setupgame(game_id=200, server_id=1, player_count=2,
                                player_order=(0, 1), account_ids=(1, 2),
                                match_id=200, game_num=1, game_version=0, version=11,
                                my_player_id=1, position_map=None, team_map=None):
    """GameCommand_SetupGame(67). Binds players/seating to the Game LaunchGame created.
    The GameCommand-base baseInt is the LOCAL player id -> SetupGame::run calls setMyPlayerID(baseInt),
    which asserts non-zero (Game.cpp:4228); so my_player_id MUST be non-zero. game_id (base copy) must
    name a live client Game. game_version=0 skips the (non-fatal) server-version mismatch log.

    ★ SEATING (2026-07-03, 4-agent RE): base fields (FUN_0050b6b0, V<=9) alone leave the client's seat
    map game+0x3e8 EMPTY -> EQGame::setup never calls setPosition -> EQPlayer mPosition(+0x58)=-100 ->
    render asserts (eqplayer.cpp:376). The position + team maps are EQ-derived command fields
    (EQGameCommand_SetupGame::readFrom FUN_0063b050, ver>10): cmd+0x80 position, cmd+0x8c team. Their
    execute FUN_0063b380 COPIES them into game+0x3e8/+0x3f4, then setup's map::find(playerAcctId) hits
    -> setPosition/setTeam fire -> players seated. Wire = int->int map FUN_0045c310 = enc_int(count) +
    (key,value) enc_int pairs. Keys = the account ids the client stamps on each fresh EQPlayer +0x34 via
    addPlayer (LIVE-observed 3,4 for u1), value = seat/team. => version 11 (>10) + the two maps."""
    V = version
    # ★ The EQ variant (classid 80007 = EQGameCommand_SetupGame) is a SEPARATE class from base
    # GameCommand_SetupGame (67). Only the EQ execute FUN_0063b380 copies the position/team maps into the game;
    # classid 67 -> base execute FUN_0050af90 (no maps). So to carry the maps we must send the EQ classid as the
    # OUTERMOST framing id -> the client builds EQGameCommand_SetupGame -> its readFrom FUN_0063b050 (1 extra
    # begin, prepended) reads the maps at ver>10. Envelope depth: EQ(80007) -> SetupGame(67) -> GameCommand(67)
    # -> Serializable(67) = 4 (classid,version) pairs; base = the last 3. eq_version (pair1) gates the maps.
    eq = (position_map is not None) or (team_map is not None)
    eq_version = 11
    if eq:
        env = ((enc_int(80007) + enc_int(eq_version)) + (enc_int(67) + enc_int(V)) +
               (enc_int(67) + enc_int(1)) + (enc_int(67) + enc_int(1)))
    else:
        env = ((enc_int(67) + enc_int(V)) + (enc_int(67) + enc_int(1)) + (enc_int(67) + enc_int(1)))
    def vec_int(xs):
        out = enc_int(len(xs))
        for x in xs: out += enc_int(x)
        return out
    def enc_intmap(m):            # FUN_0045c310: count + (key,value) enc_int pairs
        out = enc_int(len(m or {}))
        for k, v in (m or {}).items(): out += enc_int(k) + enc_int(v)
        return out
    body = env
    body += enc_int(my_player_id) # GameCommand base: baseInt = local player id (setMyPlayerID; non-zero!)
    body += enc_int(game_id)      # GameCommand base: Game ID copy -> mGame lookup (REQUIRED valid)
    body += enc_int(game_id)      # leaf: Game ID
    body += enc_int(server_id)    # leaf: Game Server ID / Server ID
    if V > 8: body += enc_int(0)  # leaf: unlabeled v>8
    body += enc_int(player_count) # leaf: Player Count
    body += vec_int(player_order) # leaf: Player Order Data (vec<int>)
    body += vec_int(account_ids)  # leaf: Ordered Account IDs (vec<int>)
    body += enc_int(game_version) # leaf: GameVersion (0 => skip check)
    body += enc_int(match_id)     # leaf: MatchID
    if V > 2: body += enc_int(0)  # Duration
    if V > 3: body += enc_int(0)  # GameType
    if V > 4: body += enc_int(0)  # PlayType
    if V > 5: body += enc_int(0)  # ExtraFormatFlag
    if V > 6: body += enc_int(game_num)  # GameNum
    if V > 7:
        body += enc_int(0)        # MatchStructure data (empty vec<subobject>)
        body += enc_int(0)        # LeagueID
    if V > 9: body += enc_int(0)  # MatchStructure (base)
    # ★ EQ-derived (EQGameCommand_SetupGame::readFrom FUN_0063b050): matchStructure scalar (eq_ver>11),
    #   then the position + team int-maps (eq_ver>10) that seat the players.
    if eq:
        if eq_version > 11: body += enc_int(0)       # EQ matchStructure scalar (cmd+0x98), eq_ver>11 only
        body += enc_intmap(position_map)             # cmd+0x80 -> game+0x3e8 (accountId -> seat)
        body += enc_intmap(team_map)                 # cmd+0x8c -> game+0x3f4 (accountId -> team)
    return frame(body)

def build_gamecommand_setplayer(player_id=1, game_id=200):
    """GameCommand_SetPlayer(65). Fills one player slot. Run asserts PlayerID!=0 + live mGame."""
    env = ((enc_int(65) + enc_int(1)) + (enc_int(65) + enc_int(1)) + (enc_int(65) + enc_int(1)))
    return frame(env + enc_int(player_id) + enc_int(game_id))

DECK_CLASSID = 46  # 0x2e — Deck::getClassId (vt6) returns 0x2e; begin validates "Wanted classID"

def build_deck_subobject(deck_id="deck_0001", deck_name="Test Deck",
                         cards=((267554, 1), (267555, 2)),  # (cardId, instanceId)
                         version=10, field_2="", field_4="",
                         b0=0, b88=0, b89=0, b8a=0, i8c=0, i90=0):
    """PRESENT Deck sub-object BYTES (NOT framed) for SelectDeckForPlayer's [+0x10] slot.
    Deck = ComponentFactory classid 46 (RE-confirmed). DEPTH 1 (monolithic deser). Fields:
    deckID(str), str, deckName(str), str, bool, card-list(count + {cardId,instanceId}*), then
    version-gated bools/ints. version=10 = the client-native version."""
    out  = enc_int(0) + enc_int(DECK_CLASSID) + enc_int(version)
    out += enc_str(deck_id) + enc_str(field_2) + enc_str(deck_name) + enc_str(field_4)
    out += enc_int(1 if b0 else 0)
    cards = list(cards)
    out += enc_int(len(cards))
    for cid, inst in cards:
        out += enc_int(cid) + enc_int(inst)
    if version > 1: out += enc_int(1 if b88 else 0)
    if version < 4: out += enc_int(0)            # legacy discard int
    if version > 5: out += enc_int(1 if b89 else 0) + enc_int(1 if b8a else 0)
    if version > 7: out += enc_int(i8c)
    if version > 9: out += enc_int(i90)
    return out

def build_gamecommand_selectdeckforplayer(player_id=1, game_id=200, deck_id="deck1", deck=None, version=1):
    """GameCommand_SelectDeckForPlayer(58). Run REQUIRES a PRESENT Deck sub-object (NULL crashes)."""
    env = ((enc_int(58) + enc_int(version)) + (enc_int(58) + enc_int(1)) + (enc_int(58) + enc_int(1)))
    if deck is None: deck = enc_int(1)  # NULL placeholder (crashes run) — supply a PRESENT deck
    return frame(env + enc_int(player_id) + enc_int(game_id) + enc_str(deck_id) + deck)

def build_gamecommand_readyforstartofgame(player_account=1, game_id=200, timestamp=(0, 0), sync_ids=None, version=2):
    """GameCommand_ReadyForStartOfGame(117). vt15 run is a stub -> nothing asserted."""
    env = (enc_int(117) + enc_int(version)) + (enc_int(117) + enc_int(version)) + (enc_int(117) + enc_int(1))
    body = env + enc_int(player_account) + enc_int(game_id)
    if version >= 2:
        ts0, ts1 = timestamp
        body += enc_int(ts0) + enc_int(ts1)
        ids = sync_ids or []
        body += enc_int(len(ids))
        for v in ids: body += enc_int(v)
    return frame(body)

def build_gamecommand_drawcards(player_id=1, game_id=200, card_ids=(301, 302), source_element_id=200, version=1):
    """GameCommand_DrawCards(68). Draws card element ids into a hand. Each must resolve to a
    client-side PlayElement(Card) or the loop breaks (non-fatal); empty list = safe no-op."""
    env = ((enc_int(68) + enc_int(version)) + (enc_int(68) + enc_int(1)) + (enc_int(68) + enc_int(1)))
    base = enc_int(player_id) + enc_int(game_id)
    drawn = enc_int(len(card_ids)) + b"".join(enc_int(c) for c in card_ids)
    return frame(env + base + drawn + enc_int(0) + enc_int(source_element_id))

def build_gamecommand_introducecard(player_id=1, game_id=200, card_id=267554, instance_first=1, instance_second=0, version=1):
    """GameCommand_IntroduceCard(56). Introduces a card instance onto the board."""
    env = ((enc_int(56) + enc_int(version)) + (enc_int(56) + enc_int(1)) + (enc_int(56) + enc_int(1)))
    return frame(env + enc_int(player_id) + enc_int(game_id) + enc_int(card_id)
                 + enc_int(instance_first) + enc_int(instance_second))

def build_sendserializedgame(serialized, game_id=200, base_int=1, version=1):
    """GameCommand_SendSerializedGame(262) — deliver a serialized Game (zlib blob) -> Game::deserialize
    (FUN_100fca50) reconstructs the started game (players/cards/GameTurn/StateMachine). PHASE C.
    Wire (M13): env(262,3) + GameCommand-base(baseInt+GameID) + uncompLen + compLen + raw zlib bytes.
    The run zlib-inflates the bytes (assert zres==Z_OK) then calls getGameById(GameID)->Game::deserialize."""
    comp = zlib.compress(serialized)
    env = ((enc_int(262) + enc_int(version)) + (enc_int(262) + enc_int(1)) + (enc_int(262) + enc_int(1)))
    body = env + enc_int(base_int) + enc_int(game_id) + enc_int(len(serialized)) + enc_int(len(comp)) + comp
    return frame(body)

# ============================================================================
# COLLECTION (owned cards) + ONLINE DECK delivery  — RE'd from the live DLL.
#   Sync session  : SynchronizationCommand_StartInstances(84)=FUN_101b24f0 ->
#                   SendInstances(85)=FUN_101b0bc0 (rows) -> Complete(86)=FUN_101aeec0.
#                   All three chain X -> SynchronizationCommand(82,FUN_101ae790) ->
#                   Command(FUN_1016c640): 3 begins => envelope DEPTH=3 (leaf classid x3),
#                   and the bases read NO extra int (unlike LobbyCommand). The run sets up
#                   the collection by TYPE: 1=online(owned), 2=offline, 3=trade
#                   (FUN_101b3380/FUN_101b2a20 "%s's Online Collection"). type 1 = owned cards.
#   Instance row  : the SendInstances vector (reader FUN_10077f40, elem vt+0x24) carries flat
#                   instances, ground-truthed from TCGStandalone .cln files as FIVE ints:
#                       [350, 1, productId, 0, instanceId]
#                   productId = the card's catalog id (= storage.dat record id, standalone-sourced); instanceId
#                   = a unique id per physical copy. The run (FUN_101b3990) groups instances by
#                   productId into CollectionItem(52) objects -> the owned collection.
#   Online decks  : DeckCommand_PopulateOnlineDeckData(363)=FUN_100a6a80 is the SERVER->CLIENT
#                   push that fills the deck picker (run FUN_100a5ea0 fires UI event 0xc2).
#                   361 AddOnlineDeck is the client->server UPLOAD (its run is a stub) and 101
#                   SelectDeck is the client->server PICK (run stub) — we don't push those.
#   Deck object   : the deck sub-object (reader FUN_100a4ed0) factory-creates by classid. Deck
#                   (46) is NOT registered, but EQDeck(80002=0x13882) IS (REG4 trigger 102216e0).
#                   EQDeck::readFrom (FUN_10222130) = EQDeck begin + Deck-base readFrom
#                   (FUN_10099770) + EQDeck tail => the object is written classid-twice (80002),
#                   sub-object framing = [present=0][80002,verEQ][80002,verDeck][deck fields...].
# ============================================================================

SYNC_TYPE_ONLINE = 1    # owned cards bucket (2=offline, 3=trade)
EQDECK_CLASSID = 80002  # 0x13882 — the *registered* deck class for the online-deck factory path
INSTANCE_TAG = 350      # constant first field of every .cln instance row (RE ground truth)

def _env(cid, versions):
    """Command envelope: one (classid,version) per hierarchy level (begin re-reads the leaf classid)."""
    return b"".join(enc_int(cid) + enc_int(v) for v in versions)

def _vec_ints(xs):
    return enc_int(len(xs)) + b"".join(enc_int(x) for x in xs)

def mint_instances(cards, start=1000001, tag=INSTANCE_TAG):
    """Expand [(catalog_id, qty), ...] into flat instance rows, each a unique instanceId.
    Returns (rows, next_id) where rows = [(tag,1,catalog_id,0,instanceId), ...]."""
    rows = []; iid = start
    for cat, qty in cards:
        for _ in range(qty):
            rows.append((tag, 1, cat, 0, iid)); iid += 1
    return rows, iid

def build_sync_startinstances(account, count, ctype=SYNC_TYPE_ONLINE):
    """SynchronizationCommand_StartInstances(84). Opens/creates the collection of `ctype` for
    `account` and declares the row count. Fields (deser FUN_101b24f0): key(+4)=account,
    type(+8)=ctype(1=online), count(+0xc). leaf-ver=1 skips the optional +0x10 int."""
    return frame(_env(84, [1, 1, 1]) + enc_int(account) + enc_int(ctype) + enc_int(count))

def build_sync_sendinstances(account, rows):
    """SynchronizationCommand_SendInstances(85). Delivers instance rows that are ADDED to the
    collection. Fields (deser FUN_101b0bc0, leaf-ver=1): key(+8)=account, count(+0xc)=len(rows),
    vector(+0x10)=instances. Each instance = 5 ints [350,1,catalogId,0,instanceId]."""
    vec = enc_int(len(rows)) + b"".join(b"".join(enc_int(v) for v in r) for r in rows)
    return frame(_env(85, [1, 1, 1]) + enc_int(account) + enc_int(len(rows)) + vec)

def build_sync_complete(account):
    """SynchronizationCommand_Complete(86). Finalizes the session (run FUN_101b2c50 fires the
    'collection ready' event 0x1d). leaf-ver=1 => no fields."""
    return frame(_env(86, [1, 1, 1]))

def build_collection_sync(account, cards, start_instance=1000001):
    """Full owned-cards delivery: [84 StartInstances, 85 SendInstances, 86 Complete] frames.
    `cards` = [(catalog_id, qty), ...]; the player ends up OWNING exactly those copies."""
    rows, _ = mint_instances(cards, start=start_instance)
    return [build_sync_startinstances(account, len(rows)),
            build_sync_sendinstances(account, rows),
            build_sync_complete(account)]

def _card_seq_vec(seq):
    """The deck's card list wire = [count] + count x (cardId, 0). The client stores ONE entry per
    PHYSICAL COPY (quantity is by REPETITION, NOT a (cardId,qty) pair) and the 2nd int is always 0.
    Ground-truthed from the client's own DeckCommand_AddOnlineDeck(361) upload (see captures)."""
    out = enc_int(len(seq))
    for cid in seq:
        out += enc_int(cid) + enc_int(0)
    return out

def build_eqdeck_subobject(deck_id, deck_name, main_cards, avatar_catid, quest_catids,
                           avatar_subobj=b"", str2="", str4=""):
    """PRESENT EQDeck(80002) sub-object BYTES (not framed) for the 363/101 deck slot.
    EXACT format reverse-engineered from the live client's own AddOnlineDeck(361) upload
    (E:\\SWGTCG\\re\\server\\captures\\srv_181719_p16783_c2.bin) -- reproduces it BYTE-FOR-BYTE.
    Wire (EQDeck::readFrom FUN_10222130 -> Deck base FUN_10099770), classid 80002 written TWICE,
    VERSION 10 (the client-native version, Deck::getVersion vt8 = 10):
      [present=0]
      [80002][10]                                  EQDeck begin
      [80002][10]                                  Deck-base begin
      [str deckID][str str2][str deckName][str str4]
      [int 0]                                      Deck +0x74 bool
      [count=N][ N x (cardId,0) ]                  Deck +0x78 = MAIN deck, one entry PER COPY
      [int 0]                                      Deck +0x88
      [int 0][int 0]                               Deck +0x89,+0x8a
      [int 1]                                      Deck +0x8c   (=1 in the client deck)
      [int 0]                                      Deck +0x90
      [int avatar_catid]                           EQDeck +0x94 = AVATAR catalog id
      [int 0]                                      EQDeck +0x98
      [count=Q][ Q x (questId,0) ]                 EQDeck +0x9c = QUEST list, one entry per quest
      [avatar_subobj bytes]                        EQDeck +0xac = avatar's Card object (v>6)

    KEY (the qty bug fix): quantities are expressed by REPETITION in the +0x78 list, NOT as a
    (cardId,qty) pair -- the client ignored our 2nd-int "qty", so 20 pairs showed as 20 single
    cards. We now emit each copy separately (a 55-card deck = 50 main copies + 1 avatar + 4 quests).
    The AVATAR is NOT in the card list -- it lives in +0x94 (id) + the +0xac embedded Card.
    The QUESTS are NOT in the main list -- they are the +0x9c list.

    `main_cards`  = [(catalog_id, qty), ...] for NON-avatar, NON-quest cards (expanded to copies).
    `avatar_catid`= the avatar's catalog id.
    `quest_catids`= [catalog_id, ...] of the quests (each appears once).
    `avatar_subobj`= raw bytes of the avatar's Card object for +0xac (required at v10). If empty,
        we fall back to version 6 (which skips +0x8c/+0x90 and the +0xac subobject); version 6 still
        carries the avatar id in +0x94 and the quests in +0x9c, but omits the embedded avatar card."""
    ver = 10 if avatar_subobj else 6
    main_seq = []
    for cat, qty in main_cards:
        main_seq += [cat] * qty
    out  = enc_int(0)                                            # sub-object present flag
    out += enc_int(EQDECK_CLASSID) + enc_int(ver)              # EQDeck begin
    out += enc_int(EQDECK_CLASSID) + enc_int(ver)              # Deck-base begin (same classid)
    out += enc_str(deck_id) + enc_str(str2) + enc_str(deck_name) + enc_str(str4)
    out += enc_int(0)                                          # +0x74 bool
    out += _card_seq_vec(main_seq)                             # +0x78 MAIN (per-copy, qty via repetition)
    if ver > 1: out += enc_int(0)                              # +0x88
    if ver > 5: out += enc_int(0) + enc_int(0)                # +0x89,+0x8a
    if ver > 7: out += enc_int(1)                             # +0x8c  (=1 in the captured client deck)
    if ver > 9: out += enc_int(0)                             # +0x90
    out += enc_int(avatar_catid) + enc_int(0)                 # +0x94 avatar id, +0x98
    out += _card_seq_vec(list(quest_catids))                  # +0x9c QUEST list (per quest)
    if ver > 6: out += avatar_subobj                          # +0xac avatar Card object
    return out

# Avatar Card sub-object (+0xac) bytes, extracted verbatim from the client's own deck upload
# (captures/srv_181719_p16783_c2.bin). Keyed by avatar catalog id. Needed for the v10 EQDeck.
_AVATAR_SUBOBJ_DIR = os.path.dirname(os.path.abspath(__file__))
def load_avatar_subobj(avatar_catid):
    p = os.path.join(_AVATAR_SUBOBJ_DIR, "starter_avatar_%d.eqcard" % avatar_catid)
    try:
        return open(p, "rb").read()
    except Exception:
        return b""   # -> build_eqdeck_subobject falls back to version 6 (no embedded avatar card)

def build_populate_online_decks(decks):
    """DeckCommand_PopulateOnlineDeckData(363) — SERVER->CLIENT push that fills the deck picker.
    Deser FUN_100a6a80: env(363)x3 + [int count] + count x deck-subobject. `decks` = list of
    EQDeck sub-object byte-blobs (from build_eqdeck_subobject)."""
    body = _env(363, [1, 1, 1]) + enc_int(len(decks)) + b"".join(decks)
    return frame(body)

_n = 0; _lock = threading.Lock()

# --- multi-client lobby state: drive a REAL 2-player match (two standalones) instead of faking the
#     opponent. Each 16783 (lobby) connection gets its own account; we relay lobby/match commands between
#     the connected clients so they see each other, and start the game only when BOTH report ready. ---
import os as _os
MULTI_CLIENT = _os.environ.get("MULTI_CLIENT", "1") != "0"   # True = real 2-client; env MULTI_CLIENT=0 for single-client test
LOBBY = {}                      # account -> conn (connected lobby clients), keyed by DB account id
READY = {}                      # account -> last ChangeStatus value (DIAGNOSTIC only; the authoritative
                                # ready state is the per-match MATCHES[mid]["ready"] set, not this dict)
NAMES = {}                      # account -> username (for match Introduce names)
_clients_lock = threading.Lock()

def lobby_broadcast(sender_acct, body):
    """Relay a command body to every OTHER lobby client (server->client frames omit the 12-byte client
    header, so we re-frame the raw body). Returns the list of accounts it was sent to."""
    with _clients_lock:
        targets = [(a, c) for a, c in LOBBY.items() if a != sender_acct]
    sent = []
    for a, c in targets:
        try:
            c.sendall(frame(body)); sent.append(a)
        except Exception:
            pass
    return sent

_ka_logged = set()
def _keepalive_loop():
    """Proactively push NetworkCommand_Ping(112) to every connected client < every 60s so the client-side
    ClientApplication watchdog (ClientApp+0x5c) is reset (via NetworkCommand_Ping::execute) before it reaches 60
    and pops the 'no response from the server' dialog. Uses each client's own captured 112 body for an exact
    envelope; falls back to classid 112 + a zero Spoof Value int (per DUMP_NetworkCommand_Ping @571bd0)."""
    fallback = enc_int(112) + enc_int(0)          # base classid + Spoof Value (obj+4) = 0
    while True:
        time.sleep(KEEPALIVE_IV)
        with _clients_lock:
            targets = list(LOBBY.items())
        for a, c in targets:
            pbody = LAST_PING.get(a, fallback)
            try:
                c.sendall(frame(pbody))
                if a not in _ka_logged:
                    _ka_logged.add(a)
                    log(LOBBY_PORT, 0, "KEEPALIVE: proactive Ping(112) -> acct=%s (%s, %dB) every %.0fs"
                        % (a, "captured" if a in LAST_PING else "fallback", len(pbody), KEEPALIVE_IV))
            except Exception:
                pass

def handle(conn, addr, port):
    global _n, LAST_GW_SESSION
    with _lock:
        _n += 1; n = _n
    ACCT = None   # set when a 16783 lobby connection logs in; used for relay + disconnect cleanup
    dbc = dbmod.connect()   # per-connection DB handle (sqlite3 is per-thread)
    ts = time.strftime("%H%M%S")
    cap = open(os.path.join(OUTDIR, "srv_%s_p%d_c%d.bin" % (ts, port, n)), "wb") if CAPTURE else None
    log(port, n, "connect from %s:%d" % addr)
    conn.settimeout(600)
    # PROACTIVE LOBBY: the FinalLive DLL connects to :16783 but never sends SendSessionID. If enabled, wait briefly
    # for it; on timeout synthesize the login from the gateway-validated session so our lobby handler engages.
    proactive = (port == 16783 and os.environ.get("SWGTCG_PROACTIVE_LOBBY") and LAST_GW_SESSION)
    if proactive:
        conn.settimeout(2.5)
    acc = b""; replied = False
    try:
        while True:
            try:
                data = conn.recv(4096)
                if proactive:   # client sent real data before the 2.5s synth-wait timeout -> synth not needed; restore the long idle timeout so the lobby connection isn't dropped
                    proactive = False; conn.settimeout(600)
            except socket.timeout:
                if proactive and not replied:
                    data = build_fake_sendsessionid(LAST_GW_SESSION)
                    proactive = False; conn.settimeout(600)
                    log(port, n, "PROACTIVE: no SendSessionID from DLL -> synthesized login from gateway session %r" % LAST_GW_SESSION)
                else:
                    raise
            if not data:
                log(port, n, "closed"); break
            if cap: cap.write(data); cap.flush()
            acc += data
            frames, acc = unframe(acc)
            for fr in frames:
                log(port, n, "FRAME %d bytes: %s" % (len(fr), hexdump(fr)))
                if port == 16782 and not replied:
                    replied = True
                    try:
                        sid, user = parse_getconnserver(fr)
                    except Exception:
                        sid, user = None, None
                    sess = dbmod.peek_session(dbc, sid) if sid else None
                    ok = sess is not None
                    if ok: LAST_GW_SESSION = sid   # stash for the proactive lobby synth (FinalLive DLL)
                    rep = build_reply(fr, success=1 if ok else 0)
                    conn.sendall(rep)
                    log(port, n, "-> GetConnectionServer session=%r user=%r -> %s (reply %d B)"
                        % (sid, user, "OK" if ok else "REJECT", len(rep)))
                if port == 16783 and not replied:
                    replied = True
                    # SEAM B: identity is the DB session bound at SendSessionID(411), NOT connection
                    # order. ACCT = the real accounts.id; an invalid/expired session is refused.
                    sid = parse_sendsessionid(fr)
                    account = dbmod.bind_session(dbc, sid) if sid else None
                    if account is None:
                        res = frame(((enc_int(81) + enc_int(1)) * 3)
                                    + enc_int(0) + enc_str("invalid session") + enc_int(0))
                        conn.sendall(res)
                        log(port, n, "lobby login REJECT: session=%r -> no account; closing" % sid)
                        return
                    ACCT = account["id"]
                    username = account["username"]
                    with _clients_lock:
                        LOBBY[ACCT] = conn
                        NAMES[ACCT] = username
                        # Snapshot the joinable rooms ATOMICALLY with the LOBBY add: any create after this
                        # lock targets ACCT via its broadcast (and is NOT in this list); any create before
                        # is in this list (and its broadcast did NOT reach ACCT). No dup, no miss.
                        open_matches_snapshot = [
                            (m["mid"], m["owner"], m["name"], m.get("advert"))
                            for m in MATCHES.values()
                            if m.get("state") in ("open", "full") and not m.get("launched")
                            and m["owner"] != ACCT and len(m["members"]) < 2]
                    log(port, n, "lobby login: session=%r -> account %d (%s) (%d connected)"
                        % (sid, ACCT, username, len(LOBBY)))
                    # ★ SOLO PROACTIVE LAUNCH (headless deal-drive): the minimal SwgTcgHost reaches the casual
                    # lobby but never self-creates a match (no UI). With SOLO_LAUNCH+LAUNCH_EQ, after the client
                    # settles, synthesize a 1-player match + mark ready -> _launch_board pushes the EQ deal
                    # sequence (80008+67+262+116) so advanceTurn->EQStartOfGameState runs. Gated (test-only).
                    if SOLO_LAUNCH and LAUNCH_EQ:
                        def _solo_fire(_conn=conn, _n=n, _acct=ACCT):
                            time.sleep(float(os.environ.get("SWGTCG_SOLO_DELAY", "8")))
                            with _clients_lock:
                                _mid = _alloc_match_locked(_acct, "SOLO")
                            _mark_ready(_conn, _n, _acct)
                            log(16783, _n, "-> [SOLO] proactive launch fired acct=%s mid=%s" % (_acct, _mid))
                        threading.Thread(target=_solo_fire, daemon=True).start()
                        log(16783, n, "-> [SOLO] proactive launch scheduled acct=%s" % ACCT)
                    # each begin reads [classid][version]; count = hierarchy depth.
                    def env(cid, levels): return (enc_int(cid) + enc_int(1)) * levels
                    # Sub-object encoding (reader FUN_1000b460): leading int N != 0 => NULL
                    # (consume ONLY N); N == 0 => PRESENT, then [classid][child deserialize].
                    null_subobj = enc_int(1)
                    # KEY: the factory/sub-object classid read (FUN_1008caf0) is a PEEK -- it does
                    # NOT advance the stream; the object's begin then RE-READS the classid. So a
                    # factory-created object is written with its classid ONCE: [classid][version]
                    # [fields]. (Writing it twice -- as the old code did -- made begin read the 2nd
                    # classid as the VERSION, shifting everything: propmap count read the version
                    # byte, then a bogus entry -> create(classid 0) -> throw. Verified via cdb
                    # caf0/ca30 offset trace.) Present empty PropertySet(23) = N(0=present) +
                    # classid(23) + version(1) + propmap-count(0).
                    present_propset = enc_int(0) + enc_int(23) + enc_int(1) + enc_int(0)
                    TEST_PRESENT_SUBOBJ = False
                    # IntroduceAccount(114): begin + acctbase(begin+int) + name + subobj + acctId
                    #   (DEPTH=2). NULL subobj is 1 byte, so acctId reads the very next int = ACCT
                    #   (the OLD code appended 4 extra propset bytes after the NULL flag, which
                    #   shifted acctId to 23 -- harmless then, but wrong for the lobby self-join).
                    # FIX (RE agent): entitlements (attr 0xfc5 StringList) must ride on
                    # IntroduceAccount(114).mPropertySet, which FUN_1000fe40->FUN_10009800->
                    # FUN_10192ba0 copies WHOLESALE into Account+0x34 -- the object FUN_10006930
                    # reads. GetAccountInfo(297) only fires event 0x4b and never touches
                    # Account+0x34, so putting entitlements only on 297 can never unlock the gates.
                    # Names the client actually checks (cdb bp 0x6930): navigator ->
                    # "SubscriptionMember"/"Staff"/"WorldsApart"; create-match -> "RegisteredUser".
                    def _ent_stringlist(strs):  # ValueData classid22 ver1 mTypeID7=StringList ownRef1 N strs
                        return (enc_int(22) + enc_int(1) + enc_int(7) + enc_int(1)
                                + enc_int(len(strs)) + b"".join(enc_str(s) for s in strs))
                    _ENTITLEMENTS = dbmod.load_entitlements(dbc, ACCT)  # per-account, from the DB
                    # NB: "Staff"/"WorldsApart" (admin/special modes) are granted explicitly, not by default.
                    # Property 1: entitlements @ 0xfc5. Property 2 (when the account has scenario completions):
                    # the campaign-tree map @ 0x1054. BOTH must ride on THIS 114 propset -- FUN_10192ba0 copies
                    # it wholesale into Account+0x34, the container the campaign tree (FUN_10068cd0) reads. The
                    # 297 GetAccountInfo never touches Account+0x34, so 0x1054 there is inert (that was the bug).
                    _iprops = enc_int(0xfc5) + _ent_stringlist(_ENTITLEMENTS)
                    _inp = 1
                    if not os.environ.get("SWGTCG_NO_CAMPAIGN_REPLAY"):
                        try:
                            _icomp = dbmod.load_scenario_completion(dbc, ACCT)
                        except Exception:
                            _icomp = {}
                        if _icomp:
                            # (a) flat per-node type-2 property = the UNLOCK state the tree reader
                            # (FUN_00882e50 -> account.getProperty(node), mTypeID==2) actually checks.
                            for _nkey, _nval in build_node_props(_icomp):
                                _iprops += enc_int(_nkey) + _nval
                                _inp += 1
                            # (b) 0x1054 nested map = the completion detail (difficulties/archetypes).
                            _iprops += build_prop_0x1054(_icomp)
                            _inp += 1
                            log(port, n, "-> campaign on IntroduceAccount(114): %d node(s) (flat unlock props + 0x1054)" % len(_icomp))
                    ent_propset = (enc_int(0) + enc_int(23) + enc_int(1)          # PRESENT PropertySet(23) v1
                                   + enc_int(_inp) + _iprops)
                    # base id (AccountCommand+0x4, read by FUN_10009fc0) MUST equal ACCT: the
                    # execute (FUN_1000fe40) looks up/creates the account by THIS id and merges
                    # the PropertySet into it; login keys the local account by ACCT, so a mismatch
                    # (old value 0) parked the entitlements on a throwaway account-0.
                    intro_body = env(114, 2) + enc_int(ACCT) + enc_str(username) + ent_propset + enc_int(ACCT)
                    intro = frame(intro_body)
                    conn.sendall(intro)
                    log(port, n, "-> IntroduceAccount(114) acct=%d (%d B): %s" % (ACCT, len(intro), hexdump(intro)))
                    time.sleep(0.05)
                    # LoginCommand_Results(81): 3 levels (LoginCmd_Results:LoginCommand:NetworkCommand)
                    res = frame(env(81, 3) + enc_int(1) + enc_str("") + enc_int(ACCT))
                    conn.sendall(res)
                    log(port, n, "-> LoginCommand_Results(81) acct=%d (%d B): %s" % (ACCT, len(res), hexdump(res)))
                    # BISECT: minimal login = IntroduceAccount + Results only, skip all optional pushes. Used to
                    # find which pushed lobby command crashes the FinalLive DLL deserializer.
                    if os.environ.get("SWGTCG_MINIMAL_LOGIN"):
                        log(port, n, "MINIMAL_LOGIN: bare login only (skipping News/MOTD/AddGroups/collection/decks/291)")
                        continue
                    # ============================================================
                    # POST-LOGIN STREAM. Push commands so the client stops looping
                    # and shows the Casual Games menu.
                    #
                    # NetworkCommand_EULA (classid 456). deserialize = FUN_10154f20:
                    #   begin(classid,ver) -> string(+1) -> int(+0x20) -> int(+0x24)
                    #   -> IF ver>=3: nested(+0x28) + nested(+0x34,+0x40). We send ver=1
                    #   so the nested fields are SKIPPED (the `unaff_EDI < 3` branch goes
                    #   straight to end). Only ONE begin in the whole deserialize (no
                    #   parent-chain like Login_81), so envelope DEPTH = 1.
                    # run = FUN_101535e0 fires UI event 0xad with (string, int1, int2) ~
                    #   EULA text + version/required-version. Send empty/0,0 = nothing to
                    #   accept, so the client should proceed.
                    SEND_EULA = False  # the EULA push shows a modal Decline/Accept dialog over
                    # the lobby; test whether the client still reaches the lobby without it.
                    if SEND_EULA:
                        time.sleep(0.05)
                        eula = frame(env(456, 1) + enc_str("") + enc_int(0) + enc_int(0))
                        conn.sendall(eula)
                        log(port, n, "-> NetworkCommand_EULA(456) (%d B): %s" % (len(eula), hexdump(eula)))
                    #
                    # NetworkCommand_News (classid 457). deserialize = FUN_10157980:
                    #   begin(457,ver) -> vec<string>(+1) -> vec<string>(+5) -> vec<int>(+9) -> end.
                    #   Single begin (no parent-chain) => DEPTH = 1. Each vector wire =
                    #   [int count][elements]; empty vector = enc_int(0). run = FUN_10157ab0
                    #   iterates the 3 parallel arrays (headline/body/id). Empty = no news,
                    #   client just has nothing to show in the ticker.
                    time.sleep(0.05)
                    try:
                        news_items = dbmod.active_news(dbc, limit=20)   # [(id, headline, body), ...] from the DB
                    except Exception as e:
                        news_items = []
                        log(port, n, "-> news load FAILED: %s (sending empty)" % e)
                    news = build_news(news_items)
                    if not os.environ.get("SWGTCG_NO_NEWS"):
                        conn.sendall(news)
                        log(port, n, "-> NetworkCommand_News(457) %d item(s) (%d B): %s"
                            % (len(news_items), len(news), hexdump(news)))
                    #
                    # LoginCommand_DisplayMOTD(309) -- push a message-of-the-day if an admin set one.
                    # Empty MOTD => no push => the login flow stays byte-identical to before (safe default).
                    if PUSH_MOTD:
                        try:
                            motd_txt = dbmod.get_motd(dbc)
                        except Exception:
                            motd_txt = ""
                        if motd_txt:
                            time.sleep(0.05)
                            motd = build_displaymotd(motd_txt)
                            conn.sendall(motd)
                            log(port, n, "-> LoginCommand_DisplayMOTD(309) %r (%d B): %s"
                                % (motd_txt[:40], len(motd), hexdump(motd)))
                    #
                    # NetworkCommand_LeaderBoardData(458) -- OFF by default (unproven at login). When enabled,
                    # push the top standings as (rank, name, rating) rows in the primary board.
                    if PUSH_LEADERBOARD:
                        try:
                            top = dbmod.leaderboard_top(dbc, limit=50)
                            rows = [(e["rank"], e["name"], e["rating"]) for e in top]
                        except Exception as e:
                            rows = []
                            log(port, n, "-> leaderboard load FAILED: %s (sending empty)" % e)
                        time.sleep(0.05)
                        # Populate BOTH boards with the standings: the deserializer reads two (int,name,int)
                        # groups and which one the client DISPLAYS can't be told from the reader alone (RE agent
                        # ad71061b). Filling both means whichever renders has data (live-test disambiguation).
                        lb = build_leaderboard(rows, rows)
                        conn.sendall(lb)
                        log(port, n, "-> NetworkCommand_LeaderBoardData(458) %d row(s) x2 boards (%d B): %s"
                            % (len(rows), len(lb), hexdump(lb)))
                    #
                    # AccountCommand_GetAccountInfo(297) -- FIXED. deserialize FUN_1000b5e0 =
                    #   297-begin -> acctbase(begin+int=ACCT) -> subobj(PRESENT empty PropertySet)
                    #   -> [ver>1: int] -> end. DEPTH=2, ver=1. Uses the corrected `propset`
                    #   (leads with enc_int(0)=PRESENT) so it consumes EXACTLY its frame (the old
                    #   enc_int(1)=NULL left 4 stray bytes -> 2nd first-chance exception).
                    BISECT_297 = not os.environ.get("SWGTCG_NO_ACCTINFO")
                    if BISECT_297:
                        time.sleep(0.05)
                        # AccountInfo PropertySet carrying the ENTITLEMENT StringList at attribute 0xfc5 (4037).
                        # The client gates Casual Games / Scenarios / Tournaments / Guilds on membership in
                        # this list (FUN_10006930 reads attr 0xfc5 of account+0x34 = this PropertySet). An
                        # empty PS -> every check fails -> the buttons are dead. RegisteredUser unlocks
                        # online/Casual + Guilds; ScenarioEnabled etc. unlock Scenarios. (RE agent ac0aa471.)
                        def _value_stringlist(strs):  # ValueData: classid22, ver1, mTypeID7=StringList, ownRef1, N, strs
                            return (enc_int(22) + enc_int(1) + enc_int(7) + enc_int(1)
                                    + enc_int(len(strs)) + b"".join(enc_str(s) for s in strs))
                        _ents = dbmod.load_entitlements(dbc, ACCT)
                        # Entitlements StringList @ 0xfc5. (Campaign 0x1054 rides the 114 IntroduceAccount, not
                        # here -- 297 never writes Account+0x34, so a property here can't reach the campaign tree.)
                        sub = (enc_int(0) + enc_int(23) + enc_int(1)        # PRESENT PropertySet(23) v1
                               + enc_int(1) + enc_int(0xfc5) + _value_stringlist(_ents))
                        acctinfo = frame(env(297, 2) + enc_int(ACCT) + sub)
                        conn.sendall(acctinfo)
                        log(port, n, "-> AccountCommand_GetAccountInfo(297) acct=%d (%d B): %s" % (ACCT, len(acctinfo), hexdump(acctinfo)))
                    #
                    # LobbyCommand_AddGroups(94) -- the Casual Games room tree (menu populator).
                    #   deserialize FUN_101211e0: 94-begin -> LobbyCommand base FUN_1011f130
                    #   (begin + netbase begin + 1 int) -> group vector FUN_10121140 -> end.
                    #   DEPTH=3. payload = env(94,3) + enc_int(baseInt) + enc_int(N) + N*group.
                    #   Each group element (factory class Lobby=103, deser FUN_1011e500, ver=1) =
                    #     enc_int(103)[classid, peeked by factory + read by begin] + enc_int(1)[ver]
                    #     + 5 PRESENT PropertySets + 5 empty vec<int> role lists
                    #     + enc_int(contain) + enc_int(GID) + enc_int(eLobbyTypeID).
                    #   run FUN_101206a0 fires per-group event 3 (+ batch 0x39/0x3a when N>=2,
                    #   + per-account 0x43) carrying eLobbyTypeID -> SWLobbyTranslator turns it
                    #   into the Casual-Games rows.
                    def lobbygroup(contain, gid, typ):
                        # classid ONCE (the factory caf0 read is a peek; begin re-reads it).
                        return (enc_int(103) + enc_int(1)
                                + present_propset * 5 + enc_int(0) * 5
                                + enc_int(contain) + enc_int(gid) + enc_int(typ))
                    # AddGroups now deserializes + runs cleanly (classid-once fix). Send 2 groups
                    # (GID 100,101, eLobbyTypeID Standard=1) so the batch events 0x39/0x3a fire too.
                    BISECT_ADDGROUPS = not os.environ.get("SWGTCG_NO_ADDGROUPS")
                    NGROUPS = 2
                    if BISECT_ADDGROUPS:
                        time.sleep(0.05)
                        groups = b"".join(lobbygroup(0, 100 + i, 1) for i in range(NGROUPS))
                        addgroups = frame(env(94, 3) + enc_int(0) + enc_int(NGROUPS) + groups)
                        conn.sendall(addgroups)
                        log(port, n, "-> LobbyCommand_AddGroups(94) %d groups (%d B): %s" % (NGROUPS, len(addgroups), hexdump(addgroups)))
                    # NOTE: campaign progress is restored via property 0x1054 inside the 297 reply above
                    # (the real mechanism), NOT by replaying 415/487 frames -- those are client-side no-ops.
                    # ---- TOURNAMENT LOBBY (gated + DEFERRED; RE agent ad4dbec9 + create-gate RE) ----
                    # The display=5 tournament screen needs a gid=5 (==display id) type-5 group + SetTournament
                    # (299)/UpdateTournament(293) so FUN_10153800(5) resolves and the models populate. BUT that
                    # type-5 group in the lobby model + SetTournament's active-tournament manager state BREAK the
                    # casual Create dialog (create-gate RE) -- so we do NOT push it at a casual login. Fresh session
                    # -> allow re-push; only push now if this login lands on the tournament screen. The Tournaments
                    # tab click pushes it on demand (dispatch cid==92). Casual logins stay byte-clean -> Create works.
                    TOURNEY_GROUP_SENT.discard(ACCT)
                    if PUSH_TOURNAMENTS and LOGIN_SCREEN == "tournaments":
                        _push_tourney_lobby(conn, dbc, ACCT, port, n)
                    # Open-match snapshot: advertise existing joinable rooms (captured atomically with the
                    # LOBBY registration above) so this client can quick-join a match created before it
                    # logged in. introduce(owner) must precede the advertise (Join's account lookup).
                    for mid_s, owner_s, name_s, advert in open_matches_snapshot:
                        try:
                            conn.sendall(build_introduce(owner_s, NAMES.get(owner_s, "player%s" % owner_s)))
                            conn.sendall(advert if advert else build_match_advertise_clean(mid_s, name_s))
                        except Exception:
                            pass
                    if open_matches_snapshot:
                        log(port, n, "-> open-match snapshot to acct=%d: %d room(s) %s"
                            % (ACCT, len(open_matches_snapshot), [m[0] for m in open_matches_snapshot]))
                    # ---- STARTER GIFT: owned cards (collection sync) + a selectable online deck ----
                    # Give a new account its starter: push the owned-card collection (84->85->86,
                    # type 1=online/owned) then the online-deck list (363) so the deck appears and is
                    # selectable in Create. The picker requires the deck's cards to be OWNED, so the
                    # collection MUST be delivered first.
                    # CONFIRMED end-to-end on the live SWGTCG.dll under cdb (no assert/AV, clean exit):
                    #   84 deser+run -> 85 deser+run -> 86 deser+run (collection-ready 0x1d) ->
                    #   363 deser -> 100a4ed0 EQDeck(80002) factory-create OK -> 363 run (populate 0xc2);
                    #   the client then ACKs with SynchronizationCommand_Update(87). Set False to disable.
                    # SEAM D: per-account COLLECTION from the DB. A new account with no cards gets a
                    # starter seeded on the spot (the launcher normally seeds at create-time; this is the
                    # safety net for accounts made elsewhere, e.g. future web registration). The picker
                    # requires the deck's cards OWNED, so the collection is delivered before the decks (363).
                    # NB: the collection/deck push is GUARDED so a per-account data error can NEVER abort the
                    # post-login push before ChangeLobbyDisplay(291) below -- otherwise the client hangs at the
                    # MOTD screen and trips the 60s "no response from server" timeout (the lobby must open even
                    # if a deck fails to build).
                    GIVE_STARTER = not os.environ.get("SWGTCG_NO_STARTER")
                    if GIVE_STARTER:
                        try:
                            coll = dbmod.load_collection(dbc, ACCT)
                            if not coll:
                                dbmod.seed_starter(dbc, ACCT)
                                coll = dbmod.load_collection(dbc, ACCT)
                            for fr in build_collection_sync(ACCT, coll):
                                time.sleep(0.05); conn.sendall(fr)
                            log(port, n, "-> collection sync acct=%d: %d kinds / %d owned instances"
                                % (ACCT, len(coll), sum(q for _, q in coll)))
                        except Exception as e:
                            log(port, n, "-> collection sync FAILED acct=%s: %s (continuing to lobby)" % (ACCT, e))
                        time.sleep(0.1)
                        # SEAM E: per-account DECKS from the DB. Each deck row -> build_eqdeck_subobject
                        # with its main/avatar/quests; the avatar Card sub-object (+0xac, v10) is replayed
                        # from the captured starter avatar so a 55-card deck delivers byte-correct. Each deck
                        # is built UNDER ITS OWN try so one bad deck can't drop the others (or block the lobby).
                        try:
                            db_decks = dbmod.load_decks(dbc, ACCT)
                        except Exception as e:
                            db_decks = []
                            log(port, n, "-> load_decks FAILED acct=%s: %s" % (ACCT, e))
                        decks, names = [], []
                        for d in db_decks:
                            try:
                                avsub = load_avatar_subobj(d["avatar"]) if d["avatar"] else b""
                                # Pass the friendly NAME as the EQDeck's id field too: the lobby deck-picker
                                # displays that first string (the user saw the raw wire id "deck_5"). Both the
                                # id and name fields carry the friendly name now.
                                decks.append(build_eqdeck_subobject(d["name"], d["name"],
                                             d["main"], d["avatar"], d["quests"], avsub))
                                names.append(d["name"])
                            except Exception as e:
                                log(port, n, "-> deck build FAILED acct=%s deck=%r: %s (skipped)" % (ACCT, d.get("name"), e))
                        if decks:
                            try:
                                conn.sendall(build_populate_online_decks(decks))
                            except Exception as e:
                                log(port, n, "-> deck push FAILED acct=%s: %s" % (ACCT, e))
                        log(port, n, "-> online decks(363) acct=%d: %d deck(s) %s" % (ACCT, len(decks), names))
                    PROBE_JOIN = False
                    if PROBE_JOIN:
                        time.sleep(0.05)
                        join = frame(env(92, 3) + enc_int(ACCT) + enc_int(100) + enc_int(0))
                        conn.sendall(join)
                        log(port, n, "-> LobbyCommand_Join(92) acct=%d grp=100 (%d B): %s" % (ACCT, len(join), hexdump(join)))
                    # ChangeLobbyDisplay(291): run fires event 0x46 -> WALobbyManager builds+shows
                    # WALobbyScreen (the menu). Now the lobby model HAS groups, so the build's group
                    # lookup (FUN_1013a580) finds GID 100. env(291,3) + baseInt + displayId.
                    PROBE_CLD = True
                    if PROBE_CLD and not os.environ.get("SWGTCG_NO_LOBBY_BUILD"):
                        time.sleep(0.1)
                        # Which screen the client lands on at login. Default = casual (display=100). Set
                        # SWGTCG_LOGIN_SCREEN=tournaments to land on the tournament lobby (display=5, the
                        # hardcoded special case that builds tournamentlobby.ui) -- a guaranteed proof the
                        # tournament screen renders + SetTournament(299) populates it, bypassing the button gate.
                        login_disp = 5 if LOGIN_SCREEN == "tournaments" else 100
                        cld = frame(env(291, 3) + enc_int(0) + enc_int(login_disp))
                        conn.sendall(cld)
                        log(port, n, "-> LobbyCommand_ChangeLobbyDisplay(291) display=%d (%d B): %s" % (login_disp, len(cld), hexdump(cld)))
                    # LOBBY_ONLY: populate the match browser -- add match-type groups (type=6) under the display
                    # WITHOUT joining the client, so "Matches: N" shows + the client can Create/Quick-Join.
                    if os.environ.get("SWGTCG_LOBBY_ONLY"):
                        time.sleep(0.2)
                        for _gid in (200, 201):
                            mg = frame(env(94, 3) + enc_int(0) + enc_int(1) + lobbygroup(100, _gid, 6))
                            conn.sendall(mg)
                            log(port, n, "-> LOBBY_ONLY: match group gid=%d type=6 under 100, no join (%d B)" % (_gid, len(mg)))
                    # SUCCESS = host.log GetWindows count>0 / "Star Wars Galaxies TCG" vis=1.
                    # ============================================================
                    # EXPERIMENT: push a synthetic "player joined" via LobbyCommand_Join(92) v4 to
                    # populate the lobby (Users/Matches > 0) WITHOUT needing client UI interaction.
                    # Join(92) v4 fields (deser FUN_10145b90, run FUN_10145290): account(base int),
                    # group, field3, name string (v2), int-vector (v3), string + PRESENT PropertySet
                    # + int (v4). The run asserts the PropertySet present + account valid, and self-
                    # joins iff account==local(1); use account 2 (a remote synthetic player).
                    PUSH_JOIN = (not MULTI_CLIENT) and not LAUNCH_EQ   # single-client only: fake remote player. Multi-client
                    # relies on the real clients' own Joins (relayed between them). Disabled under LAUNCH_EQ (the
                    # real match-controller render flow drives the client's OWN match -- the auto-push conflicts).
                    if PUSH_JOIN:
                        time.sleep(0.2)
                        rj = build_join(2, 100, "TestBot")
                        conn.sendall(rj)
                        log(port, n, "-> PUSH Join(92) remote player TestBot acct=2 grp=100 (%d B)" % len(rj))
                    PUSH_SELF_JOIN = False  # tested: self-join (acct=1) to a CATEGORY group is accepted
                    # (no crash) and the client re-sends ChangeStatus, but it does NOT switch screens —
                    # entering a match needs a match-type group + the match-controller flow (see
                    # GAME-PROTOCOL.md). Kept here for reference / the next layer's experiments.
                    if PUSH_SELF_JOIN:
                        time.sleep(0.3)
                        sj = build_join(1, 100, "tester")
                        conn.sendall(sj)
                        log(port, n, "-> PUSH Join(92) SELF acct=1 grp=100 (%d B): %s" % (len(sj), hexdump(sj)))
                    PUSH_MATCH = (not MULTI_CLIENT) and not LAUNCH_EQ and not os.environ.get("SWGTCG_LOBBY_ONLY")  # single-client only. Multi-client / LAUNCH_EQ: the client creates/joins its own match.
                    # then self-join it -> the screen builder's cat-3/type-6 branch + match-controller
                    # flow should transition the client into a match (vs the type-1 category no-op).
                    # Observe cdb: BUILDSCREEN (new cat) / wa_assert (missing match data) / crash.
                    if PUSH_MATCH:
                        time.sleep(0.3)
                        mg = frame(env(94, 3) + enc_int(0) + enc_int(1) + lobbygroup(100, 200, 6))
                        conn.sendall(mg)
                        log(port, n, "-> PUSH AddGroups match group gid=200 type=6 in 100 (%d B): %s" % (len(mg), hexdump(mg)))
                        time.sleep(0.2)
                        mj = build_join(1, 200, "tester")
                        conn.sendall(mj)
                        log(port, n, "-> PUSH Join(92) SELF into match 200 (%d B): %s" % (len(mj), hexdump(mj)))
                        # The opponent must also join the MATCH: the game (262) has 2 players, so the lobby
                        # match needs 2 or the client waits at status 0x05 ("opponent not here") and never
                        # enters the gameScreen (0x0b). Join the bot (acct=2) into match 200 too.
                        time.sleep(0.2)
                        bj = build_join(2, 200, "TestBot")
                        conn.sendall(bj)
                        log(port, n, "-> PUSH Join(92) TestBot acct=2 into match 200 (%d B)" % len(bj))
                    # ── CAPTURE-REPLAY EXPERIMENT (SWGTCG_TEST_CAPTURE) ─────────────────────────────────────
                    # THE WIN PATH: replay the REAL captured running game (real_wire_game.bin -- a COMPLETE game:
                    # 2 EQPlayers, 211 populated PlayElements/cards, GameTurn + 9-deep StateMachine) with the
                    # embedded strict-checked EQ classids remapped to base (build_capture_remap: 80003->166,
                    # 80004->232 GameTurn, 80006->233 SM; the length-prefixed buffers ride along verbatim). The
                    # capture's internal gameid=1, so LaunchGame(match=1)+262(gameid=1). A real running game the
                    # base reader deserializes SHOULD make SWGameScreen draw the real board. WATCH wa_error.log:
                    # further than the from-scratch base? which embedded class asserts next (remap it too)?
                    TEST_CAPTURE = (not MULTI_CLIENT) and os.environ.get("SWGTCG_TEST_CAPTURE", "0") != "0"
                    if TEST_CAPTURE:
                        time.sleep(0.3)
                        # RENDER ROUTE A (2026-07-02): the game screen's setGame slot (FUN_7f4240) looks up a
                        # LobbyService node keyed by the LaunchGame game id (REPLAY_GAMEID) and copies node+0x20 into
                        # screen+0x28 (the game the board draws). LIVE PROBE showed setGame(key=1) finds NO node ->
                        # screen+0x28=0 -> bounce. Register a lobby group UNDER that key so the lookup hits; watch
                        # probe_setgame_diag.cdb for whether the walk then resolves a game (or reveals the node shape).
                        if os.environ.get("SWGTCG_GAMEGROUP", "0") != "0":
                            _ggtyp = int(os.environ.get("SWGTCG_GAMEGROUP_TYPE", "6"))
                            # setGame walks node+0x1c (contain) then returns the stop-node's +0x20 (its gid).
                            # contain=REPLAY_GAMEID makes node-1's +0x1c=1 -> walk resolves SELF -> returns gid=1
                            # (the game id) into screen+0x28 (vs contain=100 which returned 100). Configurable.
                            _ggcontain = int(os.environ.get("SWGTCG_GAMEGROUP_CONTAIN", str(REPLAY_GAMEID)))
                            gg = build_addgroups([(_ggcontain, REPLAY_GAMEID, _ggtyp)])
                            conn.sendall(gg)
                            log(port, n, "-> [TEST_CAPTURE] AddGroups GAME-GROUP gid=%d type=%d contain=%d (= setGame key) (%d B)" % (REPLAY_GAMEID, _ggtyp, _ggcontain, len(gg)))
                            time.sleep(0.2)
                        # ROUTE A: SWGTCG_EQLAUNCH=1 sends EQMatchCommand_LaunchGame(80008) -> the client's own
                        # factory builds a RENDERABLE EQGame (FUN_00642020) instead of the headless base game.
                        _eq = os.environ.get("SWGTCG_EQLAUNCH", "0") != "0"
                        if _eq:
                            _d = int(os.environ.get("SWGTCG_EQLAUNCH_DEPTH", "4"))
                            lg = build_eq_launchgame(game_id=REPLAY_GAMEID, depth=_d)
                            conn.sendall(lg)
                            log(port, n, "-> [TEST_CAPTURE] EQMatchCommand_LaunchGame(80008) depth=%d match=%d (%d B) -- watch FUN_00642020" % (_d, REPLAY_GAMEID, len(lg)))
                            # then 116 to BUILD the game screen (FUN_560100 skips game creation since the EQ game
                            # already exists, but still fires UI event 0x11 -> WAGameScreen bound to OUR EQ game).
                            if os.environ.get("SWGTCG_EQ_BUILD_SCREEN", "1") != "0":
                                time.sleep(0.3)
                                lg116 = frame(env(116, 4) + enc_int(1) + enc_int(REPLAY_GAMEID))
                                conn.sendall(lg116)
                                log(port, n, "-> [TEST_CAPTURE] +LaunchGame(116) build-screen for existing EQ game (%d B)" % len(lg116))
                        else:
                            lg = frame(env(116, 4) + enc_int(1) + enc_int(REPLAY_GAMEID))
                            conn.sendall(lg)
                            log(port, n, "-> [TEST_CAPTURE] LaunchGame(116) match=%d (%d B)" % (REPLAY_GAMEID, len(lg)))
                        time.sleep(0.4)
                        try:
                            hmode = os.environ.get("SWGTCG_HYBRID", "0")
                            if os.environ.get("SWGTCG_NO262", "0") != "0":
                                # skip the 262 entirely -- for the fail-fast render injection (drive advanceTurn
                                # directly on the clean Route-A EQ game, no serialize needed).
                                raise StopIteration("NO262")
                            if hmode == "eq":
                                # THE RENDER BLOB: EQ(80003) envelope + EQ header + captured base body, built to
                                # match FUN_00636c80's double-begin read (workflow wcpis8xx0). For the Route-A EQ
                                # game this should deserialize to completion -> setup -> advanceTurn -> board.
                                _dxf = int(os.environ.get("SWGTCG_EQ_DXF", "0")); _dfd = int(os.environ.get("SWGTCG_EQ_DFD", "0"))
                                _ev = int(os.environ.get("SWGTCG_EQ_VER", "48")); _ic = int(os.environ.get("SWGTCG_EQ_INNER", "80003"))
                                blob = build_eq_80003_blob(eq_ver=_ev, inner_classid=_ic, dxf=_dxf, dfd=_dfd); tag = "EQ-80003-HEADER"
                            elif hmode == "real":
                                # REAL captured EQ(80003) game verbatim. CRASHED the base reader (classID 166
                                # assert) but now that Route A(80008) made the game EQ, the EQ reader FUN_636c80
                                # should accept the native 80003 format -> full EQ state -> advanceTurn -> render.
                                blob = build_real_game_replay(); tag = "REAL-EQ-80003"
                            elif hmode == "2":
                                blob = build_capture_hybrid2(); tag = "HYBRID2"
                            elif hmode == "3":
                                # BASE render game. EXPERIMENT A (SWGTCG_STARTCMD=setup): the render gate
                                # FUN_004f3130 (SetupGame's mGame->vt+0x24) requires GameIsSetup==0 on entry,
                                # GameStarted(game+0x108)==0, and mGameIsReadyForStart(game+0x169)==1 -> then it
                                # sets GameIsSetup=1 and calls advanceTurn -> StartOfGame -> render.
                                _setup = os.environ.get("SWGTCG_STARTCMD", "") == "setup"
                                blob = build_base_render_game(accounts=(1, 2), game_id=REPLAY_GAMEID,
                                    game_started=(False if _setup else None),
                                    game_is_setup=(False if _setup else None),
                                    ready_for_start=(True if _setup else None)); tag = "BASE"
                            elif hmode != "0":
                                blob = build_capture_hybrid(); tag = "HYBRID"
                            else:
                                blob = build_capture_remap(); tag = "REMAPPED"
                            conn.sendall(build_sendserializedgame(blob, game_id=REPLAY_GAMEID))
                            log(port, n, "-> [TEST_CAPTURE] SendSerializedGame(262) %s real capture %dB gid=%d -- watch board + wa_error.log" % (tag, len(blob), REPLAY_GAMEID))
                        except Exception as e:
                            log(port, n, "-> [TEST_CAPTURE] build FAILED: %s" % e)
                        # DISPLAY trigger: the 262-bulk fires no cardIntroduced events -> empty board. Now that
                        # the game is loaded+stable in-game, fire IntroduceCard(56) for REAL EQCard element ids
                        # (forcedIDs that getOrCreateCardForForcedID can FIND -- unlike the earlier bogus 267554).
                        if os.environ.get("SWGTCG_INTRO", "0") != "0":
                            time.sleep(0.5)
                            real_cards = [1000010, 1000011, 1000012, 1000013, 1000014]  # capture EQCard elem ids
                            for _cid in real_cards:
                                conn.sendall(build_gamecommand_introducecard(player_id=1, game_id=REPLAY_GAMEID, card_id=_cid))
                                time.sleep(0.15)
                            log(port, n, "-> [TEST_CAPTURE] IntroduceCard(56) x%d real EQCards %s -- watch board + wa_error.log" % (len(real_cards), real_cards))
                        # GAME-START trigger experiment: the online client goes in-game then HOLDS at the starscape,
                        # waiting for the server's game-start command. Try the game-start command family
                        # (SWGTCG_STARTCMD=117 ReadyForStartOfGame, 63 StateSpecificMessage, 73 SynchPoint) for both players.
                        _startcmd = os.environ.get("SWGTCG_STARTCMD", "")
                        if _startcmd == "117":
                            time.sleep(0.4)
                            for pid in (1, 2):
                                conn.sendall(build_gamecommand_readyforstartofgame(player_account=pid, game_id=REPLAY_GAMEID))
                            log(port, n, "-> [TEST_CAPTURE] ReadyForStartOfGame(117) for players 1,2 -- watch board")
                        elif _startcmd == "setup":
                            # EXPERIMENT A (workflow-derived): the render trigger is GameCommand_SetupGame(67),
                            # whose run calls mGame->vt+0x24 = FUN_004f3130 -> if mGameIsReadyForStart(game+0x169)==1
                            # -> advanceTurn -> StartOfGame -> render. Send 117 (arm the ready byte) BEFORE 67.
                            time.sleep(0.4)
                            conn.sendall(build_gamecommand_readyforstartofgame(player_account=1, game_id=REPLAY_GAMEID))
                            time.sleep(0.25)
                            conn.sendall(build_gamecommand_setupgame(game_id=REPLAY_GAMEID, player_count=2,
                                account_ids=(1, 2), player_order=(0, 1), my_player_id=1))
                            log(port, n, "-> [TEST_CAPTURE] EXPERIMENT A: ReadyForStartOfGame(117) + SetupGame(67) -- watch board + 'creating StartOfGame'")
                    PUSH_LAUNCHGAME = (not MULTI_CLIENT) and not TEST_CAPTURE and not LAUNCH_EQ and not os.environ.get("SWGTCG_LOBBY_ONLY")  # single-client only. MatchCommand_LaunchGame(116). Deser chain is
                    # LaunchGame->MatchCommand->LobbyCommand->Command = DEPTH 4; fields = baseInt + matchId.
                    # Run @10160100 creates the client-side Game (operator_new 0x380) + fires UI event 0x11
                    # -> WAMatchViewController should build the WAGameScreen. Watch cdb BUILDSCREEN/assert.
                    if PUSH_LAUNCHGAME:
                        time.sleep(0.3)
                        lg = frame(env(116, 4) + enc_int(1) + enc_int(200))
                        conn.sendall(lg)
                        log(port, n, "-> PUSH MatchCommand_LaunchGame(116) match=200 (%d B): %s" % (len(lg), hexdump(lg)))
                    # ── CARD-DISPLAY EXPERIMENT (SWGTCG_TEST_CARDS) ─────────────────────────────────────────
                    # RE'd 2026-07-01: the board = SWGameScreen (cat 11, built by LaunchGame above). It shows cards
                    # via the cardIntroduced EVENT that GameCommand_IntroduceCard(56) fires (game vt+0xc0 FUN_4f5550
                    # -> GameTurn::getCurrentStateMachine -> SM->dispatch(2,{player,card}) -> SWGameScreen draws).
                    # A 262-BULK load reconstructs card DATA but fires NONE of those events -> empty board (the live
                    # symptom). This streams the DISPLAY path instead: base-262 (for a GameTurn(232)+StateMachine
                    # (233)) -> SetupGame(67) -> SetPlayer(65)x2 -> IntroduceCard(56)x2. WATCH E:\SWGTCG\TCGStandalone\
                    # wa_error.log + the board: if a card DRAWS, the display path works (PvP board cracked). If it
                    # asserts "mCurrentTurn"/"mCurrentStateMachine", the runtime "current" pointers (game+0x64 /
                    # GameTurn+0x18) aren't wired by 262 -> next = RE how the skirmish sets them without advanceTurn.
                    # Single-client only (MULTI_CLIENT=0); skips the old minimal-blob PUSH_SERGAME below.
                    TEST_CARDS = (not MULTI_CLIENT) and os.environ.get("SWGTCG_TEST_CARDS", "0") != "0"
                    if PUSH_LAUNCHGAME and TEST_CARDS:
                        PUSH_SERGAME = False   # use the base blob here, not the minimal one below
                        time.sleep(0.35)
                        blob = build_base_render_game(accounts=(1, 2), game_id=200)
                        conn.sendall(build_sendserializedgame(blob, game_id=200))
                        log(port, n, "-> [TEST_CARDS] SendSerializedGame(262) base blob (GameTurn+SM) %dB" % len(blob))
                        time.sleep(0.35)
                        conn.sendall(build_gamecommand_setupgame(game_id=200, account_ids=(1, 2)))
                        conn.sendall(build_gamecommand_setplayer(player_id=1, game_id=200))
                        conn.sendall(build_gamecommand_setplayer(player_id=2, game_id=200))
                        log(port, n, "-> [TEST_CARDS] SetupGame(67)+SetPlayer(65)x2")
                        time.sleep(0.35)
                        for _ci in (267554, 267554):
                            conn.sendall(build_gamecommand_introducecard(player_id=1, game_id=200, card_id=_ci))
                        log(port, n, "-> [TEST_CARDS] IntroduceCard(56)x2 player=1 card=267554 -- watch board + wa_error.log")
                    # PHASE C harness: SendSerializedGame(262). FRAMING TEST first — push a trivial blob
                    # to confirm the 262 envelope + zlib wrapper reach Game::deserialize (the fault should be
                    # INSIDE Game::deserialize parsing the bytes, not in the framing/zlib). Then the real
                    # serialized-game blob goes here once the serializer is built (see PORT-PLAN.md Phase C).
                    PUSH_SERGAME = (not MULTI_CLIENT) and not LAUNCH_EQ and not os.environ.get("SWGTCG_LOBBY_ONLY")  # single-client only. Multi-client / LAUNCH_EQ: server starts the game on both-ready.
                    if PUSH_LAUNCHGAME and PUSH_SERGAME:
                        time.sleep(0.3)
                        # Minimal Game::serialize (empty-board game) per the FUN_100fca50 build spec:
                        # envelope[166][1] + 22 header scalars + empty containers + markers 99999/666666
                        # + embedded GameTurn(232)+StateMachine(233) + v1 trailer. Target: "Game Reconstructed".
                        # Phase 2 increment 3 (link player->PlayerPlayArea so the UI reaches zones -> RENDER):
                        #  - item-33 LIST#1 now carries the player readFrom (third loop FUN_100fca50:636 ->
                        #    FUN_100d9400 player registry -> vt+0x24 Player::readFrom, NO cast). This sets
                        #    Player+0x04 WITHOUT the Game.cpp:0x2444 fault (the buffer was wrongly in LIST#2).
                        #  - _card_envelope now emits its 4 trailing v1 containers (Card.cpp:0x1185/0x118f/
                        #    0x11b8/0x11bd), killing the non-fatal card bad_alloc (truncated buffer -> unchecked
                        #    FUN_1000b2a0 count). The 174 envelope itself was already byte-correct.
                        # Board = PlayerPlayArea(174) root owning 3 PilePlayAreas(173) [DrawDeck/Discard/Hand]
                        # + 1 Card(168) in the Hand pile. player_data[0] links player 1 -> ppa 900010 + card.
                        # THE BLACK-BOARD ROOT CAUSE (RE workflow w861n1tjj): the gameScreen build creates one
                        # PlayerView per serialized player, then switches on the count with `add eax,-2; cmp
                        # eax,3; ja` -> it REQUIRES 2-5 players. With 1, the count underflows -> assert(false)
                        # at swgamescreen.cpp:10171 -> the per-player zone-placement loop is SKIPPED -> empty
                        # QGraphicsScene -> BLACK board (clean deserialize, no crash). So serialize BOTH players,
                        # each a PlayerPlayArea(174) + 3 piles(173) + a Card(168) in the Hand. Step 2 of the plan.
                        CATALOG = 267554
                        PPA1, DRAW1, DISCARD1, HAND1, CARD1 = 900010, 900011, 900012, 900013, 900014
                        PPA2, DRAW2, DISCARD2, HAND2, CARD2 = 900020, 900021, 900022, 900023, 900024
                        zones = [
                            {"id": PPA1, "classid": 174, "owner": 1, "pile_refs": [DRAW1, DISCARD1, HAND1]},
                            {"id": DRAW1, "classid": 173, "owner": 1},
                            {"id": DISCARD1, "classid": 173, "owner": 1},
                            {"id": HAND1, "classid": 173, "owner": 1},
                            {"id": CARD1, "classid": 168, "owner": 1, "parent": HAND1, "catalog_id": CATALOG},
                            {"id": PPA2, "classid": 174, "owner": 2, "pile_refs": [DRAW2, DISCARD2, HAND2]},
                            {"id": DRAW2, "classid": 173, "owner": 2},
                            {"id": DISCARD2, "classid": 173, "owner": 2},
                            {"id": HAND2, "classid": 173, "owner": 2},
                            {"id": CARD2, "classid": 168, "owner": 2, "parent": HAND2, "catalog_id": CATALOG},
                        ]
                        player_data = [
                            {"objid": 1, "ppa_id": PPA1, "card_id": CARD1},
                            {"objid": 2, "ppa_id": PPA2, "card_id": CARD2},
                        ]
                        blob = serialize_minimal_game(200, players=[1, 2], zones=zones, player_data=player_data)
                        sg = build_sendserializedgame(blob, game_id=200)
                        conn.sendall(sg)
                        log(port, n, "-> PUSH SendSerializedGame(262) minimal game blob (%d B raw, %d B frame)" % (len(blob), len(sg)))
                    # TEST: does the RECONSTRUCTED game (via 262) accept a GameCommand? The old forced
                    # SetupGame/SetPlayer (M13) crashed on a NON-reconstructed game; now the game is real.
                    # FINDING: the reconstructed game ACCEPTS GameCommands (no crash; client replies
                    # "SENT bytes"), but game-state cmds (IntroduceCard) need GameTurn::getCurrentStateMachine
                    # -> GameTurn.cpp:437 "mCurrentStateMachine" (null). So driving the game (Phase D) is
                    # GATED on a valid state machine (the 903 fix). Gated off to keep the stable recon base.
                    PUSH_RECON_CMD = False  # isolate the zone reconstruction test
                    if PUSH_LAUNCHGAME and PUSH_SERGAME and PUSH_RECON_CMD:
                        time.sleep(0.4)
                        sp = build_gamecommand_setplayer(player_id=1, game_id=200)
                        conn.sendall(sp)
                        log(port, n, "-> PUSH SetPlayer(65) on the RECONSTRUCTED game player=1 (%d B)" % len(sp))
                        time.sleep(0.3)
                        ic = build_gamecommand_introducecard(player_id=1, game_id=200, card_id=267554)
                        conn.sendall(ic)
                        log(port, n, "-> PUSH IntroduceCard(56) card=267554 on the RECONSTRUCTED game (%d B)" % len(ic))
                    # After LaunchGame created the client Game(200) + switched to gameScreen, push
                    # the GameCommand sequence to set up + seat a 2-player board. Safe steps on by
                    # default; deck/card steps gated off (need the Deck sub-object spec + live card
                    # elements). All require Game(200) to exist or the runs assert on null mGame.
                    # PUSH_GAME (force SetupGame+SetPlayer+Ready) is a CONFIRMED DEAD END: forced
                    # GameCommands can't build a real game. SetupGame::run -> Game::setup needs the game
                    # already "ready" (mGameIsReadyForStart, armed only by Game::advanceTurn, which NO
                    # network command issues) or it has no GameTurn/state machine; with a valid player id
                    # it proceeds into setup and AV-crashes. The client gets a real, started game ONLY via
                    # GameCommand_SendSerializedGame(262) (Game::deserialize builds players/cards/GameTurn/
                    # StateMachine from a zlib blob). Deck(46) is NEVER registered in the client factory
                    # (verified vs all 449 registered classids) so SelectDeckForPlayer w/ a Deck can't work.
                    # See GAME-PROTOCOL.md M13. Validated stable state = LaunchGame -> empty gameScreen.
                    PUSH_GAME = False  # SetupGame(67) on the reconstructed game CRASHES (tested) — same
                    # M13 dead-end; it does NOT create the Player objects in +0x24/+0x44. Gated off.
                    if PUSH_LAUNCHGAME and PUSH_GAME:
                        GID = 200; ACCTS = (1, 2)
                        time.sleep(0.2)
                        sg = build_gamecommand_setupgame(game_id=GID, match_id=GID,
                            player_count=len(ACCTS), player_order=tuple(range(len(ACCTS))), account_ids=ACCTS)
                        conn.sendall(sg)
                        log(port, n, "-> PUSH GameCommand_SetupGame(67) game=%d players=%s (%d B): %s" % (GID, ACCTS, len(sg), hexdump(sg)))
                        for pid in ACCTS:
                            time.sleep(0.2)
                            sp = build_gamecommand_setplayer(player_id=pid, game_id=GID)
                            conn.sendall(sp)
                            log(port, n, "-> PUSH GameCommand_SetPlayer(65) player=%d (%d B)" % (pid, len(sp)))
                        # SelectDeckForPlayer(58): assign each player a PRESENT Deck sub-object.
                        # build_deck_subobject (Deck classid 46) is RE-confirmed as the wire format,
                        # but pushing it currently CRASHES in ComponentFactory::create(46) (both DLLs,
                        # empty card list too) -> 46 may not be the factory-create classid for this
                        # context (concrete subclass? EQDeck=80002?). GATED OFF pending that RE.
                        PUSH_SELECT_DECK = False  # BLOCKED: "Couldn't get class 46" (Deck not in
                        # ComponentFactory at this point) -> null deck -> run crash. Needs the Deck
                        # class registered + the game state machine (deeper game-init). See GAME-PROTOCOL.md.
                        if PUSH_SELECT_DECK:
                            for pid in ACCTS:
                                time.sleep(0.2)
                                deck = build_deck_subobject(deck_id="deck_%d" % pid, deck_name="Deck %d" % pid)
                                sd = build_gamecommand_selectdeckforplayer(player_id=pid, game_id=GID,
                                    deck_id="deck_%d" % pid, deck=deck)
                                conn.sendall(sd)
                                log(port, n, "-> PUSH GameCommand_SelectDeckForPlayer(58) player=%d deck=46 (%d B)" % (pid, len(sd)))
                        for pid in ACCTS:
                            time.sleep(0.2)
                            rg = build_gamecommand_readyforstartofgame(player_account=pid, game_id=GID)
                            conn.sendall(rg)
                            log(port, n, "-> PUSH GameCommand_ReadyForStartOfGame(117) player=%d (%d B)" % (pid, len(rg)))
                        PUSH_DRAW = False         # needs live card-element ids
                        PUSH_INTRODUCE = False    # needs a live card id
                elif port == 16783 and replied:
                    dispatch(conn, n, fr, ACCT, dbc)
    except socket.timeout:
        log(port, n, "idle timeout")
    except Exception as e:
        log(port, n, "err %s" % e)
    finally:
        if ACCT is not None:
            with _clients_lock:
                LOBBY.pop(ACCT, None)                 # remove the dead socket FIRST (never send to it)
            if CONN_MATCH.get(ACCT) in MATCHES:       # owner-disconnect -> teardown; joiner-disconnect ->
                _remove_from_match(ACCT)              # drop only them, the owner's room survives + re-advertises
            with _clients_lock:
                READY.pop(ACCT, None); NAMES.pop(ACCT, None)
                LAST_STATUS.pop(ACCT, None); CONN_MATCH.pop(ACCT, None)
            log(port, n, "lobby account %d disconnected (%d connected)" % (ACCT, len(LOBBY)))
        try: dbc.close()
        except Exception: pass
        if cap: cap.close()
        conn.close()

def listen(port):
    s = socket.socket()
    # FAIL LOUD on a duplicate instance instead of silently fighting for the port. On Windows SO_REUSEADDR
    # lets TWO server processes bind the SAME port and connections go to EITHER unpredictably (the documented
    # "a stale server stole the connects" gotcha -> lobby won't open / 60s no-response). SO_EXCLUSIVEADDRUSE
    # prevents a 2nd instance from binding; a clear error then tells the user to kill the stale server.
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try: s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError: pass
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # POSIX: safe quick-restart
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        print("FATAL: cannot bind port %d (%s).\n"
              "       Another SWG TCG server (or stale python) is ALREADY running on this port.\n"
              "       Kill ALL python first, then restart:  taskkill /f /im python.exe" % (port, e))
        os._exit(1)
    s.listen(8)
    print("listening %d (%s)" % (port, "gateway" if port == 16782 else "lobby"))
    while True:
        c, a = s.accept()
        threading.Thread(target=handle, args=(c, a, port), daemon=True).start()

if __name__ == "__main__":
    _c = dbmod.connect(); dbmod.init_db(_c)
    if _c.execute("SELECT COUNT(*) FROM card_catalog").fetchone()[0] == 0:
        print("card_catalog seeded: %d cards" % dbmod.rebuild_card_catalog(_c))
    # Single-player standalone: guarantee the one StandAloneUser account + a fresh auto-login
    # session every boot (self-heals an expired/missing token or a swapped-in db).
    _sa = dbmod.ensure_standalone_account(_c)
    print("standalone account ready: id=%s (%s)" % (_sa, dbmod.STANDALONE_USERNAME))
    print("DB ready: %s (%d accounts)" % (config.DB_PATH,
          _c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]))
    _c.close()
    for p in (16782, 16783):
        threading.Thread(target=listen, args=(p,), daemon=True).start()
    if KEEPALIVE:
        threading.Thread(target=_keepalive_loop, daemon=True).start()
        print("KEEPALIVE: proactive Ping(112) every %.0fs -> resets client watchdog (ClientApp+0x5c)" % KEEPALIVE_IV)
    # Feature-flag banner -- so srv.log immediately shows which optional pushes are ACTIVE (a stale
    # flag-less server holding the ports is the classic reason a feature "doesn't work" -- see this here).
    print("FEATURE FLAGS: MOTD=%s LEADERBOARD=%s TOURNAMENTS=%s(type=%d) LOGIN_SCREEN=%s | BOARD launch=%s mode=%s board=%s gameover=%s boot(ready=%s,queue=%d)"
          % (PUSH_MOTD, PUSH_LEADERBOARD, PUSH_TOURNAMENTS, TOURNEY_LOBBYTYPE, LOGIN_SCREEN,
             LAUNCH_BOARD, LAUNCH_MODE, BOARD_MODE, BASE_GAMEOVER, BASE_READY_FOR_START, BASE_QUEUE_MODE))
    print("SWG TCG server up. Captures -> %s" % OUTDIR)
    while True:
        time.sleep(1)
