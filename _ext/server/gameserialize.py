"""Retail SWG TCG Game::serialize — the SendSerializedGame(262) blob (Phase C).

Emits the byte stream the retail client's Game::deserialize (FUN_100fca50) reads, so the client
reconstructs a game. This MINIMAL version yields a deserialize-ACCEPTING empty-board game (the natural
completion of the validated iterate-loop). Built from the FUN_100fca50 field spec:
  Game envelope DEPTH 1 [166][ver]; 22 header scalars (field#3 skipped at ver<0x19); empty object-graph
  containers; magic marker 99999; embedded mGameTurn(232) which embeds a PRESENT StateMachine(233)
  (required: mCurrentStateMachine must be non-null); StateMachine has magic marker 666666; then the
  version-1 Game trailer. All embedded gameids = the Game id (so FUN_100df070 resolves + getGameID asserts pass).
Returns RAW bytes; the caller (build_sendserializedgame) zlib-compresses + frames as 262.
"""
import os
from swgcodec import enc_int, enc_str

i = enc_int
s = enc_str

# Test sweep knobs (env-overridable; production defaults render a current state).
#   SM_STATE=0    -> empty state stack (no current state)
#   SM_CLASSID=N  -> the current state's classid (234 base StateMachineState, 374 PhaseState, ...)
WITH_STATE = os.environ.get("SM_STATE", "1") != "0"
STATE_CLASSID = int(os.environ.get("SM_CLASSID", "234"))


def _statemachine(game_id, ver=1, state_id=1, state_classid=234, with_state=True):
    # state_classid MUST be a ComponentFactory-registered StateMachineState subclass.
    # LIVE-build classids (verified via the factory registry REG4_trigger_*): 233 StateMachine,
    # 234 StateMachineState (REG4_trigger_101ac1f0 = 0xea). NOTE 366 = MacroNode (NOT PhaseState);
    # PhaseState's CID_374 filename is a stale label. Use 234 (base) for the minimal current-state.
    # MINIMAL CURRENT-STATE (stops the gameScreen bounce): the teardown predicate WAGameScreen vt55
    # FUN_103f49a0 closes the gameScreen when Game::hasCurrentState()==false (empty mStateStack) &&
    # !gameEnded(). hasCurrentState() walks mGameTurn(Game+0x64)->getCurrentStateMachine(GameTurn+0x18)
    # ->mStateStack top. So a NON-EMPTY stack with a factory-creatable state is enough; the per-state
    # buffer (vector#2) can be EMPTY -> the state is default-constructed, readFrom is NOT called -> we
    # avoid the huge StateMachineState/EvaluationEnvironment sub-graph. (RE agent a3c856a7.)
    b = i(233) + i(ver)                 # SM begin (DEPTH 1)
    b += i(game_id)                     # gameid -> owning Game (asserts non-null)
    # --- 3 SM vectors: mStateStackMap / names / mStateStack ---
    if with_state:
        # vector#1 mStateStackMap = FUN_1005c310 = count + (stateID, stateClassID) int-pairs
        #   (pair reader FUN_10177090 = 2 ints). The loop factory-creates each state by classID.
        # vector#2 state-buffers = FUN_100e2ec0 = count + (stateID, bufLen, buffer); EMPTY -> no
        #   readFrom -> default-constructed state (enough for hasCurrentState()).
        # vector#3 mStateStack = FUN_1000b2a0 = vector<int> of stateIDs; last = top = current state.
        b += i(1) + i(state_id) + i(state_classid)   # vector#1 mStateStackMap (stateID,stateClassID)
        b += i(0)                                     # vector#2 state-buffers (EMPTY = default-construct)
        b += i(1) + i(state_id)                       # vector#3 mStateStack (top -> current state)
    else:
        b += i(0) + i(0) + i(0)         # 3 empty vectors — PROVEN: empty game ACCEPTED (Phase-1)
    b += i(666666)                      # magic marker (logs if wrong; non-fatal)
    b += i(0)                           # FUN_101a90e0 vector (empty)
    b += s("") + s("")                  # +0x10, +0x17
    # ver<10 branch (FUN_101a9630): FUN_100975c0 PEEK (NON-advancing, confirmed: it reads param_1[3]
    # but never writes it, unlike FUN_10097500) + 2 strings. The peek consumes NO wire byte, so emit
    # ONLY the 2 strings -- a prior i(0) here over-emitted 1 byte, leaving a trailer leftover that the
    # final endRead (FUN_100fca50 line 1129 (**(*local_3c+0x14))(param_2)) would see.
    b += s("") + s("")                  # ver<10 branch: 2 strings (NO peek byte)
    b += s("") + s("")                  # +0x2a, +0x31
    b += i(0)                           # +0x3f
    # (ver>3 and ver>0xd blocks skipped at ver=1)
    return b


def _gameturn(game_id, ver=1, current_player=0, state_classid=None, with_state=None):
    if state_classid is None:
        state_classid = STATE_CLASSID
    if with_state is None:
        with_state = WITH_STATE
    b = i(232) + i(ver)                 # GameTurn begin (DEPTH 1)
    b += i(game_id)                     # gameid
    # field#2 = CURRENT-PLAYER id (GameTurn::readFrom FUN_10116100 @GameTurn.cpp:0x31e). If non-zero it
    # is resolved via FUN_100d9400 (the player registry, keyed by *(player+0x34)) and ASSERTS @0x322 if
    # it doesn't resolve. The captured real board had GameTurn+0x08 -> a real EQPlayer, so a rendered
    # board needs this set; players ride item-33 LIST#1 and register before this inline GameTurn runs.
    b += i(current_player)              # current-player id (0 = none; set to a real player objid)
    b += i(0) + i(0) + i(0)             # param_1[3],[4],[5]
    # mCurrentStateMachine: PRESENT sub-object via FUN_100d4a60. CRITICAL: the codec reads the
    # present-flag (enc_int 0=present) and then PEEKS the classid (FUN_100975c0 does NOT advance the
    # cursor); the child's begin re-reads+advances that same classid. So emit the classid ONCE (in the
    # SM begin), NOT a separate present-codec classid — dynamic byte-dump confirmed the cursor.
    b += i(0) + _statemachine(game_id, 1, state_classid=state_classid, with_state=with_state)  # ON (default): minimal current
    # state (mStateStack=[1], stateClassID 234) so Game::hasCurrentState()==true and the gameScreen
    # teardown predicate (FUN_103f49a0) keeps the board on screen instead of bouncing to the starscape.
    b += i(0)                           # FUN_101140e0 (+7) empty
    b += i(0)                           # param_1[10]
    b += i(0) + i(0)                    # FUN_101140e0 (+0xb),(auStack_20) empty
    b += i(0)                           # +0x11 bool
    b += i(0)                           # FUN_10063b50 (+0x2c) vector empty
    b += i(0) + i(0)                    # +0x15, +0x1a bool
    # (ver>5..ver>0x10 fields skipped at ver=1)
    return b


# PlayElement zone subclasses (Game::deserialize item 32 classids) + their areaType (+0x3c).
PLAYAREA = 172        # areaType 2 (a quest/board area)
PILEPLAYAREA = 173    # areaType 1 (hand/deck/discard pile)
PLAYERPLAYAREA = 174  # areaType 0 (the per-player root that owns the piles)
AREATYPE = {PLAYAREA: 2, PILEPLAYAREA: 1, PLAYERPLAYAREA: 0}
# begin DEPTH = inheritance depth (each level's deserialize calls begin). Dynamic dumps:
# 172 PlayArea -> PlayElement = 2 begins; 173 PilePlayArea -> PlayArea -> PlayElement = 3;
# 174 PlayerPlayArea -> PlayArea -> PlayElement = 3 (assumed). All begins use the LEAF classid.
ZONE_DEPTH = {PLAYAREA: 2, PILEPLAYAREA: 3, PLAYERPLAYAREA: 3}


def _childmap(contents):
    """PlayElement child-map F: {bucket: [elementIds]}. empty -> enc_int(0).
    {0:[cardId]} -> i(1)[buckets] i(0)[bucket] i(1)[listLen] i(cardId)."""
    if not contents:
        return i(0)
    b = i(len(contents))
    for bucket, ids in contents.items():
        b += i(bucket) + i(len(ids))
        for cid in ids:
            b += i(cid)
    return b


def _zone_envelope(classid, game_id, element_id, owner, parent=0, pile_refs=None, contents=None, ver=1,
                   areatype=None):
    """A PlayElement::readFrom (FUN_101842b0) envelope, read directly via the element's secondary
    vtable slot 9 (the item-33 loop looks the element up by id, so the begin classid is emitted ONCE,
    no present-codec peek). Field order is exact (PlayElement.cpp:0x1f8-0x229).
    areaType (+0x3c) is a PER-INSTANCE field (the gameScreen build groups a player's zones by it):
    real board uses 0=root(174), 1=pile(173), 2=PlayArea(172), 3=deck-pile(173). Pass `areatype` to
    override the per-classid default (a 173 pile can be areaType 1 OR 3)."""
    b = (i(classid) + i(ver)) * ZONE_DEPTH[classid]   # `depth` nested begins (all the leaf classid)
    b += i(game_id)                     # #A gameId
    b += i(element_id)                  # #B elementId
    b += i(0)                           # #C +0x38
    b += i(AREATYPE[classid] if areatype is None else areatype)   # #D areaType (+0x3c) -- per-instance
    b += i(parent)                      # #E parent element-ref (0 = none)
    b += _childmap(contents)            # #F child-map (FUN_10063b50): pile contents, e.g. {0:[cardId]}
    b += i(23) + i(1) + i(0)            # embedded mPropertySet (classid 23 + ver + empty map)
    b += i(owner)                       # owner player id (+0x40)
    if pile_refs is not None:           # PlayerPlayArea(174): 3 pile-refs (DrawDeck, Discard, Hand)
        for r in pile_refs:
            b += i(r)
    return b                            # end: no bytes


