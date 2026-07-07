"""Parse a client AddOnlineDeck(361) upload into a deck dict -- the byte-exact inverse of
swgtcg_server.build_eqdeck_subobject. Confirmed against the client's own uploads
(captures/srv_18xxxx_p16783_c2.bin): the frame payload AFTER the 12-byte CLIENT_HDR is

    env(361,1) x3  +  baseInt  +  [present EQDeck(80002) sub-object]

EQDeck (v>=6): deck_id, str2, deck_name, str4, +0x74 bool, MAIN list (one entry PER COPY),
  [v>1 +0x88], [v>5 +0x89,+0x8a], [v>7 +0x8c], [v>9 +0x90], avatar_catid, +0x98,
  QUEST list, [v>6 embedded avatar Card]. Each MAIN/QUEST entry = (cardId, 0).

We extract deck_id / name / main (collapsed to (catalog_id, qty)) / avatar / quests. The
embedded avatar Card (+0xac) is re-derived on push via load_avatar_subobj, so we stop before it.
"""
from swgcodec import dec_int, dec_str

EQDECK_CLASSID = 80002
CMD_ADD_ONLINE_DECK = 361


def _card_seq(buf, i):
    """Read [count][count x (cardId, 0)] -> (list_of_cardIds, new_i)."""
    n, i = dec_int(buf, i)
    out = []
    for _ in range(n):
        cid, i = dec_int(buf, i)
        _, i = dec_int(buf, i)          # trailing 0 per entry
        out.append(cid)
    return out, i


def parse_add_online_deck(body):
    """`body` = the 361 frame payload AFTER the 12-byte client header (what dispatch passes as `body`).
    Returns {wire_deck_id, name, main:[(catalog_id, qty)], avatar, quests} or None on any mismatch."""
    try:
        i = 0
        for _ in range(3):
            c, i = dec_int(body, i); _, i = dec_int(body, i)    # env(361,ver) x3
            if c != CMD_ADD_ONLINE_DECK:
                return None
        _, i = dec_int(body, i)                                 # DeckCommand base int
        present, i = dec_int(body, i)                           # EQDeck present flag (0 = present)
        if present != 0:
            return None
        cls, i = dec_int(body, i); ver, i = dec_int(body, i)    # EQDeck begin (80002, ver)
        if cls != EQDECK_CLASSID:
            return None
        _, i = dec_int(body, i); _, i = dec_int(body, i)        # Deck-base begin (80002, ver)
        deck_id, i = dec_str(body, i)
        _str2, i = dec_str(body, i)
        deck_name, i = dec_str(body, i)
        _str4, i = dec_str(body, i)
        _, i = dec_int(body, i)                                 # +0x74 bool
        main_ids, i = _card_seq(body, i)                        # +0x78 MAIN (per-copy)
        if ver > 1: _, i = dec_int(body, i)                     # +0x88
        if ver > 5: _, i = dec_int(body, i); _, i = dec_int(body, i)   # +0x89,+0x8a
        if ver > 7: _, i = dec_int(body, i)                     # +0x8c
        if ver > 9: _, i = dec_int(body, i)                     # +0x90
        avatar, i = dec_int(body, i)                            # +0x94 avatar catalog id
        _, i = dec_int(body, i)                                 # +0x98
        quests, i = _card_seq(body, i)                          # +0x9c QUEST list
        # (+0xac embedded avatar Card follows for v>6; not parsed -- re-derived on push)

        # collapse the per-copy MAIN list into (catalog_id, qty), preserving first-seen order
        counts, order = {}, []
        for c in main_ids:
            if c not in counts:
                order.append(c)
            counts[c] = counts.get(c, 0) + 1
        main = [(c, counts[c]) for c in order]
        return {"wire_deck_id": deck_id, "name": deck_name, "main": main,
                "avatar": avatar, "quests": quests}
    except Exception:
        return None