CARD = 168  # Card is a PlayElement subclass (Card -> PlayElement = 2 begins)


def _card_envelope(game_id, element_id, catalog_id, owner, parent, ver=1):
    """Card::deserialize (FUN_10065250, version 1): 2 begins(168) + PlayElement base fields +
    Card-specific fields. parent = the pile/zone the card lives in (e.g. the hand pile)."""
    b = (i(CARD) + i(ver)) * 2          # 2 begins: Card -> PlayElement (all leaf classid 168)
    b += i(game_id) + i(element_id)     # PE #A gameId, #B elementId
    b += i(0) + i(0)                    # PE #C +0x38, #D areaType (0 for a card)
    b += i(parent)                      # PE #E parent (the hand pile id)
    b += i(0)                           # PE #F child-map empty
    b += i(23) + i(1) + i(0)            # embedded mPropertySet (classid 23 + ver + empty map)
    b += i(owner)                       # PE #H owner
    b += i(catalog_id)                  # Card 0x1139 catalogId -> mArchetype lookup
    b += i(0)                           # Card 0x113d
    b += i(0) + i(0)                    # Card flags 0x1146, 0x114a
    b += i(0) + i(0) + i(0)             # Card vectors 0x1152, 0x1159, 0x1161 (empty)
    b += i(0)                           # Card attr-mod map (empty, Card.cpp:0x116f)
    # --- 4 trailing version-1 containers FUN_10065250 reads AFTER the attr-mod map (all ungated).
    # Omitting them left the card buffer SHORT: the first, FUN_1000b2a0 @0x1185, reads its count WITHOUT
    # checking read-success, so on an exhausted buffer it pre-allocates uninitialized-stack-garbage*4 bytes
    # -> std::bad_alloc with NO assert (the observed non-fatal "bad allocation"). Emitting these 4 empty
    # counts makes the card self-terminating so FUN_1000b2a0 never reads past the buffer end.
    b += i(0)                           # Card.cpp:0x1185  FUN_1000b2a0 vector (empty)  [bad_alloc source]
    b += i(0)                           # Card.cpp:0x118f  FUN_10064e60 map    (empty)
    b += i(0)                           # Card.cpp:0x11b8  FUN_1005c3b0 vector (empty)
    b += i(0)                           # Card.cpp:0x11bd  FUN_1000b2a0 vector (empty)  [also pre-allocates]
    return b


PLAYER_CLASSID = 261  # Player getClassId = 0x105 (mov eax,0x105;ret); the begin validates this


def _player_buffer(game_id, ppa_id, card_id, deck_id="", ver=1):
    """Player::readFrom (FUN_10186dc0) wire: begin(261) + 5 ints + PlayArea-id(->PlayerPlayArea,+0x04)
    + 1 int + mDeckID string + Deck sub-object(NULL) + PlayerCard-id(->Card,+0x30). The PlayArea-id and
    PlayerCard-id are LOAD-BEARING (assert Player.cpp:0x3de/0x3ee if they don't resolve+cast). Delivered as
    an item-33 entry (id=the player's objId): the item-33 loop calls getObjectById(id)->vt+0x24 with no cast,
    so it runs Player::readFrom on the shell player from item 28 -> links player->PlayerPlayArea -> UI reaches
    the zones -> board renders."""
    b = i(PLAYER_CLASSID) + i(ver)      # begin (classid 261 + version)
    b += i(0) * 5                       # ints #1-5 (flag, account, #3, prefLang, gender)
    b += i(ppa_id)                      # #6 PlayArea object-id -> PlayerPlayArea (+0x04) [LOAD-BEARING]
    b += i(0)                           # #7
    b += s(deck_id)                     # mDeckID string (+0x0c)
    b += i(1)                           # Deck sub-object = NULL (FUN_100a4ed0; avoids Deck-46)
    b += i(card_id)                     # #8 PlayerCard object-id -> Card (+0x30) [LOAD-BEARING]
    return b                            # end: no bytes


def serialize_minimal_game(game_id, ver=1, players=None, zones=None, player_data=None,
                           started=False, first_player=0, current_player=0, random_seed=0,
                           match_id=None, player_count=None,
                           state_classid=None, with_state=None, server_id=0):
    """Build the Game::deserialize blob.

    started=False  -> legacy minimal path: BYTE-IDENTICAL to the previous version (GameID field=0,
                      MatchID field=game_id, all flags 0). Keeps the single-client / Phase-C harness
                      paths unchanged.
    started=True   -> a *running* 2-player board templated on the captured real game (capture_game.log):
                      GameID set, mGameStarted=1, GameIsSetup=1, PlayerCount/OrigPlayerCount set,
                      mFirstPlayerID + GameTurn current-player set. (mGameStarted/GameIsSetup are plain
                      bool stores in Game::deserialize — no asserts; safe.)

    VERSION (ver): the 262 reader FUN_100fca50 gates EVERY field on this envelope version. The captured
    real renderable game was version 48 (the base Game getVersion FUN_100fa930 returns 0x32=50). Our
    default ver=1 SKIPS all version-gated reads -- and at ver>=0xf the structure DIVERGES (the GameTurn
    no longer embeds the StateMachine; the post-magic trailer carries many populated maps). Raising ver
    here only makes the HEADER version-coherent (ServerID added at ver>=0x19); the sub-objects + trailer
    are still v1-shaped, so ver>1 will NOT round-trip until those high-version fields are emitted. See
    the WIP note above build_2p_game. Kept as a param for the staged high-version rebuild.
    """
    G = game_id
    zones = zones or []
    player_data = player_data or []
    # Counts/IDs only diverge from the legacy zeros when started (keeps legacy bytes identical).
    pc = player_count if player_count is not None else (len(players) if (players and started) else 0)
    gid_field = G if started else 0          # field#1 GameID (legacy path historically emitted 0 here)
    mid = match_id if match_id is not None else (0 if started else G)  # field#2 MatchID (legacy: G)
    b = i(166) + i(ver)                 # Game envelope (DEPTH 1)
    # --- header scalars in EXACT Game::deserialize order (FUN_100fca50; ServerID field#3 only @ver>=0x19) ---
    b += i(gid_field)                   # 1  GameID            foo1.1 -> param_1[1] (FUN_100e1cf0 registers)
    b += i(mid)                         # 2  MatchID           foo1.2 -> +0x14
    if ver >= 0x19:
        b += i(server_id)               # 3  ServerID          param_1[3] -- ONLY read at ver>=0x19
    b += i(first_player)               # 4  mFirstPlayerID    foo1.3 -> +0x3d
    b += i(1 if started else 0)        # 5  mGameStarted      foo1.7 -> +0x3e (bool)
    b += i(0)                           # 6  mAIEnabled        foo1.8 -> +0x44 (bool)
    b += i(0)                           # 7  GameEnded         foo1.9 -> +0x111 (bool)
    b += i(0)                           # 8  OutOfSync         -> +0x4a (bool)
    b += i(0)                           # 9  BatchControlStatus-> +0x4b
    b += i(pc)                          # 10 PlayerCount       -> +0x4c
    b += i(pc)                          # 11 OrigPlayerCount   -> +0x4d
    b += i(1 if started else 0)        # 12 GameIsSetup       -> +0x56 (bool)
    b += i(0)                           # 13 ReadyForStart     -> +0x159 (bool)
    b += i(random_seed)                # 14 RandomSeed        -> +0x57
    b += i(0)                           # 15 Version           -> +0x59
    b += i(0)                           # 16 QueueMode         -> +0x5a
    b += i(0)                           # 17 displayState      -> +0x5b
    b += s("")                          # 18 MatchDirectory    -> +0x5c (string)
    b += i(0)                           # 19 RevealID          -> +0x69
    b += i(0)                           # 20 GameOver          -> +0x6d (bool)
    b += i(0)                           # 21 EventID           -> +0x71
    b += i(0)                           # 22 GetTargetID       -> +0x72
    # --- object-graph containers (items 23..33) ---
    b += i(0) + i(0)                    # items 23,24: vector<int>, empty
    # item 25: PLAYER OBJECT-ID LIST = vector<int> (FUN_1000b2a0, assert Game.cpp:0x23a5). THIS DRIVES
    # the player-creation loop (HELP_100fca50.c:382): for each id here, the loop looks it up in the item-28
    # map -> classId -> creates a Player (shell if classId 0) + setId + push into realGame+0x24. Emitting
    # this EMPTY is why no players were created. (RE: agent a4b9d813.)
    if players:
        b += i(len(players))
        for objid in players:
            b += i(objid)
    else:
        b += i(0)
    b += i(0) + i(0)                    # items 26,27: vector<int>, empty
    # item 28: objId->classId MAP = vector<(objId, classId)> (FUN_100c94c0). MUST BE EMPTY: the player loop
    # looks up each item-25 id here (FUN_100243e0); a MISS -> shell path (operator_new(0x4c)+FUN_10186a00,
    # a valid 0x4c-byte Player). A HIT with classId 0 makes the loop call ComponentFactory::create(0), which
    # is FATAL ("Couldn't get class 0" -> the always-throwing logger -> NULL-gameTurn crash). LIVE-CONFIRMED
    # via the factory-create trace (...116,262,0 -> FATAL). Leave empty for shell players. (RE agent a1a8bf58.)
    b += i(0)
    # item-33 LIST#1 (FUN_100e2ec0 -> Game local_30), read at FUN_100fca50:483 BEFORE item-32. This is the
    # PLAYER readFrom list: the THIRD item-33 loop (:636 / LAB_100fdaa0) resolves each objId via FUN_100d9400
    # (the PLAYER registry — Game+0x24/0x44 vectors keyed by *(player+0x34)), then calls vtable slot vt+0x24
    # (Player::readFrom) with NO RTTI cast. Players live ONLY in this list, NOT in LIST#2 (which the pass1/
    # pass2 element-only loops walk via FUN_100e1e30 -> assert Game.cpp:0x2444 on a non-element player id).
    # It runs LAST (after item-32 builds shells + LIST#2 readFroms run), so the PPA(174)+Card(168) the player
    # buffer points at are already in the element map and Player::readFrom's internal resolves (0x3de/0x3ee) pass.
    b += i(len(player_data))
    for pd in player_data:
        buf = _player_buffer(G, pd["ppa_id"], pd["card_id"], pd.get("deck_id", ""))
        b += i(pd["objid"]) + i(len(buf)) + buf
    b += i(0) + i(0)                    # items 30,31: FUN_100e2ff0 lists (Game+0x6a,+0x6e) — stay empty
    # item 32: PlayElement SHELLS = vector<(elementId, classId)> (FUN_1005c310). classId in {172,173,174}.
    # item 33 LIST#2 (FUN_100e2ec0 -> local_48): ELEMENT readFroms = vector<(elementId, bufLen, buffer)>. The
    # pass1 (cards) + pass2 (non-card PlayElements) loops resolve via FUN_100e1e30 (ELEMENT map only), so this
    # list holds ONLY zones/cards — NEVER players (a player id here faults Game.cpp:0x2444/0x2459).
    # item 32: element SHELLS = vector<(elementId, classId)> (created by classid; players are NOT here).
    b += i(len(zones))
    for z in zones:
        b += i(z["id"]) + i(z["classid"])
    # item 33 LIST#2: vector<(elementId, bufLen, buffer)>. pass1/pass2 resolve via FUN_100e1e30 (element map)
    # and cast to Card / PlayElement, so this list is ELEMENTS ONLY. buffer = that element's readFrom envelope.
    entries = []
    for z in zones:
        if z["classid"] == CARD:
            env = _card_envelope(G, z["id"], z["catalog_id"], z["owner"], z["parent"])
        else:
            env = _zone_envelope(z["classid"], G, z["id"], z["owner"], z.get("parent", 0),
                                 pile_refs=z.get("pile_refs"), contents=z.get("contents"),
                                 areatype=z.get("areatype"))
        entries.append((z["id"], env))
    # NOTE: players are NOT appended here — they ride item-33 LIST#1 above (the FUN_100d9400 / vt+0x24 path).
    b += i(len(entries))
    for eid, env in entries:
        b += i(eid) + i(len(env)) + env
    b += i(99999)                       # item 34: magic marker (non-fatal)
    b += i(0) * 4                       # items 35..38: empty counts
    # --- mGameTurn (embedded inline, DEPTH 1) ---
    b += _gameturn(G, 1, current_player=current_player, state_classid=state_classid,
                   with_state=with_state)
    # --- Game trailer (version 1 path) ---
    b += i(0)                           # FUN_100c94c0 (+0x2e) vector empty
    b += i(0)                           # +0xb9 int
    b += i(0)                           # FUN_1005c310 (+0xba) vector empty
    return b


# ============================================================================
# *** VERSION / STRUCTURE FINDING (real 262 vs our v1) -- the open render blocker ***
# ============================================================================
# The 262 wire MUST be base Game classid 166 (FUN_100fca50 asserts classID==166 @Game.cpp:0x232f;
# an 80003/EQ envelope -> "Wanted classID 166, but got 80003" @0x9007 -> CLIENT CRASH). So 166 is right.
# BUT the version matters: FUN_100fca50 gates EVERY field on the envelope version. The captured REAL
# renderable game decoded to version 48 (base Game getVersion FUN_100fa930 returns 0x32=50). Our
# build_2p_game emits version 1, which SKIPS all the version-gated reads -- and the structure DIVERGES
# at high version, which is almost certainly why our enriched v1 still bounces:
#   * GameTurn (FUN_10116100 @GameTurn.cpp): the inline StateMachine present-codec (FUN_100d4a60) is read
#     ONLY at ver<0xf. At ver>=0xf the GameTurn does NOT embed the StateMachine -- so our entire v1
#     "StateMachine-inside-GameTurn" layout is a v1-only artifact the real (v48) game never uses; the
#     real game's StateMachine is serialized SEPARATELY near the end (captured wire: SM magic 666666
#     @70479, AFTER the GameTurn + a 9-deep state stack, ids 487..523 top=523).
#   * Post-magic (FUN_100fca50 @0x2480+): after the item-34 magic the real game reads MANY populated
#     maps/loops (FUN_10064e60, repeated FUN_1000b2a0, FUN_100ef480 ...) that our v1 emits as empty.
#   * Header: ServerID (param_1[3]) is read at ver>=0x19 (now emitted here when ver>=0x19).
# CONSEQUENCE: rendering needs the HIGH-VERSION (v48/50) base-166 structure, not our v1 shell. That is a
# staged rebuild (high-version GameTurn w/o inline SM, separate StateMachine + per-state buffer, populated
# post-magic maps, EQ-subclass contents -- e.g. players as EQPlayer/80005 via map28, EQ elements). Use the
# decoded real wire (decode_wire_game.py on real_wire_game.bin) for the content SHAPE, re-encoded to 166.
# The further bounce gate beyond hasCurrentState() is the gameScreen vt55 predicate FUN_103f49a0 ->
# checks FUN_100cef10 / FUN_100cfd30 (game-state predicates, not yet decompiled) + screen flag +0x21
# (FUN_10485e20); StateMachine::process is FUN_101a8420 (vt15), currentState->updateState = state vt
# slot 15. Confirming those pass conditions needs a live sweep (env knobs below).
#
# ============================================================================
# 2-PLAYER RUNNING BOARD  (v1 shell -- templated on the captured real campaign game)
# ============================================================================
# Capture ground truth (E:\SWGTCG\re\out\capture_game.log, EQMainController @0a903ca8):
#   mGameStarted=1, GameIsSetup=1, GameEnded=0, OutOfSync=0, PlayerCount=2, OrigPlayerCount=2,
#   GameTurn+0x08 -> a real EQPlayer (the current player), mCurrentStateMachine non-null with a
#   current state (real game was mid-match: PlayCardState/247; a freshly-started game's first state
#   is a setup/start state). EvaluationEnvironment(170) is NOT in the wire blob -- the StateMachine
#   readFrom (StateMachine.cpp:0x387..) never deserializes a classid-170 object; the EvalEnv is built
#   at runtime during state processing, so we do not (and must not) emit one here.
#
# NOTE the per-state field buffer (StateMachine vector#2) is left EMPTY -> the state is
# default-constructed (its readFrom is NOT called). That is enough for Game::hasCurrentState()==true.
# A state that also drives StateMachine::process (updateState != 0) needs a valid per-state readFrom
# buffer; that wire format (StateMachineState/PlayCardState::readFrom + the element refs it casts) is
# the remaining RE item and can only be confirmed against a live client. The .bin captures are the
# in-memory object layout, NOT the readFrom wire buffer, so they cannot be byte-replayed there.
GAME_START_STATE = int(os.environ.get("GAME_START_STATE", "259"))  # 259=MultiPlayerState: updateState
# returns 2 (valid yield) even default-constructed, so StateMachine::process succeeds (234 base returns 0
# =invalid -> bounce). env-tunable to sweep (234/247/259/374). All ComponentFactory-registered.
                         # Set SM_CLASSID env / state_classid to try a richer state (e.g. 247) once
                         # that classid's factory registration + readFrom buffer are confirmed.


def build_2p_game(game_id, accounts=(1, 2), deck_id=111, started=True,
                  current_player=None, state_classid=None, hand_cards=2):
    """A running 2-player board for SendSerializedGame(262), templated on the captured real game.

    accounts      = the two player object-ids (also their registry keys); player[0] is first/current.
    deck_id       = starter deck (standalone_cards.load_starter_deck) used for the avatar + hand catalog ids.
    hand_cards    = how many real starter cards to seat in each player's Hand pile (0 = just the avatar).
    Returns the raw Game::serialize bytes (caller zlib-compresses via build_sendserializedgame)."""
    try:
        from standalone_cards import load_starter_deck
        sd = load_starter_deck(deck_id)
        avatar = sd.get("avatar") or 267554
        main_pool = [c for (c, q) in sd.get("main", []) if c] or [267554]
    except Exception:
        avatar, main_pool = 267554, [267554]
    a0, a1 = accounts[0], accounts[1]
    if current_player is None:
        current_player = a0
    if state_classid is None:
        state_classid = GAME_START_STATE

    zones, player_data = [], []
    # disjoint id ranges per seat: P=PlayerPlayArea(174), D/X/H=DrawDeck/Discard/Hand piles(173),
    # A=avatar Card(168, the PlayerCard), then the hand Card(168) ids.
    for seat, acct in enumerate(accounts):
        base = 900000 + seat * 1000
        P, D, X, H, A = base + 0, base + 1, base + 2, base + 3, base + 4
        hand_ids = [base + 10 + k for k in range(hand_cards)]
        # Hand childmap lists EVERY card parented to H (avatar + hand cards) so the pile contents and
        # the cards' parent refs stay consistent (the proven single-client layout parented a card to H;
        # the childmap is the populated-zone enhancement read by FUN_10063b50 as id-only, no resolve).
        in_hand = [A] + hand_ids
        zones += [
            {"id": P, "classid": PLAYERPLAYAREA, "owner": acct, "pile_refs": [D, X, H]},
            {"id": D, "classid": PILEPLAYAREA, "owner": acct},
            {"id": X, "classid": PILEPLAYAREA, "owner": acct},
            {"id": H, "classid": PILEPLAYAREA, "owner": acct, "contents": {0: in_hand}},
            {"id": A, "classid": CARD, "owner": acct, "parent": H, "catalog_id": avatar},
        ]
        for k, hid in enumerate(hand_ids):
            zones.append({"id": hid, "classid": CARD, "owner": acct, "parent": H,
                          "catalog_id": main_pool[k % len(main_pool)]})
        player_data.append({"objid": acct, "ppa_id": P, "card_id": A})

    return serialize_minimal_game(
        game_id, players=list(accounts), zones=zones, player_data=player_data,
        started=started, first_player=a0, current_player=current_player,
        random_seed=0x6a41fd99, match_id=0, player_count=len(accounts),
        state_classid=state_classid)


# ============================================================================
# REAL EQ(80003)/v48 WIRE -- the captured renderable game (the ACTUAL format)
# ============================================================================
# DECODED FINDING (decode_wire_game.py on real_wire_game.bin, 70645 bytes captured live):
#   The renderable game's envelope is **classid 80003 (EQGame/EQMainController), version 48** -- NOT the
#   base Game(166)/v1 that build_2p_game + serialize_minimal_game emit. This is the likely bounce cause:
#   the client reconstructs our 166/v1 shell, but the gameScreen needs the full EQ(80003)/v48 graph.
#   Validated container layout (magic 99999 @47280, SM magic 666666 @70479):
#     header = base layout but @v48 so ServerID is present (GameID=1, MatchID=-5, ServerID=1,
#       mGameStarted=1, AI=1, PlayerCount=2, GameIsSetup=1, RandomSeed, Version=1394) ;
#     5 leading vecs (PlayerOrderData=[1,2], OrderedAccountIDs, PlayerIDList=[1,2], +2 empty) ;
#     objId->classId map POPULATED: (1->80005),(2->80005)  [80005 = EQPlayer; OUR v1 LEFT THIS EMPTY
#       = shell players -- a real game factory-creates real EQPlayers] ;
#     player readFroms = EQPlayer v48 buffers (~862 / 889 bytes each, vs our 18-byte stubs) ;
#     RevealMap/PlayerDrawMap empty ; 211 PlayElement shells + 211 readFroms = 43 KB of populated
#       board (PlayAreas 172, piles 169/181, EQCards 80001, archetypes) ;
#     item34 magic 99999 ; then a v48-populated trailer + GameTurn + StateMachine with a DEEP 9-state
#       stack (mStateStack ids 487..523, top/current = 523) and SM magic 666666.
# A faithful PARAMETERIZED v48 emitter (EQPlayer/EQCard/EQPlayElement v48 readFrom layouts + the
# per-state buffer) is a sizeable follow-up. The captured wire is ITSELF a valid renderable 80003/v48
# game, so the immediate render-confirmation is to REPLAY it (below).
# DEFAULT = the real ENGINE-DEALT skirmish capture (2026-07-05, server-engine M1): a COMPLETE 80003/v48 game
# (2 EQPlayers + 162 materialized element shells + live StateMachine), account-ids remapped 0xfffffc18/17->1,2
# to match the online launch convention. Stable (not clobbered by fresh out\real_wire_game.bin captures).
# Override with SWGTCG_REAL_WIRE to A/B against another capture.
REAL_WIRE_PATH = os.environ.get("SWGTCG_REAL_WIRE", r"E:\SWGTCG\re\out\dealt_skirmish_v48_aligned.bin")
# The captured wire's INTERNAL gameid is 1, embedded throughout (every sub-object stores it for the
# FUN_100df070 lookup). Remapping it is infeasible (enc_int(1)=0x01 appears as data everywhere), so a
# replay MUST be launched with this gameid: LaunchGame(116) and SendSerializedGame(262) base BOTH = 1.
REPLAY_GAMEID = 1


def build_real_game_replay(path=None):
    """Return the captured REAL EQ(80003)/v48 serialized game verbatim. *** CRASHES the 262 client ***
    (FUN_100fca50 asserts classID==166, gets 80003 -> Game.cpp:0x9007). Analysis/decode reference ONLY;
    do NOT send as 262. Use build_166_v48_from_capture() for a sendable wire."""
    with open(path or REAL_WIRE_PATH, "rb") as f:
        return f.read()


def build_eq_80003_blob(path=None, eq_ver=48, inner_classid=80003, dxf=None, dfd=0, coll=0):
    """The RENDER-path 262 blob (workflow wcpis8xx0). FUN_00636c80 (EQGame::deserialize) reads the archive
    "begin" ([classid][version]) TWICE: line40 = EQ begin, line165 (FUN_004fca50) = base begin+body. The
    captured wire has only ONE [80003][48] (= the BASE begin). So PREPEND a 2nd [80003][eq_ver] (EQ begin) +
    the 11 EQ-header fields, then the capture VERBATIM. Empty header = fresh 2p game; the ver>0x2b card-template
    COLLECTION block reads ZERO wire bytes. Assert->field map for debugging (eqgame.cpp line @0x497a80):
    0x1329 begin / 0x1337=+0xdf / 0x133c=+0xfd / 0x1349=+0xe8 / 0x134c=+0xec / 0x1351=+0xf0 / 0x136f=+0xe0 /
    0x1373=+0xe4 / 0x1377=+0xf6 / 0x137b=+0xf9 / 0x137e=base body / (0x232f=base classid=Risk#3 -> inner_classid=166)."""
    with open(path or REAL_WIRE_PATH, "rb") as f:
        wire = f.read()
    if wire[:3] != enc_int(80003):
        raise ValueError("capture head=%s != enc_int(80003)=%s" % (wire[:6].hex(), enc_int(80003).hex()))
    # L165 base body = capture verbatim (its [80003][48] = the base begin)
    body = wire if inner_classid == 80003 else (enc_int(inner_classid) + wire[3:])
    return _eq_header(eq_ver, dxf, dfd, coll) + body


EQ_DXF = int(os.environ.get("SWGTCG_EQ_DXF", "1"))  # ★ the L48 +0xdf scalar = Game+0x38c (EQGame::deserialize
# 0x636d01 lea [edi+0x37c] readInt, edi=base subobj=EQGame+0x10 -> EQGame+0x38c). POPULATE FUN_62a950 resolves
# it as a card element id -> RTTI EQCard -> vt+0x34; =0 CRASHES (the deser-time AV the cdb "+0x38c seed" dodged).
# DEFAULT 1 makes the 262 blob carry +0x38c=1 -> POPULATE survives with NO cdb seed (fully server-driven).


def _eq_header(eq_ver=48, dxf=None, dfd=0, coll=0):
    """The EQGame::deserialize (FUN_00636c80) EQ-envelope prefix that precedes the base Game body: the
    2nd [80003][ver] begin (line40) + the 11 EQ-header fields. Split out of build_eq_80003_blob so a
    from-scratch body can reuse the PROVEN wrap. Field->assert map: see build_eq_80003_blob docstring.
    dxf (L48 +0xdf) = Game+0x38c, the element id POPULATE needs; default EQ_DXF=1 (nonzero = survives)."""
    if dxf is None:
        dxf = EQ_DXF
    z = enc_int(0)
    out  = enc_int(80003) + enc_int(eq_ver)   # L40 EQ begin
    out += z                                  # L47  +0xdc
    out += enc_int(dxf)                        # L48  +0xdf scalar (Risk#2: may need nonzero)
    if eq_ver > 0x2f: out += enc_int(dfd)      # L54  +0xfd scalar (Risk#2)
    if eq_ver < 0x2d: out += z                 # L59  legacy discard (skipped at 48)
    out += z                                  # L65  vec +0xe8
    out += z                                  # L71  vec +0xec
    if eq_ver > 0x20: out += z                # L79  set +0xf0
    # L86 ver>0x2b COLLECTION -- count-prefixed (Risk#1 resolved: without it +0xf9 asserted at 0x137b).
    if eq_ver > 0x2b: out += enc_int(coll)
    if eq_ver > 0x2c:
        out += z + z + z + z                  # L137 +0xe0, L144 +0xe4, L151 +0xf6, L158 +0xf9
    return out


def build_eq_prestart_blob(accounts=(1, 2), game_id=None, eq_ver=48, inner_classid=80003, deck_cards=50,
                           hand_cards=0, deck_id=111):
    """★ SERVER-DRIVEN BOARD (the pre-start path, card-placement-solved memory 2026-07-03): an EQ(80003)/v48
    game in the PRE-START state so the CLIENT'S OWN engine boots the board itself (no cdb force). Disasm-
    conclusive trigger chain: SetupGame(67)->Game::setup(FUN_4f3130) [GameIsSetup 0->1, needs mPlayerCount>=2 +
    ordered-account/player-order sizes==count] -> since mGameIsReadyForStart==1 it calls advanceTurn (EQGame
    vt+0xa0=FUN_6300f0) [now GameIsSetup==1 -> REAL advance] -> creates+runs EQStartOfGameState (classid 80016,
    'kWaitForDeckStartOfGame') -> WAITS FOR DECKS then DEALS from each player's DrawDeck -> emits the board
    burst (0x60 add-players + per-card 0x73). So the blob MUST be: GameStarted=0, GameIsSetup=0, ReadyForStart
    (mGameIsReadyForStart game+0x169)=1, EMPTY state stack, 2 players, and each player's DrawDeck pile POPULATED
    with a full deck (so kWaitForDeck satisfies). Wrapped in the EQ envelope so vt+0xa0 dispatches to the EQ
    advanceTurn that creates the EQ start state. inner_classid=80003: the EQ path's base-body begin must match
    the VIRTUAL classid the original base-serialize wrote (80003), NOT 166 -- LIVE-CONFIRMED 2026-07-03: a 166
    inner body crashed with wa_assert Game.cpp:0x232f(9007) + eqgame.cpp:0x137e(4990) (the classid checks); the
    working capture render path also uses an 80003 inner body. eq_ver/inner_classid are live-sweep knobs.
    OPEN RISK: re-running start-of-game may double-deal; iterate live."""
    G = REPLAY_GAMEID if game_id is None else game_id
    body = build_base_render_game(
        accounts=accounts, game_id=G, deck_id=deck_id, deck_cards=deck_cards, hand_cards=hand_cards,
        gt_ver=18,                       # >=0xf: GameTurn has NO inline SM -> hasCurrentState empty ->
        game_started=0, game_is_setup=0, # advanceTurn creates the real EQStartOfGameState fresh (not a shell)
        ready_for_start=True, queue_mode=0, game_over=False, with_state_stack=False)
    if inner_classid != 166:
        body = enc_int(inner_classid) + body[len(enc_int(166)):]
    return _eq_header(eq_ver) + body


# Capture header flag offsets (LIVE-DECODED 2026-07-03; all single-byte enc_int, patch in place, no shift).
# The v48 EQ capture (REAL_WIRE_PATH) begins [80003][48][GameID][MatchID][ServerID]... single-byte scalars.
CAP_FLAG_OFF = {"mGameStarted": 9, "GameIsSetup": 16, "ReadyForStart": 17, "mAIEnabled": 10}


def build_eq_prestart_capture(path=None, eq_ver=48, ai=None):
    """★ SERVER-DRIVEN BOARD v2 (2026-07-03): make the PROVEN working EQ capture (build_eq_80003_blob renders
    it) into a PRE-START game by patching only its 3 header flags -> GameStarted=0, GameIsSetup=0,
    ReadyForStart=1. This KEEPS every EQ-format sub-object (EQGameTurn 80004, EQPlayers 80005, 119 EQCards
    80001 = 2 full decks, EQStateMachine) intact -- unlike build_eq_prestart_blob (from-scratch BASE sub-objects)
    which the EQ reader rejected LIVE with eqgameturn.cpp:461 (base-232 GameTurn read as EQ). Then the server
    sends SetupGame(67): Game::setup [GameIsSetup 0->1] -> ReadyForStart==1 -> advanceTurn (EQGame vt+0xa0) ->
    the client boots EQStartOfGameState itself -> deals from the capture's decks -> board, no cdb.
    ai: override mAIEnabled (capture=1); pass 0 for a pure-PvP pre-start. OPEN RISK: the capture is a MID-GAME
    snapshot (9-deep state stack, cards already in hands) -> re-running start-of-game may double-deal/conflict;
    iterate live (next step if it asserts: also reset/empty the post-magic state stack)."""
    with open(path or REAL_WIRE_PATH, "rb") as f:
        cap = bytearray(f.read())
    if bytes(cap[:len(enc_int(80003))]) != enc_int(80003):
        raise ValueError("capture head != 80003")
    cap[CAP_FLAG_OFF["mGameStarted"]] = 0      # mGameStarted 1 -> 0
    cap[CAP_FLAG_OFF["GameIsSetup"]] = 0       # GameIsSetup  1 -> 0
    cap[CAP_FLAG_OFF["ReadyForStart"]] = 1     # ReadyForStart 0 -> 1 (arm advanceTurn after setup)
    if ai is not None:
        cap[CAP_FLAG_OFF["mAIEnabled"]] = 1 if ai else 0
    return _eq_header(eq_ver) + bytes(cap)     # EQ envelope + patched capture verbatim


def build_166_v48_from_capture(path=None):
    """Transform the captured wire into a SENDABLE base-Game(166)/v48 262 blob.

    KEY RE FINDING: real_wire_game.bin was produced by calling the BASE Game::serialize (0x4fb170 =
    GSER28) on the live EQ object. That base serializer writes its begin via `vtable+8 -> this->
    getClassId()` which is VIRTUAL -> on the EQ object it returns 80003 -- but the BODY writes PURE BASE
    content (no EQ-override fields; those live in EQGame::serialize 0x633950 @ offsets ebx+0x370..0x3f4,
    which we did NOT call). So the capture = begin(80003,48) + exactly-base-content. The 166 reader
    FUN_100fca50 (which asserts classID==166) will consume it verbatim once the begin classid is 166.

    TRANSFORM (verified: header + 5 vecs + map28[(1,80005),(2,80005)] + 2 player readFroms + 211 element
    shells/readFroms + magic 99999 @47280 all consumed): replace the leading enc_int(80003) with
    enc_int(166); keep version 48 and every following byte (the separately-serialized StateMachine +
    9-deep state stack + per-state buffers, players, 211 PlayElements, post-magic maps).
    Returns the raw 166/v48 bytes. Launch with REPLAY_GAMEID (the wire's internal GameID=1)."""
    with open(path or REAL_WIRE_PATH, "rb") as f:
        wire = f.read()
    c80003 = enc_int(80003)
    if wire[:len(c80003)] != c80003:
        raise ValueError("captured wire does not begin with classid 80003 (head=%s)" % wire[:6].hex())
    return enc_int(166) + wire[len(c80003):]   # 80003 -> 166, keep ver 48 + all base content


# EQ-class -> base-class map for the FULL capture remap. The base Game::deserialize (FUN_100fca50)
# consumes the length-prefixed sub-object buffers (players ~862B, 211 elements) BY LENGTH regardless of
# class, so those need no remap; only the STRICT classid-checked sub-objects crash it: the embedded
# GameTurn (80004, base 232 -- the known "232!=80004" assert) + StateMachine (80006, base 233). Remap
# those + the top EQGame(80003->166). Position-specific, left-to-right, delta-tracked (enc lengths differ).
# EQ-class -> base-class remap for the full object graph. Only the DIRECTLY-READ classids get remapped
# (the objId->classId player map + the 211-entry element-shell map, both read inline; the top EQGame; and
# the post-magic GameTurn/StateMachine). The length-prefixed sub-object BUFFERS (player/element readFroms,
# SM per-state buffers) are consumed BY LENGTH and MUST NOT be touched (remapping inside them shifts content
# vs the unchanged length prefix -> misalignment). Verified: 119 EQCards(80001->168), 2 EQPlayers(80005->261)
# in the maps + top EQGame(80003->166) + GameTurn(80004->232)+StateMachine(80006->233) in the trailer.
# NOTE: the STANDALONE (SWGTCGGame.exe) has the EQ classes factory-registered (EQPlayer 80005, EQCard 80001),
# NOT the base ones -- remapping players/cards to base -> "Couldn't get class 261/168" AV. So LEAVE players/
# cards EQ (the factory creates real EQPlayers/EQCards). Only remap the classids the base Game::deserialize
# reads INLINE with a strict base-classid check: the top EQGame(80003->166) + GameTurn(80004->232) +
# StateMachine(80006->233). (Those aren't factory-created; game->vt+0x14 makes a base GameTurn, so the
# stream classid must be 232.)
EQ_TO_BASE = {80003: 166, 80004: 232, 80006: 233}
# The GameTurn (@49362) + StateMachine (@49707) in the capture are each serialized as a DOUBLE
# [classid, version, classid, version] pair (EQ framing + the object's own begin), but the base reader
# (game->vt+0x14 creates a base GameTurn/SM, then GameTurn::deserialize begin reads ONE [classid, version]
# then fields). Verified by CDB (probe_getelem): base reader ate the 1st [232,18] as begin, then read the
# 2nd [232,18] as (gameID=232, current-element=18) -> getElement(18)=null -> GameTurn.cpp:0x322 assert.
# Base blob's working GameTurn = SINGLE [232,14,1,1]. FIX: collapse the double to single + remap the classid.
CAPTURE_COLLAPSE_SITES = [(49362, 80004, 232), (49707, 80006, 233)]  # (offset, eq_classid, base_classid)

def _capture_map_sites(d):
    """Walk real_wire_game.bin exactly like base Game::deserialize (FUN_100fca50) and return the byte
    offsets of every DIRECTLY-READ classid in the objId->classId player map + the element-shell map. The
    length-prefixed buffers (player/element/reveal/draw readFroms) are skipped by length (untouched)."""
    from swgcodec import dec_int as _di, dec_str as _ds
    p = [0]
    def i():
        v, p[0] = _di(d, p[0]); return v
    def s():
        v, p[0] = _ds(d, p[0]); return v
    def vints():
        n = i(); [i() for _ in range(n)]
    def vblobs():
        n = i()
        for _ in range(n):
            i(); bl = i(); p[0] += bl        # skip each length-prefixed buffer by its length
    sites = []
    def vpairs_cls():                          # (objId, classId) -- record each classId offset
        n = i()
        for _ in range(n):
            i(); off = p[0]; cid = i(); sites.append((off, cid))
    i(); ver = i()                              # envelope classid, version
    i(); i()                                    # GameID, MatchID
    if ver >= 0x19: i()                         # ServerID
    for _ in range(14): i()                     # header scalars
    s()                                         # MatchDir
    for _ in range(4): i()                      # RevealID/GameOver/EventID/GetTargetID
    for _ in range(5): vints()                  # 5 leading vecs
    vpairs_cls()                                # objId->classId player map (DIRECTLY READ)
    vblobs()                                    # player readFroms (skip)
    vblobs(); vblobs()                          # RevealMap, PlayerDrawMap (skip)
    vpairs_cls()                                # element-shell map (DIRECTLY READ)
    return sites

def build_capture_hybrid2(path=None):
    """HYBRID-2: the capture's REAL header + players + 211 elements + REAL GameTurn+StateMachine (with the
    collapse fix, the base reader consumes these cleanly up to the game+0x77 map at pos 49447), spliced onto
    the base blob's EMPTY post-GameTurn maps (skipping the capture's EQ-format maps that the base reader
    can't parse). Unlike build_capture_hybrid (minimal base SM), this keeps the capture's REAL PLAYING SM
    (9-deep state stack) -> the game holds the board view instead of reverting to the starscape. Launch at
    REPLAY_GAMEID=1."""
    cap = build_capture_remap(path)                 # collapse fix: real GameTurn+SM read cleanly to 49447
    base = build_base_render_game(accounts=(1, 2), game_id=REPLAY_GAMEID)
    CAP_MAP = 49447                                 # verified (CDB): the game+0x77 map pos = end of GameTurn+SM
    b666 = base.find(enc_int(666666))               # base SM magic -> base empty-maps start right after it
    if b666 < 0:
        raise ValueError("base 666666 magic not found")
    base_map = b666 + len(enc_int(666666))
    return cap[:CAP_MAP] + base[base_map:]          # real board+GameTurn+SM + base empty maps


def build_capture_hybrid(path=None):
    """HYBRID win path: the capture's REAL header + 2 players + 211 populated elements (verbatim up to the
    magic 99999), spliced onto the base blob's CLEAN tail (GameTurn current_player=1 + minimal SM + all
    post-GameTurn maps EMPTY count=0). The capture's EQ GameTurn/SM/maps are the base reader's roadblock
    (EQ field layouts != base); the base tail sidesteps that entirely while keeping the real board elements.
    The base GameTurn's current_player=1 resolves to the capture's player 1 (registered EQPlayer). If the
    element/player readFroms register cleanly, the base reader deserializes this -> SWGameScreen draws the
    REAL cards with a base turn state. Launch at REPLAY_GAMEID=1."""
    cap = build_166_v48_from_capture(path)          # top EQGame 80003->166, players/elements EQ + verbatim
    base = build_base_render_game(accounts=(1, 2), game_id=REPLAY_GAMEID)
    magic = enc_int(99999)
    mpos = cap.find(magic, 40000)                   # capture magic (item34, ~47280) -> end of real board data
    bpos = base.find(magic)                         # base magic -> start of the clean tail
    if mpos < 0 or bpos < 0:
        raise ValueError("magic 99999 not found (cap=%d base=%d)" % (mpos, bpos))
    return cap[:mpos] + base[bpos:]                 # real board head + clean base tail


def build_capture_remap(path=None, remap=None):
    """Transform the REAL captured running game (real_wire_game.bin: 2 players, 211 populated PlayElements/
    cards, GameTurn + 9-deep StateMachine) into a SENDABLE base-262 blob by remapping the DIRECTLY-READ EQ
    classids across the whole object graph (EQ_TO_BASE): the player + element maps (walked) + the top EQGame
    + the trailer GameTurn/StateMachine (pinned offsets). Length-prefixed buffers ride along verbatim.
    Launch at REPLAY_GAMEID=1. THE CAPTURE-REPLAY win path. remap overrides EQ_TO_BASE for iterating."""
    from swgcodec import dec_int
    with open(path or REAL_WIRE_PATH, "rb") as f:
        d = f.read()
    m = EQ_TO_BASE if remap is None else remap
    # edit list: (start, end, new_bytes) applied left-to-right. Remap = same-position replace; collapse =
    # replace the double [C,V,C,V] with single [base_classid, V].
    edits = []
    # top EQGame @0 -> 166
    if 80003 in m and d[:len(enc_int(80003))] == enc_int(80003):
        edits.append((0, len(enc_int(80003)), enc_int(m[80003])))
    # map classids (walked) -- only if present in the remap (players/cards stay EQ by default)
    for off, cid in _capture_map_sites(d):
        if cid in m:
            oe = enc_int(cid)
            edits.append((off, off + len(oe), enc_int(m[cid])))
    # GameTurn/StateMachine: collapse the double [C,V,C,V] -> [base, V]
    for off, eq_cid, base_cid in CAPTURE_COLLAPSE_SITES:
        c1, p1 = dec_int(d, off)                 # C1
        v1, p2 = dec_int(d, p1)                  # V1
        c2, p3 = dec_int(d, p2)                  # C2
        _v2, p4 = dec_int(d, p3)                 # V2
        if c1 != eq_cid or c2 != eq_cid:
            raise ValueError("collapse site @%d not a double %d pair (got %d,%d)" % (off, eq_cid, c1, c2))
        edits.append((off, p4, enc_int(base_cid) + d[p1:p2]))   # [base_classid][V1]  (drop [C2,V2])
    edits.sort()
    out = bytearray(); prev = 0
    for start, end, new in edits:
        if start < prev:                         # overlap -- skip
            continue
        out += d[prev:start]; out += new; prev = end
    out += d[prev:]
    return bytes(out)


# ============================================================================
# BASE-166 GAME SERIALIZER FROM SCRATCH  (build_base_render_game)  -- the path
# ============================================================================
# Full base Game::deserialize FUN_100fca50 LAYOUT SPEC at the renderable version (default 48; base
# getVersion slot8 FUN_100fa930=0x32=50; the live capture was v48). Field types: i=enc_int, s=enc_str,
# vint=count+ints (FUN_1000b2a0), vpair=count+(a,b) (FUN_1005c310), map=count+entries (FUN_100c94c0/
# 100ef6c0/100ef480/100e3120/100e3270/10064e60), blobvec=count+(id,len,buf) (FUN_100e2ec0),
# present=enc_int(0)+child (FUN_100d4a60). Linear v48 read order (RVAs = Game.cpp asserts in FUN_100fca50):
#   HEADER(@0x232f..0x2382): GameID,MatchID,[ServerID @ver>=0x19],mFirstPlayerID,mGameStarted,AI,GameEnded,
#     OutOfSync,BatchControl,PlayerCount,OrigPlayerCount,GameIsSetup,ReadyForStart,RandomSeed,Version,
#     QueueMode,displayState,MatchDir(s),RevealID,GameOver,EventID,GetTargetID.
#   CONTAINERS(@0x2392..0x2422): vint(0x4e PlayerOrderData), vint(0x52 OrderedAccountIDs), vint(PlayerIDList),
#     vint, vint, map28(objId->classId; players->261), blobvec(player readFroms -> Player(261)::readFrom),
#     RevealMap, PlayerDrawMap, vpair(element shells: id,classId in {174,173,168}), blobvec(element readFroms).
#   MAGIC(@0x2480): i==99999.
#   POST-MAGIC(@0x248e..0x24c6): map(local_b0), vint(0x3f mValidActionFilters), map(local_c0). [emit empty]
#   GAMETURN(@line880): controller-created GameTurn(232)::readFrom -- begin(232,gtVer)+gameid+current-player+...
#     (at gtVer>=0xf NO inline StateMachine; current player resolved via FUN_100d9400).
#   VER-GATED GAME FIELDS(@0x24f0..0x25a1): RevealedCards[>1], IgnorePlayerList[>2], WinMap+ElapsedTime[>4],
#     Duration[>5], RunTimer[>6], PlayerActionCountMap[>=8], PlayType[>9], LossTypeMap[>10], WhoSelectedCard[>=0xc],
#     InstalledActionCardMap[>0xc], PrePassMap[>0xd], suppressedGameTextCards[>0xf], GameNum[>=0x12],
#     leagueID[>=0x15], map(+0x2e), TurnNumber, vpair(+0xba), vint(+8)[>=0x19], bool+map[>=0x1d].
#   STATEMACHINE(@0x2577): [ver>=0x1f] present-codec FUN_100d4a60 -> StateMachine(233) with 3 vectors
#     (stateStackMap / state-buffers / stateStack) + magic 666666; current state classid from the knob.
#   TRAILER: MatchStructure[>=0x1f], vint[>=0x24], int[>=0x2a], map[>=0x2e], bool[>=0x2f]; ver<0x31 -> done.
import os as _os
BASE_GAME_VER     = int(_os.environ.get("SWGTCG_GAME_VER", "48"))     # envelope version (gates all game fields)
BASE_GT_VER       = int(_os.environ.get("SWGTCG_GT_VER", "14"))       # GameTurn version. RENDER FIX: <0xf so the
# GameTurn reader (FUN_10116100 line 94) reads an INLINE StateMachine present-codec into GameTurn+0x18 (param_1+6).
# hasCurrentState() walks mGameTurn->getCurrentStateMachine()(GameTurn+0x18)->mStateStack top, so +0x18 MUST be a
# live SM. The BASE Game::deserialize (FUN_100fca50) does NOT link the game-level SM(Game+0xc2) back to
# GameTurn+0x18 -- its FUN_101113d0(regSM) sets the SM object's own +0x18, not the GameTurn's (only EQGame's
# override links it). At gtV>=0xf GameTurn+0x18 stays NULL -> hasCurrentState false -> silent teardown/bounce.
# gtV<0xf makes the base GameTurn path populate +0x18 itself. (Real v48 games use gtV=18 but run EQGame::deserialize.)
BASE_SM_VER       = int(_os.environ.get("SWGTCG_SM_VER", "1"))        # StateMachine version (minimal)
# RENDER SHORTCUT (RE agents a6445e07 + ae00e5d3, decisive): the gameScreen bounce predicate (vt55 FUN_103f49a0)
# closes the board unless screen+0xed OR game+0x1C4 (mGameOver) OR game+0x121 (winner) is set.
#  * game+0x1C4 = mGameOver is READ STRAIGHT FROM THE 262 BLOB (Game::deserialize FUN_100fca50:284 = the +0x71
#    header int). Setting it 1 forces a GAME-OVER board that RENDERS STATICALLY (proves the zone/player/card graph
#    builds + escapes the black board). This is the ONLY server-drivable render path.
#  * screen+0xed (the clean, live/interactive board flag) is set ONLY by the WAGameScreen start-of-game slot
#    FUN_007f3f70, invoked via Qt meta-call from the client's LOCAL game engine (advanceTurn -> the state machine
#    actually runs). After a 262 blob, StateMachine::deserialize (FUN_101a9630) restores state DATA passively and
#    never enters/runs states, so NO wire command reaches the slot: SendSerializedGame(262) is data-only,
#    ReadyForStartOfGame(117) client-run is a no-op (return 1), StateSpecificMessage(63) needs an already-running
#    SM. => a server-driven 262 board is INHERENTLY STATIC; a live interactive board needs the client's local
#    engine (single-player/AI advanceTurn) and cannot be forced from the wire. See swgtcg-platform-foundation memory.
# Default OFF; flip SWGTCG_BOARD_GAMEOVER=1 for the static render (the achievable server-driven milestone).
BASE_GAMEOVER     = _os.environ.get("SWGTCG_BOARD_GAMEOVER", "0") != "0"
BASE_STATE_CLASSID = int(_os.environ.get("SWGTCG_STATE_CLASSID", "259"))  # current state (259 MultiPlayerState:
                                                                          # updateState->2 so process() keeps board)
# BOOT MODE flags (RE'd 2026-07-01): make the client's OWN engine build the board via advanceTurn -> StartOfGame.
# ReadyForStart (game+0x159) arms the update loop to call advanceTurn; QueueMode (game+0x168) != 0 is advanceTurn's
# proceed gate. Default OFF (=the prior byte-identical blob). SWGTCG_BOARD_BOOT=1 sets both (server also forces
# game_over=0). Sweepable independently for the live experiment.
_BOOT = _os.environ.get("SWGTCG_BOARD_BOOT", "0") != "0"   # single switch: arm both boot gates
BASE_READY_FOR_START = _os.environ.get("SWGTCG_BOARD_READY", "1" if _BOOT else "0") != "0"
BASE_QUEUE_MODE      = int(_os.environ.get("SWGTCG_BOARD_QUEUEMODE", "1" if _BOOT else "0"))


def _base_gameturn(game_id, ver, current_player):
    """base GameTurn(232)::readFrom wire (FUN_10116100). At ver>=0xf NO inline StateMachine."""
    b = i(232) + i(ver)
    b += i(game_id)                 # gameid (FUN_100df070)
    b += i(current_player)          # field#2 current-player id (FUN_100d9400 lookup; 0=none)
    b += i(0) + i(0) + i(0)         # [3],[4],[5]
    if ver < 0xf:
        b += i(0) + _statemachine(game_id, 1, state_classid=BASE_STATE_CLASSID)  # inline SM (ver<0xf only)
    b += i(0)                       # FUN_101140e0(+7) map      [reader line 97]
    b += i(0)                       # [10] int                  [reader line 101]
    b += i(0)                       # FUN_101140e0(+0xb) map     [reader line 105]
    b += i(0)                       # FUN_101140e0(local) map    [reader line 116 -- was MISSING:
    #   a THIRD FUN_101140e0 read (the cards loop @123-161 only PROCESSES it, no stream read). Its
    #   1-byte (empty=count0) omission drifted the cursor forward, overrunning EOF at trailer 9604.]
    b += i(0)                       # +0x11 bool                 [reader line 162]
    b += i(0)                       # FUN_10063b50(+0x2c) vec
    b += i(0) + i(0)                # +0x15,+0x1a bool
    if ver > 5:    b += i(0)        # +0x69 bool
    if ver > 6:    b += i(0) + i(0) # FUN_1000b2a0(+0x1b) vec + +0x1f bool
    if ver > 8:    b += i(0)        # +0x20 int
    if ver > 9:    b += i(0)        # +0x21 bool
    if ver > 0x10: b += s("")       # +0x23 string
    return b


def build_base_render_game(accounts=(1, 2), game_id=200, game_ver=None, gt_ver=None, sm_ver=None,
                           state_classid=None, deck_id=111, hand_cards=1, game_over=None,
                           ready_for_start=None, queue_mode=None, game_started=None, game_is_setup=None,
                           deck_cards=0, with_state_stack=None):
    """A from-scratch base Game(166) at the renderable version (default v48), per the LAYOUT SPEC above:
    2 base Players(261), base GameTurn(232) with current-player, a SEPARATE base StateMachine(233) with a
    valid current state, start flags set, populated minimal zones, all version-gated containers (empty).
    Env knobs: SWGTCG_GAME_VER / SWGTCG_GT_VER / SWGTCG_SM_VER / SWGTCG_STATE_CLASSID for the live sweep.

    BOOT MODE (RE'd 2026-07-01): the board WIDGETS are built by Game::advanceTurn (FUN_004f2750 / DLL 0x100f2750,
    Game vt+0xA0) -> "creating StartOfGame" -> the start-of-game Qt signal -> the board-build slot FUN_007f3f70.
    advanceTurn creates StartOfGame iff QueueMode (game+0x168, field #16) != 0 AND the runtime GameStarted flag
    (game+0x108, NOT serialized, defaults 0) == 0; it is ARMED by mGameIsReadyForStart (game+0x159 = ReadyForStart,
    field #13). Our default blob sets BOTH gates OFF (ReadyForStart=0, QueueMode=0) -> the client's update loop
    never calls advanceTurn -> empty gameScreen (the live symptom). Set ready_for_start=1 + queue_mode=1 (+ game_over
    =0) to make the client's OWN engine boot the board. game_started overrides the serialized mGameStarted (game+
    0xf8, field #5) -- distinct from the runtime GameStarted advanceTurn branches on."""
    V = game_ver if game_ver is not None else BASE_GAME_VER
    gtV = gt_ver if gt_ver is not None else BASE_GT_VER
    smV = sm_ver if sm_ver is not None else BASE_SM_VER
    stc = state_classid if state_classid is not None else BASE_STATE_CLASSID
    go = BASE_GAMEOVER if game_over is None else game_over
    rfs = BASE_READY_FOR_START if ready_for_start is None else ready_for_start
    qm  = BASE_QUEUE_MODE if queue_mode is None else queue_mode
    gst = 1 if game_started is None else (1 if game_started else 0)
    gis = 1 if game_is_setup is None else (1 if game_is_setup else 0)   # GameIsSetup blob field #12 (game+0x168)
    wss = True if with_state_stack is None else bool(with_state_stack)  # False = EMPTY SM stack (pre-start boot)
    G, a0, a1 = game_id, accounts[0], accounts[1]
    try:
        from standalone_cards import load_starter_deck
        sd = load_starter_deck(deck_id); avatar = sd.get("avatar") or 267554
        main_pool = [c for (c, q) in sd.get("main", []) if c] or [267554]
        # full flattened draw deck (each main entry expanded by its qty) for the pre-start deal
        deck_pool = [c for (c, q) in sd.get("main", []) for _ in range(max(1, q)) if c] or [267554]
    except Exception:
        avatar, main_pool, deck_pool = 267554, [267554], [267554]

    # --- per-seat zones + players: REPLICATE THE REAL BOARD'S ZONE SKELETON (decoded from
    # real_wire_game.bin, ids 1000002-1000009 / 2000002-2000009). Each player owns 8 zones:
    #   root PlayerPlayArea(174) areaType 0  + FIVE PilePlayAreas(173) areaTypes [3,1,1,1,1]
    #   + TWO PlayAreas(172) areaType 2, plus a top-level avatar Card(168, parent 0) = the PlayerCard.
    # Our OLD build had only root+3 piles(173, all areaType 1) and NO PlayArea(172) and NO areaType 3 ->
    # the gameScreen build (which groups a player's zones by areaType for placement) never finished init
    # (screen+0x100 stayed 0 -> teardown). areaType is PER-INSTANCE: 3 = the draw deck, 2 = the board
    # play areas. child-map key on a zone's card list = 3 (matches the real board). Ids follow the real
    # 1000000*(seat+1) scheme. Cards kept minimal (avatar + `hand_cards` in the hand pile).
    zones, player_data = [], []
    for seat, acct in enumerate(accounts):
        base = 1000000 * (seat + 1)
        AV   = base + 1                 # avatar / PlayerCard (168, top-level parent 0) -> player+0x30
        P    = base + 2                 # root PlayerPlayArea (174) areaType 0
        DECK = base + 3                 # PilePlayArea (173) areaType 3  (draw deck)
        PIL4 = base + 4                 # PilePlayArea (173) areaType 1
        HAND = base + 5                 # PilePlayArea (173) areaType 1  (hand)
        PA6  = base + 6                 # PlayArea     (172) areaType 2  (board play area)
        PIL7 = base + 7                 # PilePlayArea (173) areaType 1
        PIL8 = base + 8                 # PilePlayArea (173) areaType 1
        PA9  = base + 9                 # PlayArea     (172) areaType 2  (board play area)
        hand_ids = [base + 10 + k for k in range(hand_cards)]
        # DRAW-DECK cards (pre-start boot): base+100.. so they never collide with hand (base+10..). Each is a
        # Card(168) parented to DECK -> on readFrom the pile's child vector links them, so StartOfGame's
        # kWaitForDeck sees a populated deck and DEALS. childmap {3:deck_ids} kept for shape parity.
        deck_ids = [base + 100 + k for k in range(deck_cards)]
        deck_contents = {3: deck_ids} if deck_ids else None
        zones += [
            {"id": AV,   "classid": CARD,          "owner": acct, "parent": 0, "catalog_id": avatar},
            {"id": P,    "classid": PLAYERPLAYAREA, "owner": acct, "areatype": 0, "pile_refs": [DECK, PIL4, HAND]},
            {"id": DECK, "classid": PILEPLAYAREA,   "owner": acct, "areatype": 3, "contents": deck_contents},
            {"id": PIL4, "classid": PILEPLAYAREA,   "owner": acct, "areatype": 1},
            {"id": HAND, "classid": PILEPLAYAREA,   "owner": acct, "areatype": 1, "contents": {3: hand_ids}},
            {"id": PA6,  "classid": PLAYAREA,       "owner": acct, "areatype": 2},
            {"id": PIL7, "classid": PILEPLAYAREA,   "owner": acct, "areatype": 1},
            {"id": PIL8, "classid": PILEPLAYAREA,   "owner": acct, "areatype": 1},
            {"id": PA9,  "classid": PLAYAREA,       "owner": acct, "areatype": 2},
        ]
        for k, hid in enumerate(hand_ids):
            zones.append({"id": hid, "classid": CARD, "owner": acct, "parent": HAND,
                          "catalog_id": main_pool[k % len(main_pool)]})
        for k, did in enumerate(deck_ids):
            zones.append({"id": did, "classid": CARD, "owner": acct, "parent": DECK,
                          "catalog_id": deck_pool[k % len(deck_pool)]})
        player_data.append({"objid": acct, "ppa_id": P, "card_id": AV})

    b = i(166) + i(V)
    # HEADER
    b += i(G) + i(0)                            # GameID, MatchID
    if V >= 0x19: b += i(0)                     # ServerID (ver>=0x19)
    b += i(a0)                                  # mFirstPlayerID
    b += i(gst) + i(0) + i(0) + i(0)            # mGameStarted (field#5), AI, GameEnded, OutOfSync
    b += i(0) + i(2) + i(2)                     # BatchControl, PlayerCount=2, OrigPlayerCount=2
    b += i(gis) + i(1 if rfs else 0)            # GameIsSetup (field#12, game+0x168), ReadyForStart (field#13 = mGameIsReadyForStart game+0x169)
    b += i(0x6a4267dd) + i(0) + i(qm) + i(0)    # RandomSeed, Version, QueueMode (field#16 -> game+0x168), displayState
    b += s("")                                  # MatchDirectory
    # CORRECTED field mapping (RE agent a6445e07, 3x debugger-confirmed): the render gate the gameScreen vt55
    # predicate (FUN_103f49a0) reads is game+0x1C4 = mGameOver (getter FUN_100cef10 = "mov al,[ecx+0x1C4]"),
    # and it is READ STRAIGHT FROM THE BLOB at Game::deserialize FUN_100fca50:284 = the +0x71 field (NOT +0x6d;
    # +0x6d = byte 0x1B4, a different bool). The earlier "+0x6d = hasCurrentState, runtime-set" note was a
    # field-offset mislabel. Setting +0x71 = 1 marks the game OVER -> vt55 does not bounce -> a static board
    # renders. The clean playable path (screen+0xed via a live SM) is a follow-up.
    b += i(0)                                   # 19 RevealID (+0x69)
    b += i(0)                                   # 20 (+0x6d -> Game+0x1B4 bool; not the render gate)
    b += i(1 if go else 0)                      # 21 (+0x71 -> Game+0x1C4 = mGameOver): 1 forces a static render
    b += i(0)                                   # 22 GetTargetID (+0x75)
    # CONTAINERS
    b += i(2) + i(a0) + i(a1)                   # PlayerOrderData
    b += i(2) + i(a0) + i(a1)                   # OrderedAccountIDs
    b += i(2) + i(a0) + i(a1)                   # PlayerIDList
    b += i(0) + i(0)                            # vec26, vec27
    # map28 (objId->classId) = EMPTY. Player(261) is NOT factory-registered (REG4 scan: 261 absent,
    # like 80005) -- a HIT here calls ComponentFactory::create(261) -> null -> AV ("Couldn't get class
    # 261"). A MISS takes the SHELL path (operator_new(0x4c)+FUN_10186a00 in FUN_100fca50's player loop),
    # which creates a base Player WITHOUT the factory. So leave map28 empty; players come from PlayerIDList
    # via the shell path, then their item-33 readFroms (begin 261 is a DIRECT-READ assert, not a factory).
    b += i(0)                                   # map28 EMPTY -> shell players (no factory-create of 261)
    b += i(len(player_data))                    # player readFroms (blobvec)
    for pd in player_data:
        buf = _player_buffer(G, pd["ppa_id"], pd["card_id"], "")
        b += i(pd["objid"]) + i(len(buf)) + buf
    b += i(0) + i(0)                            # RevealMap, PlayerDrawMap
    b += i(len(zones))                          # element shells (id, classId)
    for z in zones:
        b += i(z["id"]) + i(z["classid"])
    entries = []                                # element readFroms (id, len, buf)
    for z in zones:
        if z["classid"] == CARD:
            env = _card_envelope(G, z["id"], z["catalog_id"], z["owner"], z["parent"])
        else:
            env = _zone_envelope(z["classid"], G, z["id"], z["owner"], z.get("parent", 0),
                                 pile_refs=z.get("pile_refs"), contents=z.get("contents"),
                                 areatype=z.get("areatype"))
        entries.append((z["id"], env))
    b += i(len(entries))
    for eid, env in entries:
        b += i(eid) + i(len(env)) + env
    b += i(99999)                               # MAGIC (item 34)
    # POST-MAGIC: 4 containers (FUN_100fca50 asserts 0x248e/0x24b7/0x24c6/0x24cf; GSER28 serialize order:
    # InstalledActions, Ignore Active, mValidActionFilters, CommandCardDuration). All empty (count=0).
    b += i(0)                                   # InstalledActions  map  FUN_10064e60(local_b0) [0x248e]
    b += i(0)                                   # Ignore Active     vec  FUN_1000b2a0(local_e4) [0x24b7]
    b += i(0)                                   # mValidActionFilt  vec  FUN_1000b2a0(+0x3f)    [0x24c6]
    b += i(0)                                   # CommandCardDurn   map  FUN_100ef480(local_c0) [0x24cf]
    # GAMETURN
    b += _base_gameturn(G, gtV, current_player=a0)
    # VER-GATED GAME FIELDS -- ORDER from GSER28 (the serialize side = byte-exact ground truth; the raw
    # FUN_100fca50 decompile control-flow misled the reading, e.g. it hid PlayerActionCountMap/ExtraFormatFlag).
    if V > 1:    b += i(0)                      # RevealedCards map
    if V > 2:    b += i(0)                      # IgnorePlayerList vint
    if V > 4:    b += i(0) + i(0)               # WinMap, ElapsedTime vpair
    if V > 5:    b += i(0)                      # Duration int
    if V > 6:    b += i(0)                      # RunTimer bool
    if V >= 8:   b += i(0)                      # ClonePlayElementMap vpair (ver>=8 path; was mislabeled PlayerActionCountMap)
    if V > 9:    b += i(0)                      # PlayType int
    if V > 10:   b += i(0)                      # LossTypeMap vpair
    if V >= 0xc: b += i(0)                      # WhoSelectedCard int
    if V > 0xc:  b += i(0)                      # InstalledActionCardMap vpair
    if V > 0xd:  b += i(0)                      # PrePassMap vpair (+0xa6)
    if V > 0xd:  b += i(0)                      # PlayerActionCountMap (+0xa9) map  <-- GSER28-found; FIXES 9583 drift
    if V > 0xd:  b += i(0)                      # ExtraFormatFlag (+0xac) int        <-- GSER28-found; FIXES 9583 drift
    if V > 0xf:  b += i(0)                      # suppressedGameTextCards vint
    if V >= 0x12: b += i(0)                     # GameNum int
    if V >= 0x15: b += i(0)                     # leagueID int
    b += i(0)                                   # map(+0x2e)    FUN_100c94c0   [0x2555] unconditional
    b += i(0)                                   # TurnNumber int               [0x2558] unconditional
    b += i(0)                                   # vpair(+0xba)  FUN_1005c310   [0x255e] unconditional
    if V > 0x19:  b += i(0)                     # vint(+8)      [0x2563]  (0x19 < ver)
    if V >= 0x1d: b += i(0) + i(0)              # +0xbd bool + map(+0xbe)  [0x2568/0x256f]
    # STATEMACHINE (separate present-codec) @ver>=0x1f; MatchStructure @ver>0x1f
    if V >= 0x1f:
        b += i(0) + _statemachine(G, smV, state_classid=stc, with_state=wss)   # present + SM(233); wss=False -> EMPTY
        #   state stack (pre-start boot: the client's advanceTurn creates the real EQStartOfGameState itself)
    if V > 0x1f:  b += i(0)                     # MatchStructure int (+0xcb)  [0x257d]
    if V > 0x24:  b += i(0)                     # vint(local_134)  [0x2584]
    if V > 0x2a:  b += i(0)                     # +0xd9 int        [0x2593]
    if V > 0x2e:  b += i(0)                     # map(+0x8d)       [0x259b]
    if V > 0x2f:  b += i(0)                     # +0x36a bool      [0x25a1]
    # ver<0x31 -> final trailer has no further wire reads
    return b


# ---- structural verifier: decode the header the way Game::deserialize (FUN_100fca50) reads it ----
def decode_header(blob, ver_ge_0x19=False):
    """Parse the Game envelope header back out (mirrors FUN_100fca50) to confirm field values + order."""
    from swgcodec import dec_int, dec_str
    o = {}
    p = 0
    cid, p = dec_int(blob, p); env_ver, p = dec_int(blob, p)
    o["envelope_classid"], o["envelope_ver"] = cid, env_ver
    o["GameID"], p = dec_int(blob, p)
    o["MatchID"], p = dec_int(blob, p)
    if ver_ge_0x19:
        o["ServerID"], p = dec_int(blob, p)
    o["mFirstPlayerID"], p = dec_int(blob, p)
    o["mGameStarted"], p = dec_int(blob, p)
    o["mAIEnabled"], p = dec_int(blob, p)
    o["GameEnded"], p = dec_int(blob, p)
    o["OutOfSync"], p = dec_int(blob, p)
    o["BatchControlStatus"], p = dec_int(blob, p)
    o["PlayerCount"], p = dec_int(blob, p)
    o["OrigPlayerCount"], p = dec_int(blob, p)
    o["GameIsSetup"], p = dec_int(blob, p)
    o["ReadyForStart"], p = dec_int(blob, p)
    o["RandomSeed"], p = dec_int(blob, p)
    o["Version"], p = dec_int(blob, p)
    o["QueueMode"], p = dec_int(blob, p)
    o["displayState"], p = dec_int(blob, p)
    o["MatchDirectory"], p = dec_str(blob, p)
    o["RevealID"], p = dec_int(blob, p)          # +0x69
    o["field_0x6d"], p = dec_int(blob, p)        # +0x6d -> Game+0x1B4 bool
    o["mGameOver"], p = dec_int(blob, p)         # +0x71 -> Game+0x1C4 (the vt55 render gate; 1 = static render)
    o["GetTargetID"], p = dec_int(blob, p)       # +0x75
    o["_header_end_offset"] = p
    return o


if __name__ == "__main__":
    blob = serialize_minimal_game(200)
    print("legacy minimal game blob: %d bytes" % len(blob))
    b2 = build_2p_game(800, accounts=(1, 2))
    print("2p started board blob:    %d bytes" % len(b2))
    print("2p header decode:")
    for k, v in decode_header(b2).items():
        print("   %-20s %r" % (k, v))
