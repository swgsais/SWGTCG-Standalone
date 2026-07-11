"""SQLite persistence for the SWG TCG platform.

Layering: swgtcg_server.py -> db.py -> (auth.py, standalone_cards.py, config.py).
The wire/codec layer never imports this module.

Card metadata source of truth = standalone_cards.load_cards() (parsed from the
standalone client's storage.dat + .cln). The `card_catalog` table is a DERIVED,
rebuildable cache (an FK target for collections/decks + web-panel joins), never
the source of truth -- rebuild it any time with rebuild_card_catalog().
"""
import os
import sqlite3

import auth
import config

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    pw_algo       TEXT    NOT NULL DEFAULT 'pbkdf2_sha256',
    pw_iterations INTEGER NOT NULL,
    pw_salt       BLOB    NOT NULL,
    pw_hash       BLOB    NOT NULL,
    display_name  TEXT,
    status        TEXT    NOT NULL DEFAULT 'active',   -- active | disabled | banned
    last_deck_id  INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS account_entitlements (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    entitlement TEXT    NOT NULL,
    granted_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, entitlement)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT    PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    username     TEXT    NOT NULL,
    challenge    TEXT,
    character_id INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT    NOT NULL,
    consumed_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_account ON sessions(account_id);

CREATE TABLE IF NOT EXISTS collections (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    catalog_id INTEGER NOT NULL,
    qty        INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
    PRIMARY KEY (account_id, catalog_id)
);

CREATE TABLE IF NOT EXISTS decks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name              TEXT    NOT NULL,
    wire_deck_id      TEXT    NOT NULL,
    avatar_catalog_id INTEGER,
    is_starter        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS deck_cards (
    deck_id    INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    catalog_id INTEGER NOT NULL,
    qty        INTEGER NOT NULL DEFAULT 1 CHECK (qty >= 1),
    slot       TEXT    NOT NULL DEFAULT 'main',   -- main | quest
    PRIMARY KEY (deck_id, catalog_id, slot)
);

CREATE TABLE IF NOT EXISTS card_catalog (
    catalog_id      INTEGER PRIMARY KEY,
    name            TEXT,
    type            TEXT,                          -- Avatar|Unit|Ability|Item|Tactic|Quest|NULL
    rarity          TEXT,
    set_num         INTEGER,
    collector_num   INTEGER,
    collectorinfo   TEXT,
    cost            INTEGER,
    attack          INTEGER,
    defense         INTEGER,
    health_or_level INTEGER,
    is_card         INTEGER NOT NULL DEFAULT 1     -- products (type=NULL) -> 0
);

-- ---- v2: server-driven content (home screen, leaderboard, events, tournaments) ----

-- Generic key/value settings (MOTD text, feature toggles, etc.). String values.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Home-screen news / announcements ticker. Maps to NetworkCommand_News(457):
-- parallel arrays headline[] / body[] / id[]. Ordered by sort then id.
CREATE TABLE IF NOT EXISTS news (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    headline   TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    sort       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Per-account win/loss + rating, feeds NetworkCommand_LeaderBoardData(458).
CREATE TABLE IF NOT EXISTS player_stats (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    wins       INTEGER NOT NULL DEFAULT 0,
    losses     INTEGER NOT NULL DEFAULT 0,
    draws      INTEGER NOT NULL DEFAULT 0,
    rating     INTEGER NOT NULL DEFAULT 1000,
    streak     INTEGER NOT NULL DEFAULT 0,          -- + = win streak, - = loss streak
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Seasonal / special events. A tournament may belong to an event (or stand alone).
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    kind        TEXT    NOT NULL DEFAULT 'seasonal', -- seasonal | tournament | special
    format      TEXT,                                -- deck restriction, e.g. 'standard' | 'set:3' | 'sealed'
    starts_at   TEXT,
    ends_at     TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tournaments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER REFERENCES events(id) ON DELETE SET NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    format      TEXT    NOT NULL DEFAULT 'standard', -- deck restriction
    state       TEXT    NOT NULL DEFAULT 'open',      -- open | locked | running | complete | cancelled
    max_players INTEGER NOT NULL DEFAULT 8,
    round       INTEGER NOT NULL DEFAULT 0,           -- current round (0 = not started)
    starts_at   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tournament_entries (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    deck_id       INTEGER REFERENCES decks(id) ON DELETE SET NULL,
    seed          INTEGER,
    wins          INTEGER NOT NULL DEFAULT 0,
    losses        INTEGER NOT NULL DEFAULT 0,
    dropped       INTEGER NOT NULL DEFAULT 0,
    joined_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tournament_id, account_id)
);

-- One row per pairing per round (the bracket). player_b NULL = a bye. winner = account_id, or 0 = draw,
-- or NULL = not yet reported.
CREATE TABLE IF NOT EXISTS tournament_matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    round         INTEGER NOT NULL,
    table_no      INTEGER NOT NULL,
    player_a      INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    player_b      INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    winner        INTEGER,
    reported      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tournament_id, round, table_no)
);

-- Single-player CAMPAIGN / SCENARIO progress. The client is authoritative: on each
-- scenario play it emits AccountCommand_SetCampaignStatus(415) (per-campaign status)
-- and cid 487 (a per-scenario report carrying the scenario id string). The stock server
-- dropped both, so progress reset to a blank campaign tree on every relaunch. We persist
-- the RAW command body per (account, cid, item_key) latest-wins, and replay it verbatim
-- at login so completed scenarios + unlocked rewards show again.
CREATE TABLE IF NOT EXISTS campaign_progress (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    cid        INTEGER NOT NULL,               -- 415 = SetCampaignStatus, 487 = scenario report
    item_key   TEXT    NOT NULL,               -- campaign id (415) or scenario id string (487)
    payload    BLOB    NOT NULL,               -- the raw command body (kept for reference/debug)
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, cid, item_key)
);

-- Structured scenario completion. This is the REAL save state: on a scenario WIN the client sends
-- AccountCommand_SetCampaignStatus(415) carrying (chainNodeId 0x157xx, scenarioIndex 1..5 within the
-- chain (tutorials 1..11), difficulty 1/2/3, archetypeId 0x13886 rebel/0x13887 sith/0x13888 jedi/
-- 0x13889 imperial) -- byte-verified against the exe writer FUN_006409d0 + live captures. We store one
-- row per cleared (node, index, archetype, difficulty) and at login rebuild BOTH the flat per-node
-- unlock property (type-2 int = furthest scenario index) and the 0x1054 detail map
-- (node -> {scenarioIndex -> {archetypeId -> IntegerList[difficulties]}}) on IntroduceAccount(114).
CREATE TABLE IF NOT EXISTS scenario_completion (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    node_id     INTEGER NOT NULL,              -- 0x157xx campaign CHAIN node id (from the 415)
    scenario_index INTEGER NOT NULL,           -- 1..5 scenario within the chain (tutorials 1..11)
    archetype_id INTEGER NOT NULL,             -- 0x13886 rebel/0x13887 sith/0x13888 jedi/0x13889 imperial
    difficulty  INTEGER NOT NULL,              -- 1 easy, 2 medium, 3 hard
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, node_id, scenario_index, archetype_id, difficulty)
);

-- Scenario reward-card grants (one per unique win coordinate, per campaign.dat's "Receive one reward
-- card for winning this scenario with both archetypes on Easy, Medium, or Hard difficulty (6 possible)").
-- difficulty=0 rows are the "each different archetype" standalone-scenario variant (difficulty-agnostic).
CREATE TABLE IF NOT EXISTS scenario_rewards (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    scenario_id TEXT    NOT NULL,              -- e.g. 'COTF_Scenario01' (from scenario_rewards.json)
    archetype_id INTEGER NOT NULL,
    difficulty  INTEGER NOT NULL,              -- 1/2/3, or 0 for the per-archetype variant
    catalog_id  INTEGER NOT NULL,              -- the reward card granted to collections
    granted_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, scenario_id, archetype_id, difficulty)
);

-- Per-account client preferences delivered via AccountCommand_SetPreferences(118). The 118 propset is
-- a PARTIAL update (each UI path sends only its own attrs: prefs dialog 0x1005/0x1006/0x1007 avatar,
-- welcome-seen 0x4c4, campaign writer 0x1054+0x157xx), so we store per-ATTRIBUTE raw ValueData bytes
-- and replay them on the IntroduceAccount(114) propset (skipping server-rebuilt attrs).
CREATE TABLE IF NOT EXISTS account_prefs (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    attr_id    INTEGER NOT NULL,               -- e.g. 0x1007 = settings avatar (String "avatar_NN")
    value      BLOB    NOT NULL,               -- RAW ValueData bytes exactly as the client sent them
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, attr_id)
);

-- Learned scenario-string -> numeric nodeId map (the node ids are load-time ForcedIDs, not stored in
-- any static table). Populated by correlating a 487 scenario-report (carries the string) with the
-- 415 (carries the node id) from the same session. Lets the manager's Grant target scenarios by name.
CREATE TABLE IF NOT EXISTS scenario_nodemap (
    scenario_id TEXT    PRIMARY KEY,           -- e.g. 'COTF_Scenario06'
    node_id     INTEGER NOT NULL,
    archetype_id INTEGER,                       -- an archetype seen for it (informational)
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


# ==========================================================================
# connection / schema
# ==========================================================================
def connect(path=None):
    conn = sqlite3.connect(path or config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    # Rollback (DELETE) journal, NOT WAL. This is a single local writer; WAL buys nothing
    # here and actively breaks the launcher's edit flow: the server is force-killed on Stop
    # (no clean checkpoint), so committed writes would be stranded in swgtcg.db-wal, which the
    # browser Collection Manager (sql.js) cannot read -- it opens only the main .db file. That
    # split caused edits to vanish and, when a swapped-in db had a different page layout, the
    # stale -wal replayed onto it and corrupted the file. DELETE mode folds every commit into
    # the main .db immediately, so the manager always sees current data and a kill strands nothing.
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def init_db(conn):
    conn.executescript(DDL)
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    _migrate(conn)
    return conn


def _migrate(conn):
    """Apply forward migrations based on schema_meta.version.
    v1 = base. v2 = home-screen/leaderboard/events/tournaments tables (already created by the
    IF NOT EXISTS DDL that init_db runs every startup, so v1->v2 only bumps the recorded version).
    v3 = scenario_completion gains scenario_index (the 415's field order was mis-decoded before v3:
    scenario index was stored as difficulty, difficulty as archetype -- every pre-v3 row is junk, so
    the table is recreated empty from the corrected DDL) + scenario_rewards + account_prefs tables."""
    ver = int(conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()["value"])
    if ver < 3:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(scenario_completion)")]
        if cols and "scenario_index" not in cols:
            conn.execute("DROP TABLE scenario_completion")
            conn.executescript(DDL)          # recreate with the v3 shape (+ the new v3 tables)
    if ver < SCHEMA_VERSION:
        conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'version'", (str(SCHEMA_VERSION),))
        conn.commit()
        ver = SCHEMA_VERSION
    return ver


# ==========================================================================
# accounts
# ==========================================================================
def create_account(conn, username, password, entitlements=None, display_name=None):
    salt, h, iters = auth.hash_password(password)
    cur = conn.execute(
        "INSERT INTO accounts(username, pw_algo, pw_iterations, pw_salt, pw_hash, display_name) "
        "VALUES (?,?,?,?,?,?)",
        (username, auth.PW_ALGO, iters, salt, h, display_name or username))
    account_id = cur.lastrowid
    ents = entitlements if entitlements is not None else config.DEFAULT_ENTITLEMENTS
    for e in ents:
        conn.execute("INSERT OR IGNORE INTO account_entitlements(account_id, entitlement) VALUES (?,?)",
                     (account_id, e))
    conn.commit()
    return account_id


def verify_login(conn, username, password):
    """Return account_id on success, else None. Only 'active' accounts may log in."""
    row = conn.execute(
        "SELECT id, pw_salt, pw_hash, pw_iterations, status FROM accounts WHERE username = ?",
        (username,)).fetchone()
    if row is None or row["status"] != "active":
        return None
    if auth.verify_password(password, row["pw_salt"], row["pw_hash"], row["pw_iterations"]):
        return row["id"]
    return None


def load_account(conn, account_id):
    return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


# ==========================================================================
# campaign / scenario progress (raw-frame store + verbatim replay)
# ==========================================================================
def save_campaign_frame(conn, account_id, cid, item_key, payload):
    """Upsert one campaign/scenario progress frame (latest-wins per key). `payload` is
    the raw command body the client sent; it is stored as-is and re-sent verbatim at login."""
    conn.execute(
        "INSERT INTO campaign_progress(account_id, cid, item_key, payload, updated_at) "
        "VALUES (?,?,?,?, datetime('now')) "
        "ON CONFLICT(account_id, cid, item_key) DO UPDATE SET "
        "payload=excluded.payload, updated_at=excluded.updated_at",
        (account_id, cid, item_key, sqlite3.Binary(payload)))
    conn.commit()


def load_campaign_frames(conn, account_id):
    """All stored progress frames for an account, oldest-first per cid. Returns
    [(cid, item_key, payload_bytes), ...] for verbatim replay in the login sequence."""
    rows = conn.execute(
        "SELECT cid, item_key, payload FROM campaign_progress "
        "WHERE account_id=? ORDER BY cid, updated_at", (account_id,)).fetchall()
    return [(r["cid"], r["item_key"], bytes(r["payload"])) for r in rows]


# ---- structured scenario completion (drives the flat unlock props + property 0x1054) ----
def record_scenario_completion(conn, account_id, node_id, scenario_index, archetype_id, difficulty):
    """Mark one (node, index, archetype, difficulty) cleared. Returns True if this is a NEW
    completion (first win on that exact coordinate), False if it was already recorded."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO scenario_completion"
        "(account_id, node_id, scenario_index, archetype_id, difficulty, updated_at) "
        "VALUES (?,?,?,?,?, datetime('now'))",
        (account_id, node_id, scenario_index, archetype_id, difficulty))
    conn.commit()
    return cur.rowcount > 0


def load_scenario_completion(conn, account_id):
    """-> {node_id: {scenario_index: {archetype_id: [difficulties...]}}} for the login props."""
    out = {}
    for r in conn.execute("SELECT node_id, scenario_index, archetype_id, difficulty FROM scenario_completion "
                          "WHERE account_id=? ORDER BY node_id, scenario_index, archetype_id, difficulty",
                          (account_id,)):
        out.setdefault(r["node_id"], {}).setdefault(r["scenario_index"], {}) \
           .setdefault(r["archetype_id"], []).append(r["difficulty"])
    return out


# ---- scenario reward-card grants ----
def record_scenario_reward(conn, account_id, scenario_id, archetype_id, difficulty, catalog_id):
    """Grant a scenario reward card once per unique (scenario, archetype, difficulty) win.
    Returns True (and adds the card to the collection) only on the first claim."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO scenario_rewards"
        "(account_id, scenario_id, archetype_id, difficulty, catalog_id, granted_at) "
        "VALUES (?,?,?,?,?, datetime('now'))",
        (account_id, scenario_id, archetype_id, difficulty, catalog_id))
    if cur.rowcount > 0:
        add_to_collection(conn, account_id, catalog_id, 1)
        return True
    conn.commit()
    return False


# ---- per-account client preferences (AccountCommand_SetPreferences 118) ----
def save_account_pref(conn, account_id, attr_id, value):
    """Upsert one preference attribute's RAW ValueData bytes (latest-wins per attr)."""
    conn.execute(
        "INSERT INTO account_prefs(account_id, attr_id, value, updated_at) "
        "VALUES (?,?,?, datetime('now')) "
        "ON CONFLICT(account_id, attr_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (account_id, attr_id, sqlite3.Binary(value)))
    conn.commit()


def load_account_prefs(conn, account_id):
    """-> [(attr_id, raw_valuedata_bytes), ...] for replay on the IntroduceAccount(114) propset."""
    return [(r["attr_id"], bytes(r["value"]))
            for r in conn.execute("SELECT attr_id, value FROM account_prefs WHERE account_id=? "
                                  "ORDER BY attr_id", (account_id,))]


def clear_scenario_completion(conn, account_id, node_id=None):
    if node_id is None:
        conn.execute("DELETE FROM scenario_completion WHERE account_id=?", (account_id,))
    else:
        conn.execute("DELETE FROM scenario_completion WHERE account_id=? AND node_id=?", (account_id, node_id))
    conn.commit()


def learn_scenario_node(conn, scenario_id, node_id, archetype_id=None):
    """Record a scenario-string -> nodeId pairing (from correlating a 487 report with a 415)."""
    conn.execute(
        "INSERT INTO scenario_nodemap(scenario_id, node_id, archetype_id, updated_at) "
        "VALUES (?,?,?, datetime('now')) ON CONFLICT(scenario_id) DO UPDATE SET "
        "node_id=excluded.node_id, archetype_id=COALESCE(excluded.archetype_id, scenario_nodemap.archetype_id), "
        "updated_at=excluded.updated_at", (scenario_id, node_id, archetype_id))
    conn.commit()


def scenario_node(conn, scenario_id):
    r = conn.execute("SELECT node_id, archetype_id FROM scenario_nodemap WHERE scenario_id=?", (scenario_id,)).fetchone()
    return (r["node_id"], r["archetype_id"]) if r else (None, None)


def scenario_nodemap(conn):
    return {r["scenario_id"]: r["node_id"] for r in conn.execute("SELECT scenario_id, node_id FROM scenario_nodemap")}


def account_by_username(conn, username):
    return conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()


def load_entitlements(conn, account_id):
    rows = conn.execute(
        "SELECT entitlement FROM account_entitlements WHERE account_id = ? ORDER BY entitlement",
        (account_id,)).fetchall()
    return [r["entitlement"] for r in rows]


def grant_entitlement(conn, account_id, entitlement):
    conn.execute("INSERT OR IGNORE INTO account_entitlements(account_id, entitlement) VALUES (?,?)",
                 (account_id, entitlement))
    conn.commit()


def revoke_entitlement(conn, account_id, entitlement):
    conn.execute("DELETE FROM account_entitlements WHERE account_id = ? AND entitlement = ?",
                 (account_id, entitlement))
    conn.commit()


def set_status(conn, account_id, status):
    conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id))
    conn.commit()


def set_password(conn, account_id, password):
    """Reset an account's password (re-hash with a fresh salt). Returns True if the account existed."""
    salt, h, iters = auth.hash_password(password)
    cur = conn.execute(
        "UPDATE accounts SET pw_algo=?, pw_iterations=?, pw_salt=?, pw_hash=? WHERE id=?",
        (auth.PW_ALGO, iters, salt, h, account_id))
    conn.commit()
    return cur.rowcount > 0


def list_accounts(conn):
    """Admin overview: one row per account with derived counts. Returns list of dicts."""
    rows = conn.execute("SELECT id, username, status, created_at, last_login_at, last_deck_id "
                        "FROM accounts ORDER BY id").fetchall()
    out = []
    for r in rows:
        ents = conn.execute("SELECT COUNT(*) AS n FROM account_entitlements WHERE account_id=?",
                            (r["id"],)).fetchone()["n"]
        ndecks = conn.execute("SELECT COUNT(*) AS n FROM decks WHERE account_id=?",
                              (r["id"],)).fetchone()["n"]
        ncards = conn.execute("SELECT COALESCE(SUM(qty),0) AS n FROM collections WHERE account_id=?",
                              (r["id"],)).fetchone()["n"]
        out.append({"id": r["id"], "username": r["username"], "status": r["status"],
                    "entitlements": ents, "decks": ndecks, "cards": ncards,
                    "last_login_at": r["last_login_at"], "created_at": r["created_at"],
                    "last_deck_id": r["last_deck_id"]})
    return out


# ==========================================================================
# sessions (launcher-issued tokens; the authoritative login gate)
# ==========================================================================
def create_session(conn, account_id, username, character_id=1, ttl_seconds=None, challenge=None):
    sid = auth.new_session_token()
    challenge = challenge or auth.new_challenge()
    ttl = ttl_seconds if ttl_seconds is not None else config.SESSION_TTL_SECONDS
    conn.execute(
        "INSERT INTO sessions(session_id, account_id, username, challenge, character_id, expires_at) "
        "VALUES (?,?,?,?,?, datetime('now', ?))",
        (sid, account_id, username, challenge, character_id, "+%d seconds" % ttl))
    conn.commit()
    return sid


def peek_session(conn, session_id):
    """Validate a session WITHOUT consuming it (the soft gate at the gateway). Row or None."""
    return conn.execute(
        "SELECT * FROM sessions WHERE session_id = ? AND expires_at > datetime('now')",
        (session_id,)).fetchone()


def bind_session(conn, session_id):
    """Consume a session at lobby login. Returns the accounts row, or None if invalid/expired."""
    row = peek_session(conn, session_id)
    if row is None:
        return None
    conn.execute("UPDATE sessions SET consumed_at = datetime('now') WHERE session_id = ?", (session_id,))
    conn.execute("UPDATE accounts SET last_login_at = datetime('now') WHERE id = ?", (row["account_id"],))
    conn.commit()
    return load_account(conn, row["account_id"])


def expire_session(conn, session_id):
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    conn.commit()


def purge_expired_sessions(conn):
    conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
    conn.commit()


# ==========================================================================
# collections
# ==========================================================================
def load_collection(conn, account_id):
    """Return [(catalog_id, qty), ...] (qty > 0) for build_collection_sync."""
    rows = conn.execute(
        "SELECT catalog_id, qty FROM collections WHERE account_id = ? AND qty > 0 ORDER BY catalog_id",
        (account_id,)).fetchall()
    return [(r["catalog_id"], r["qty"]) for r in rows]


def add_to_collection(conn, account_id, catalog_id, qty=1):
    conn.execute(
        "INSERT INTO collections(account_id, catalog_id, qty) VALUES (?,?,?) "
        "ON CONFLICT(account_id, catalog_id) DO UPDATE SET qty = qty + excluded.qty",
        (account_id, catalog_id, qty))
    conn.commit()


# ==========================================================================
# decks
# ==========================================================================
def _next_wire_deck_id(conn):
    n = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM decks").fetchone()["n"]
    return "deck_%d" % n


def create_deck(conn, account_id, name, main, avatar, quests, wire_deck_id=None, is_starter=0):
    """main = [(catalog_id, qty), ...]; quests = [catalog_id, ...]; avatar = catalog_id.
    Avatar lives on the deck row (decks.avatar_catalog_id), not in deck_cards -- matching
    build_eqdeck_subobject's split. Commits the whole deck (incl. any pending collection rows)."""
    wid = wire_deck_id or _next_wire_deck_id(conn)
    cur = conn.execute(
        "INSERT INTO decks(account_id, name, wire_deck_id, avatar_catalog_id, is_starter) "
        "VALUES (?,?,?,?,?)",
        (account_id, name, wid, avatar, is_starter))
    deck_id = cur.lastrowid
    for cid, qty in main:
        conn.execute("INSERT INTO deck_cards(deck_id, catalog_id, qty, slot) VALUES (?,?,?, 'main')",
                     (deck_id, cid, qty))
    for cid in quests:
        conn.execute("INSERT OR IGNORE INTO deck_cards(deck_id, catalog_id, qty, slot) VALUES (?,?,1, 'quest')",
                     (deck_id, cid))
    conn.commit()
    return deck_id


def load_decks(conn, account_id):
    """Return [{id, wire_deck_id, name, avatar, main:[(cid,qty)], quests:[cid], is_starter}]
    shaped for build_eqdeck_subobject(wire_deck_id, name, main, avatar, quests, ...)."""
    out = []
    for d in conn.execute("SELECT * FROM decks WHERE account_id = ? ORDER BY id", (account_id,)).fetchall():
        main = [(r["catalog_id"], r["qty"]) for r in conn.execute(
            "SELECT catalog_id, qty FROM deck_cards WHERE deck_id = ? AND slot = 'main' ORDER BY catalog_id",
            (d["id"],)).fetchall()]
        quests = [r["catalog_id"] for r in conn.execute(
            "SELECT catalog_id FROM deck_cards WHERE deck_id = ? AND slot = 'quest' ORDER BY catalog_id",
            (d["id"],)).fetchall()]
        out.append({"id": d["id"], "wire_deck_id": d["wire_deck_id"], "name": d["name"],
                    "avatar": d["avatar_catalog_id"], "main": main, "quests": quests,
                    "is_starter": d["is_starter"]})
    return out


def save_deck(conn, account_id, wire_deck_id, name, main, avatar, quests):
    """Upsert a deck from a client AddOnlineDeck(361) upload. Match an existing deck by (account_id,
    wire_deck_id) FIRST, then fall back to (account_id, name) -- the client re-uploads decks with a fresh
    wire id but the same name, so keying on wire id alone let near-duplicates accumulate (and UNIQUE(name)
    collisions burned autoincrement ids -> the deck-id gaps). main = [(catalog_id, qty), ...];
    quests = [catalog_id, ...]; avatar = catalog_id. Replaces cards."""
    row = conn.execute("SELECT id FROM decks WHERE account_id=? AND wire_deck_id=?",
                       (account_id, wire_deck_id)).fetchone()
    if row is None:                                   # no wire-id match -> try by name (same deck, new id)
        row = conn.execute("SELECT id FROM decks WHERE account_id=? AND name=?",
                           (account_id, name)).fetchone()
    if row is None:
        return create_deck(conn, account_id, name, main, avatar, quests, wire_deck_id=wire_deck_id)
    deck_id = row["id"]
    try:                                             # refresh wire id + name + avatar (name may collide -> fallback)
        conn.execute("UPDATE decks SET name=?, wire_deck_id=?, avatar_catalog_id=?, updated_at=datetime('now') WHERE id=?",
                     (name, wire_deck_id, avatar, deck_id))
    except sqlite3.IntegrityError:   # renamed to a sibling deck's name/wire -> keep existing name/wire
        conn.execute("UPDATE decks SET avatar_catalog_id=?, updated_at=datetime('now') WHERE id=?",
                     (avatar, deck_id))
    conn.execute("DELETE FROM deck_cards WHERE deck_id=?", (deck_id,))
    for cid, qty in main:
        conn.execute("INSERT INTO deck_cards(deck_id, catalog_id, qty, slot) VALUES (?,?,?, 'main')",
                     (deck_id, cid, qty))
    for cid in quests:
        conn.execute("INSERT OR IGNORE INTO deck_cards(deck_id, catalog_id, qty, slot) VALUES (?,?,1, 'quest')",
                     (deck_id, cid))
    conn.commit()
    return deck_id


def set_last_deck(conn, account_id, deck_id):
    conn.execute("UPDATE accounts SET last_deck_id = ? WHERE id = ?", (deck_id, account_id))
    conn.commit()


def seed_starter(conn, account_id, deck_id=None):
    """First-login seeding: grant an official starter deck's cards to the account's collection
    and create the matching deck. Card data from standalone_cards (standalone catalog)."""
    import standalone_cards
    if deck_id is None:
        deck_id = config.DEFAULT_STARTER_DECK
    sd = standalone_cards.load_starter_deck(deck_id)
    for cid, qty in sd["cards"]:
        if cid:
            conn.execute(
                "INSERT INTO collections(account_id, catalog_id, qty) VALUES (?,?,?) "
                "ON CONFLICT(account_id, catalog_id) DO UPDATE SET qty = qty + excluded.qty",
                (account_id, cid, qty))
    dname = ("%s Starter" % STARTER_NAMES[deck_id]) if deck_id in STARTER_NAMES else ("Starter %d" % deck_id)
    return create_deck(conn, account_id, dname,
                       sd["main"], sd["avatar"], sd["quests"], is_starter=1)


# ==========================================================================
# single-player standalone account (one fixed account + auto-login session)
# ==========================================================================
# The offline launcher auto-logs in with a FIXED dummy session id; login resolves purely
# session -> account. So the whole standalone runs on ONE account, seeded with every starter
# deck. These helpers guarantee that account + a long-lived session bound to it exist.
STANDALONE_USERNAME   = "StandAloneUser"
STANDALONE_SESSION_ID = "deadbeefdeadbeef"     # == launcher.ps1 --sessionID (what the client sends)

def ensure_standalone_account(conn):
    """Idempotent: guarantee the single standalone account exists with all four starter decks,
    and refresh a long-lived fixed session bound to it so the launcher's auto-login always
    resolves (the 30-min TTL would otherwise expire between launches). Safe every server boot."""
    acct = account_by_username(conn, STANDALONE_USERNAME)
    aid = acct["id"] if acct else create_account(conn, STANDALONE_USERNAME, "standalone")
    # Seed all four starter decks if the account has none yet (fresh account, or a prior partial
    # seed). Best-effort: standalone_cards + its data live in the RE repo, not the shipped bundle,
    # so seeding is a no-op there (the decks are baked into the shipped db). Never raise -- a missing
    # module must not break server boot.
    if conn.execute("SELECT COUNT(*) FROM decks WHERE account_id=?", (aid,)).fetchone()[0] == 0:
        try:
            import standalone_cards
            for did in sorted(standalone_cards.STARTER_DECKS):
                try:
                    seed_starter(conn, aid, did)
                except Exception:
                    pass
        except Exception:
            pass
    if acct is None or conn.execute("SELECT last_deck_id FROM accounts WHERE id=?", (aid,)).fetchone()["last_deck_id"] is None:
        row = conn.execute("SELECT id FROM decks WHERE account_id=? ORDER BY id LIMIT 1", (aid,)).fetchone()
        if row:
            conn.execute("UPDATE accounts SET last_deck_id=? WHERE id=?", (row["id"], aid))
    # (re)issue the fixed login session, valid for years so relaunches never hit an expired token.
    conn.execute("DELETE FROM sessions WHERE session_id=?", (STANDALONE_SESSION_ID,))
    conn.execute(
        "INSERT INTO sessions(session_id, account_id, username, challenge, character_id, expires_at) "
        "VALUES (?,?,?,?,1, datetime('now','+3650 days'))",
        (STANDALONE_SESSION_ID, aid, STANDALONE_USERNAME, "cafebabecafebabe"))
    conn.commit()
    return aid


# --- optional SECOND player for local 1v1 PvP testing (server SWGTCG_ENABLE_P2) --------------------
# Mirrors StandAloneUser's collection + decks into a distinct account with its own fixed login session, so
# two client instances can log in as two different players against the local server. standalone_cards (the
# deck-seed data) is NOT shipped in this bundle -- the starter decks are baked into the shipped db -- so we
# CLONE the source account's collection/decks rather than re-seeding from scratch.
SECOND_USERNAME   = "Player2"
SECOND_SESSION_ID = "feedfacefeedface"     # == launcher-core.ps1 $GameArgsP2 --sessionID
SECOND_CHALLENGE  = "baddecafbaddecaf"     # == launcher-core.ps1 $GameArgsP2 --challenge

def ensure_second_player(conn, source_username=None):
    """Idempotent: guarantee a 2nd account (Player2) that MIRRORS the source account's collection + decks,
    with a long-lived fixed session bound to it (character_id=2). Returns its account id (None if no source).
    Safe every boot: clones only when the 2nd account has no decks yet; always refreshes the session."""
    src = account_by_username(conn, source_username or STANDALONE_USERNAME)
    if not src:
        return None
    src_id = src["id"]
    acct = account_by_username(conn, SECOND_USERNAME)
    aid = acct["id"] if acct else create_account(conn, SECOND_USERNAME, "player2")
    if conn.execute("SELECT COUNT(*) FROM decks WHERE account_id=?", (aid,)).fetchone()[0] == 0:
        conn.execute("INSERT OR IGNORE INTO collections(account_id, catalog_id, qty) "
                     "SELECT ?, catalog_id, qty FROM collections WHERE account_id=?", (aid, src_id))
        for d in conn.execute("SELECT id, name, avatar_catalog_id, is_starter FROM decks WHERE account_id=?",
                              (src_id,)).fetchall():
            cur = conn.execute(
                "INSERT INTO decks(account_id, name, wire_deck_id, avatar_catalog_id, is_starter) "
                "VALUES (?,?,?,?,?)",
                (aid, d["name"], "p2_deck_%d" % d["id"], d["avatar_catalog_id"], d["is_starter"]))
            new_did = cur.lastrowid
            conn.execute("INSERT INTO deck_cards(deck_id, catalog_id, qty, slot) "
                         "SELECT ?, catalog_id, qty, slot FROM deck_cards WHERE deck_id=?", (new_did, d["id"]))
        row = conn.execute("SELECT id FROM decks WHERE account_id=? ORDER BY id LIMIT 1", (aid,)).fetchone()
        if row:
            conn.execute("UPDATE accounts SET last_deck_id=? WHERE id=?", (row["id"], aid))
    conn.execute("DELETE FROM sessions WHERE session_id=?", (SECOND_SESSION_ID,))
    conn.execute(
        "INSERT INTO sessions(session_id, account_id, username, challenge, character_id, expires_at) "
        "VALUES (?,?,?,?,2, datetime('now','+3650 days'))",
        (SECOND_SESSION_ID, aid, SECOND_USERNAME, SECOND_CHALLENGE))
    conn.commit()
    return aid


def reset_to_standalone(conn):
    """Collapse to exactly one StandAloneUser with every starter deck. SAFE ORDER: build the new
    account FIRST, verify it actually got its starter decks, and only THEN delete the others -- so a
    failed/unavailable seed can never leave the db with zero usable accounts. accounts is the FK root,
    so the delete cascades collections/decks/deck_cards/entitlements/sessions/player_stats/campaign_progress."""
    aid = ensure_standalone_account(conn)
    ndecks = conn.execute("SELECT COUNT(*) FROM decks WHERE account_id=?", (aid,)).fetchone()[0]
    if ndecks == 0:
        raise RuntimeError("refusing reset: StandAloneUser has no starter decks "
                           "(standalone_cards + its data are unavailable here)")
    conn.execute("DELETE FROM accounts WHERE id <> ?", (aid,))
    conn.commit()
    if aid != 1:
        _renumber_account(conn, aid, 1)   # match the historical single-player account id
        aid = 1
    return aid


def _renumber_account(conn, old_id, new_id):
    """Change the (now sole) account's id, updating every child FK. Done with foreign_keys OFF so the
    parent row can move before its children -- both are fixed in one transaction, so the db is consistent
    at commit. PRAGMA foreign_keys must be toggled outside a transaction, hence the explicit BEGIN/COMMIT."""
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        for table, col in (("accounts", "id"), ("collections", "account_id"), ("decks", "account_id"),
                           ("account_entitlements", "account_id"), ("sessions", "account_id"),
                           ("player_stats", "account_id"), ("campaign_progress", "account_id")):
            conn.execute("UPDATE %s SET %s=? WHERE %s=?" % (table, col, col), (new_id, old_id))
        conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='accounts'", (new_id,))
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


# ==========================================================================
# card_catalog (DERIVED cache; rebuildable from standalone_cards)
# ==========================================================================
def find_cards(conn, key, limit=25):
    """Look up card_catalog rows by exact catalog_id (if key is all digits) or by name (exact, then
    case-insensitive substring). Returns a list of sqlite Rows (may be empty)."""
    key = str(key).strip()
    if key.isdigit():
        r = conn.execute("SELECT * FROM card_catalog WHERE catalog_id=?", (int(key),)).fetchone()
        return [r] if r else []
    exact = conn.execute("SELECT * FROM card_catalog WHERE name=? COLLATE NOCASE ORDER BY catalog_id",
                         (key,)).fetchall()
    if exact:
        return exact
    return conn.execute("SELECT * FROM card_catalog WHERE name LIKE ? COLLATE NOCASE ORDER BY catalog_id LIMIT ?",
                        ("%" + key + "%", limit)).fetchall()


def card_name(conn, catalog_id):
    r = conn.execute("SELECT name FROM card_catalog WHERE catalog_id=?", (catalog_id,)).fetchone()
    return r["name"] if r and r["name"] else "card#%s" % catalog_id


# Booster sets/seasons. set_num is the card_catalog set number (== the collectorinfo prefix == SWG TCG release
# order). Mapping VERIFIED against the card data by signature cards: set3 = the bounty-hunter set (Bossk/Zuckuss/
# Dengar/4-LOM, "Hunter"x10) = Galactic Hunters; set4 ("Deception"x6) = Agents of Deception; set5 (Xizor,
# "Syndicate"x7) = The Shadow Syndicate; set6 (Nightsister x9); set7 ("Conqueror"x8); set8 ("Victory"x8).
# Anchor: set_num 1 = Champions (collectorinfo "1..."). Matches the documented release dates (Aug08, Dec08, Jul09,
# Sep09, Oct09, Dec09, Mar10, Nov10).
SET_NAMES = {
    1: "Champions of the Force",
    2: "Squadrons Over Corellia",
    3: "Galactic Hunters",
    4: "Agents of Deception",
    5: "The Shadow Syndicate",
    6: "The Nightsister's Revenge",
    7: "Threat of the Conqueror",
    8: "The Price of Victory",
}

# The four set-1 (Champions of the Force) starter decks shipped by the platform, keyed by deck id (their
# avatars: Jeffren Brek / Rachi Sitra / Coret Bhan / Namman Cha). See standalone_cards.STARTER_DECKS.
STARTER_NAMES = {111: "Imperial", 112: "Jedi", 113: "Rebel", 114: "Sith"}


def set_label(set_num):
    if set_num is None:
        return "all sets"
    nm = SET_NAMES.get(set_num)
    return ("Set %d: %s" % (set_num, nm)) if nm else ("Set %d" % set_num)


def list_sets(conn):
    """Booster sets that have collectible cards -> [{set_num, name, label, cards}], ordered by set_num."""
    rows = conn.execute(
        "SELECT set_num, COUNT(*) AS cards FROM card_catalog "
        "WHERE is_card=1 AND type IS NOT NULL AND set_num IS NOT NULL AND set_num > 0 "
        "GROUP BY set_num ORDER BY set_num").fetchall()
    return [{"set_num": r["set_num"], "name": SET_NAMES.get(r["set_num"]) or ("Set %d" % r["set_num"]),
             "label": set_label(r["set_num"]), "cards": r["cards"]} for r in rows]


def list_starters(conn):
    """The selectable starter decks -> [{deck_id, faction, avatar, avatar_name, label}], ordered by deck id.
    Faction names + avatars from STARTER_NAMES / standalone_cards.STARTER_DECKS (all Champions of the Force)."""
    import standalone_cards
    out = []
    for did in sorted(standalone_cards.STARTER_DECKS):
        try:
            av = standalone_cards.load_starter_deck(did).get("avatar")
        except Exception:
            av = None
        fac = STARTER_NAMES.get(did, "Starter %d" % did)
        avn = card_name(conn, av) if av else "?"
        out.append({"deck_id": did, "faction": fac, "avatar": av, "avatar_name": avn,
                    "label": "%s (%s)" % (fac, avn)})
    return out


def grant_pack(conn, account_id, n=15, set_num=None, seed=None):
    """Grant a 'booster' of n random COLLECTIBLE cards (card_catalog.is_card=1) to the account's collection,
    drawn from booster set `set_num` (None = all sets). Returns [(catalog_id, name), ...] granted (empty if the
    set has no collectible cards). Deterministic if seed given (with replacement -- a booster can dupe)."""
    import random
    # Mythic ('M') cards are heroic-instance/AI-scenario cards -- never player-awardable.
    q = "SELECT catalog_id, name FROM card_catalog WHERE is_card=1 AND type IS NOT NULL AND COALESCE(rarity,'') <> 'M'"
    args = []
    if set_num is not None:
        q += " AND set_num=?"; args.append(set_num)
    pool = conn.execute(q, args).fetchall()
    if not pool:
        return []
    rng = random.Random(seed)
    picks = [rng.choice(pool) for _ in range(n)]
    for c in picks:
        add_to_collection(conn, account_id, c["catalog_id"], 1)
    return [(c["catalog_id"], c["name"]) for c in picks]


def rebuild_card_catalog(conn):
    """Drop + repopulate card_catalog from standalone_cards.load_cards(). Returns the row count."""
    import standalone_cards
    cards = standalone_cards.load_cards()
    conn.execute("DELETE FROM card_catalog")
    rows = []
    for c in cards.values():
        cid = standalone_cards.catalog_id(c)
        if cid is None:
            continue
        rows.append((cid, c.title, c.type, getattr(c, "rarity", None),
                     getattr(c, "set_num", None), getattr(c, "collector_num", None),
                     c.collectorinfo, c.cost, c.attack, c.defense, c.health_or_level,
                     1 if c.type else 0))
    conn.executemany(
        "INSERT OR REPLACE INTO card_catalog(catalog_id, name, type, rarity, set_num, collector_num, "
        "collectorinfo, cost, attack, defense, health_or_level, is_card) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    return len(rows)


# ==========================================================================
# settings (MOTD + toggles)
# ==========================================================================
def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


def get_motd(conn):
    """Message-of-the-day text shown on the home screen (empty = no MOTD push)."""
    return get_setting(conn, "motd", "") or ""


def set_motd(conn, text):
    set_setting(conn, "motd", text or "")


# ==========================================================================
# news / announcements  (NetworkCommand_News 457: headline[]/body[]/id[])
# ==========================================================================
def add_news(conn, headline, body="", sort=0, active=1):
    cur = conn.execute("INSERT INTO news(headline, body, sort, active) VALUES (?,?,?,?)",
                       (headline, body or "", int(sort), 1 if active else 0))
    conn.commit()
    return cur.lastrowid


def list_news(conn, active_only=False):
    q = "SELECT id, headline, body, active, sort, created_at FROM news"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY sort ASC, id ASC"
    return [dict(r) for r in conn.execute(q).fetchall()]


def active_news(conn, limit=None):
    """News for the wire push -> [(id, headline, body), ...] in display order."""
    q = "SELECT id, headline, body FROM news WHERE active=1 ORDER BY sort ASC, id ASC"
    if limit:
        q += " LIMIT %d" % int(limit)
    return [(r["id"], r["headline"], r["body"]) for r in conn.execute(q).fetchall()]


def set_news_active(conn, news_id, active):
    conn.execute("UPDATE news SET active=? WHERE id=?", (1 if active else 0, news_id))
    conn.commit()


def delete_news(conn, news_id):
    conn.execute("DELETE FROM news WHERE id=?", (news_id,))
    conn.commit()


# ==========================================================================
# player stats + leaderboard  (NetworkCommand_LeaderBoardData 458)
# ==========================================================================
_WIN_DELTA, _LOSS_DELTA = 25, 20   # simple symmetric-ish rating; swap for Elo later


def _ensure_stats(conn, account_id):
    conn.execute("INSERT OR IGNORE INTO player_stats(account_id) VALUES (?)", (account_id,))


def get_stats(conn, account_id):
    _ensure_stats(conn, account_id)
    return dict(conn.execute("SELECT * FROM player_stats WHERE account_id=?", (account_id,)).fetchone())


def record_result(conn, account_id, outcome):
    """outcome: 'win' | 'loss' | 'draw'. Updates wins/losses/rating/streak. Returns the new stats row."""
    _ensure_stats(conn, account_id)
    if outcome == "win":
        conn.execute("UPDATE player_stats SET wins=wins+1, rating=rating+?, "
                     "streak=CASE WHEN streak<0 THEN 1 ELSE streak+1 END, "
                     "updated_at=datetime('now') WHERE account_id=?", (_WIN_DELTA, account_id))
    elif outcome == "loss":
        conn.execute("UPDATE player_stats SET losses=losses+1, rating=MAX(0, rating-?), "
                     "streak=CASE WHEN streak>0 THEN -1 ELSE streak-1 END, "
                     "updated_at=datetime('now') WHERE account_id=?", (_LOSS_DELTA, account_id))
    elif outcome == "draw":
        conn.execute("UPDATE player_stats SET draws=draws+1, streak=0, "
                     "updated_at=datetime('now') WHERE account_id=?", (account_id,))
    else:
        raise ValueError("outcome must be win|loss|draw, got %r" % outcome)
    conn.commit()
    return get_stats(conn, account_id)


def record_match(conn, winner_id, loser_id):
    """Convenience: apply a decisive result to both players."""
    record_result(conn, winner_id, "win")
    record_result(conn, loser_id, "loss")


def leaderboard_top(conn, limit=20):
    """Ranked standings -> [{rank, account_id, name, wins, losses, draws, rating, games}], best rating first.
    Only players who have played at least one game appear."""
    rows = conn.execute(
        "SELECT s.account_id, COALESCE(a.display_name, a.username) AS name, "
        "       s.wins, s.losses, s.draws, s.rating "
        "FROM player_stats s JOIN accounts a ON a.id=s.account_id "
        "WHERE (s.wins + s.losses + s.draws) > 0 "
        "ORDER BY s.rating DESC, s.wins DESC, s.losses ASC, a.username ASC "
        "LIMIT ?", (limit,)).fetchall()
    out = []
    for i, r in enumerate(rows, 1):
        out.append({"rank": i, "account_id": r["account_id"], "name": r["name"],
                    "wins": r["wins"], "losses": r["losses"], "draws": r["draws"],
                    "rating": r["rating"], "games": r["wins"] + r["losses"] + r["draws"]})
    return out


# ==========================================================================
# events (seasonal / special)
# ==========================================================================
def create_event(conn, name, description="", kind="seasonal", format=None,
                 starts_at=None, ends_at=None, active=1):
    cur = conn.execute(
        "INSERT INTO events(name, description, kind, format, starts_at, ends_at, active) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, description or "", kind, format, starts_at, ends_at, 1 if active else 0))
    conn.commit()
    return cur.lastrowid


def list_events(conn, active_only=False):
    q = "SELECT * FROM events"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY COALESCE(starts_at, created_at) DESC, id DESC"
    return [dict(r) for r in conn.execute(q).fetchall()]


def get_event(conn, event_id):
    r = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return dict(r) if r else None


def set_event_active(conn, event_id, active):
    conn.execute("UPDATE events SET active=? WHERE id=?", (1 if active else 0, event_id))
    conn.commit()


def delete_event(conn, event_id):
    conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    conn.commit()


# ==========================================================================
# tournaments
# ==========================================================================
_TOURNEY_STATES = ("open", "locked", "running", "complete", "cancelled")


def create_tournament(conn, name, description="", format="standard", event_id=None,
                      max_players=8, starts_at=None):
    cur = conn.execute(
        "INSERT INTO tournaments(name, description, format, event_id, max_players, starts_at) "
        "VALUES (?,?,?,?,?,?)",
        (name, description or "", format, event_id, int(max_players), starts_at))
    conn.commit()
    return cur.lastrowid


def list_tournaments(conn, states=None, event_id=None):
    q = "SELECT t.*, (SELECT COUNT(*) FROM tournament_entries e WHERE e.tournament_id=t.id) AS players " \
        "FROM tournaments t"
    conds, args = [], []
    if states:
        conds.append("t.state IN (%s)" % ",".join("?" * len(states))); args += list(states)
    if event_id is not None:
        conds.append("t.event_id=?"); args.append(event_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY t.id DESC"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_tournament(conn, tid):
    r = conn.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM tournament_entries e WHERE e.tournament_id=t.id) AS players "
        "FROM tournaments t WHERE t.id=?", (tid,)).fetchone()
    return dict(r) if r else None


def set_tournament_state(conn, tid, state):
    if state not in _TOURNEY_STATES:
        raise ValueError("bad tournament state %r" % state)
    conn.execute("UPDATE tournaments SET state=? WHERE id=?", (state, tid))
    conn.commit()


def set_tournament_round(conn, tid, round_no):
    conn.execute("UPDATE tournaments SET round=? WHERE id=?", (int(round_no), tid))
    conn.commit()


def delete_tournament(conn, tid):
    conn.execute("DELETE FROM tournaments WHERE id=?", (tid,))
    conn.commit()


def deck_format_check(conn, deck_id, fmt):
    """Validate a deck against a tournament format restriction. Returns (ok: bool, reason: str).
    Formats: None/''/'standard'/'sealed' -> unrestricted (ok). 'set:N' -> every MAIN-deck card must belong
    to booster set N. 'sets:a,b,c' -> from any of those sets. Avatar + quest cards are EXEMPT (the restriction
    is on the 50-card main deck, where set-legality is meaningful). Unknown format strings do not block."""
    if not fmt or fmt in ("standard", "sealed"):
        return True, ""
    allowed = None
    try:
        if fmt.startswith("set:"):
            allowed = {int(fmt.split(":", 1)[1])}
        elif fmt.startswith("sets:"):
            allowed = {int(x) for x in fmt.split(":", 1)[1].split(",") if x.strip()}
    except ValueError:
        return True, ""                       # malformed restriction -> don't block
    if not allowed:
        return True, ""
    if deck_id is None:
        return False, "format %s requires a deck" % fmt
    rows = conn.execute(
        "SELECT dc.catalog_id, c.name, c.set_num FROM deck_cards dc "
        "LEFT JOIN card_catalog c ON c.catalog_id=dc.catalog_id "
        "WHERE dc.deck_id=? AND dc.slot='main'", (deck_id,)).fetchall()
    if not rows:
        return False, "deck %s has no main cards (or does not exist)" % deck_id
    illegal = [r for r in rows if r["set_num"] not in allowed]
    if illegal:
        names = ", ".join((r["name"] or str(r["catalog_id"])) for r in illegal[:5])
        more = "" if len(illegal) <= 5 else " (+%d more)" % (len(illegal) - 5)
        return False, "%d card(s) outside %s: %s%s" % (len(illegal), fmt, names, more)
    return True, ""


def join_tournament(conn, tid, account_id, deck_id=None, enforce_format=True):
    """Register an account (idempotent -- updates the chosen deck). Raises if the tournament is
    not open, is full, or (when a deck_id is given + enforce_format) the deck breaks the format restriction.
    Returns the entry count after joining."""
    t = get_tournament(conn, tid)
    if not t:
        raise ValueError("no such tournament %r" % tid)
    if deck_id is not None and enforce_format:
        ok, reason = deck_format_check(conn, deck_id, t["format"])
        if not ok:
            raise ValueError("deck rejected: %s" % reason)
    already = conn.execute("SELECT 1 FROM tournament_entries WHERE tournament_id=? AND account_id=?",
                           (tid, account_id)).fetchone()
    if not already:
        if t["state"] != "open":
            raise ValueError("tournament is %s, not open" % t["state"])
        if t["players"] >= t["max_players"]:
            raise ValueError("tournament is full (%d)" % t["max_players"])
    conn.execute(
        "INSERT INTO tournament_entries(tournament_id, account_id, deck_id) VALUES (?,?,?) "
        "ON CONFLICT(tournament_id, account_id) DO UPDATE SET deck_id=excluded.deck_id",
        (tid, account_id, deck_id))
    conn.commit()
    return conn.execute("SELECT COUNT(*) c FROM tournament_entries WHERE tournament_id=?", (tid,)).fetchone()["c"]


def leave_tournament(conn, tid, account_id):
    conn.execute("DELETE FROM tournament_entries WHERE tournament_id=? AND account_id=?", (tid, account_id))
    conn.commit()


def list_entries(conn, tid):
    rows = conn.execute(
        "SELECT e.account_id, COALESCE(a.display_name, a.username) AS name, e.deck_id, e.seed, "
        "       e.wins, e.losses, e.dropped, e.joined_at "
        "FROM tournament_entries e JOIN accounts a ON a.id=e.account_id "
        "WHERE e.tournament_id=? ORDER BY e.seed IS NULL, e.seed ASC, e.joined_at ASC", (tid,)).fetchall()
    return [dict(r) for r in rows]


def drop_player(conn, tid, account_id, dropped=1):
    """Mark a player dropped (still counted historically, but excluded from future pairings)."""
    conn.execute("UPDATE tournament_entries SET dropped=? WHERE tournament_id=? AND account_id=?",
                 (1 if dropped else 0, tid, account_id))
    conn.commit()


# ---- round runner (Swiss-style pairings + result reporting) ----
def tournament_standings(conn, tid):
    """Entries ranked for pairing/standings: most wins first, then fewest losses, then seed/join order.
    Returns [{account_id, name, wins, losses, dropped, seed}]."""
    rows = conn.execute(
        "SELECT e.account_id, COALESCE(a.display_name, a.username) AS name, e.wins, e.losses, "
        "       e.dropped, e.seed "
        "FROM tournament_entries e JOIN accounts a ON a.id=e.account_id "
        "WHERE e.tournament_id=? "
        "ORDER BY e.wins DESC, e.losses ASC, e.seed IS NULL, e.seed ASC, e.joined_at ASC", (tid,)).fetchall()
    return [dict(r) for r in rows]


def start_round(conn, tid):
    """Begin the next round: assign seeds on round 1, pair active players by current standings (adjacent
    pairing; an odd player out gets a bye = auto-win), persist the bracket, advance tournaments.round, and
    set state 'running'. Returns the list of match rows created. Raises if a prior round is unreported."""
    t = get_tournament(conn, tid)
    if not t:
        raise ValueError("no such tournament %r" % tid)
    if t["state"] in ("complete", "cancelled"):
        raise ValueError("tournament is %s" % t["state"])
    cur = t["round"]
    if cur > 0 and not round_complete(conn, tid, cur):
        raise ValueError("round %d has unreported matches" % cur)
    nxt = cur + 1

    # round 1: fix seeds (in join order) so pairings are deterministic; later rounds use standings.
    if nxt == 1:
        entrants = conn.execute(
            "SELECT account_id FROM tournament_entries WHERE tournament_id=? AND dropped=0 "
            "ORDER BY seed IS NULL, seed ASC, joined_at ASC", (tid,)).fetchall()
        order = [r["account_id"] for r in entrants]
        for s, acct in enumerate(order, 1):
            conn.execute("UPDATE tournament_entries SET seed=? WHERE tournament_id=? AND account_id=? AND seed IS NULL",
                         (s, tid, acct))
    else:
        order = [s["account_id"] for s in tournament_standings(conn, tid) if not s["dropped"]]

    if len(order) < 2:
        raise ValueError("need at least 2 active players to start a round (have %d)" % len(order))

    # adjacent pairing; trailing odd player gets a bye
    pairs, table = [], 1
    i = 0
    while i + 1 < len(order):
        pairs.append((order[i], order[i + 1])); i += 2
    bye = order[i] if i < len(order) else None

    created = []
    for a, b in pairs:
        conn.execute("INSERT INTO tournament_matches(tournament_id, round, table_no, player_a, player_b) "
                     "VALUES (?,?,?,?,?)", (tid, nxt, table, a, b))
        created.append({"table_no": table, "player_a": a, "player_b": b, "winner": None})
        table += 1
    if bye is not None:
        # a bye is an auto-win: reported immediately, counts as a tournament win, but NOT a global rating game.
        conn.execute("INSERT INTO tournament_matches(tournament_id, round, table_no, player_a, player_b, "
                     "winner, reported) VALUES (?,?,?,?,?,?,1)", (tid, nxt, table, bye, None, bye))
        conn.execute("UPDATE tournament_entries SET wins=wins+1 WHERE tournament_id=? AND account_id=?", (tid, bye))
        created.append({"table_no": table, "player_a": bye, "player_b": None, "winner": bye, "bye": True})

    conn.execute("UPDATE tournaments SET round=?, state='running' WHERE id=?", (nxt, tid))
    conn.commit()
    return created


def list_matches(conn, tid, round=None):
    q = ("SELECT m.*, COALESCE(aa.display_name, aa.username) AS name_a, "
         "COALESCE(ab.display_name, ab.username) AS name_b "
         "FROM tournament_matches m "
         "LEFT JOIN accounts aa ON aa.id=m.player_a LEFT JOIN accounts ab ON ab.id=m.player_b "
         "WHERE m.tournament_id=?")
    args = [tid]
    if round is not None:
        q += " AND m.round=?"; args.append(round)
    q += " ORDER BY m.round ASC, m.table_no ASC"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def round_complete(conn, tid, round):
    row = conn.execute("SELECT COUNT(*) AS n, SUM(reported) AS done FROM tournament_matches "
                       "WHERE tournament_id=? AND round=?", (tid, round)).fetchone()
    return row["n"] > 0 and (row["done"] or 0) == row["n"]


def report_match(conn, tid, table_no, winner_account, round=None, draw=False):
    """Report a result for a table in the given round (default = current round). Updates the match row,
    the two entries' W/L, and feeds record_match into the GLOBAL player_stats/leaderboard (byes excluded).
    winner_account must be one of the pairing's players. draw=True records a draw (no W/L, no rating change)."""
    if round is None:
        round = get_tournament(conn, tid)["round"]
    m = conn.execute("SELECT * FROM tournament_matches WHERE tournament_id=? AND round=? AND table_no=?",
                     (tid, round, table_no)).fetchone()
    if not m:
        raise ValueError("no table %d in round %d" % (table_no, round))
    if m["reported"]:
        raise ValueError("table %d round %d already reported" % (table_no, round))
    a, b = m["player_a"], m["player_b"]
    if b is None:
        raise ValueError("table %d is a bye (already resolved)" % table_no)
    if draw:
        conn.execute("UPDATE tournament_matches SET winner=0, reported=1 WHERE id=?", (m["id"],))
        record_result(conn, a, "draw"); record_result(conn, b, "draw")
        conn.commit()
        return {"table_no": table_no, "draw": True}
    if winner_account not in (a, b):
        raise ValueError("winner %r is not in the pairing (%r vs %r)" % (winner_account, a, b))
    loser = b if winner_account == a else a
    conn.execute("UPDATE tournament_matches SET winner=?, reported=1 WHERE id=?", (winner_account, m["id"]))
    conn.execute("UPDATE tournament_entries SET wins=wins+1 WHERE tournament_id=? AND account_id=?", (tid, winner_account))
    conn.execute("UPDATE tournament_entries SET losses=losses+1 WHERE tournament_id=? AND account_id=?", (tid, loser))
    conn.commit()
    record_match(conn, winner_account, loser)   # global rating/leaderboard
    return {"table_no": table_no, "winner": winner_account, "loser": loser}


# ==========================================================================
# selftest
# ==========================================================================
def _selftest():
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "swgtcg_selftest.db")
    if os.path.exists(path):
        os.remove(path)
    conn = connect(path)
    init_db(conn)

    # accounts + login
    aid = create_account(conn, "tester", "secret123")
    assert verify_login(conn, "tester", "secret123") == aid, "login should succeed"
    assert verify_login(conn, "tester", "wrong") is None, "bad password must fail"
    assert verify_login(conn, "nobody", "x") is None, "unknown user must fail"
    assert set(load_entitlements(conn, aid)) == set(config.DEFAULT_ENTITLEMENTS), "default entitlements"
    grant_entitlement(conn, aid, "Staff")
    assert "Staff" in load_entitlements(conn, aid), "grant Staff"
    set_status(conn, aid, "banned")
    assert verify_login(conn, "tester", "secret123") is None, "banned account refused"
    set_status(conn, aid, "active")

    # sessions
    sid = create_session(conn, aid, "tester")
    assert peek_session(conn, sid)["account_id"] == aid, "peek returns account"
    acct = bind_session(conn, sid)
    assert acct["id"] == aid, "bind returns account"
    assert bind_session(conn, "garbage") is None, "invalid session refused"

    # collection + starter deck
    seed_starter(conn, aid, 111)
    coll = load_collection(conn, aid)
    total = sum(q for _, q in coll)
    assert total == 55, "starter 111 seeds 55 cards, got %d" % total
    decks = load_decks(conn, aid)
    assert len(decks) == 1, "one starter deck, got %d" % len(decks)
    d = decks[0]
    assert d["avatar"] == 100007836, "deck avatar = Jeffren Brek, got %s" % d["avatar"]
    assert len(d["quests"]) == 4, "4 quests, got %d" % len(d["quests"])
    main_total = sum(q for _, q in d["main"])
    assert main_total == 50, "50 main cards, got %d" % main_total

    # second account is independent
    bid = create_account(conn, "tester2", "pw2")
    seed_starter(conn, bid, 113)
    assert load_decks(conn, bid)[0]["avatar"] != d["avatar"], "accounts have distinct starter avatars"
    assert load_collection(conn, aid) != load_collection(conn, bid), "per-account collections differ"

    # card_catalog cache
    import standalone_cards
    n = rebuild_card_catalog(conn)
    expect = len([c for c in standalone_cards.load_cards().values()
                  if standalone_cards.catalog_id(c) is not None])
    assert n == expect, "catalog cache count %d != %d" % (n, expect)
    rebel = conn.execute("SELECT name, type, cost FROM card_catalog WHERE catalog_id = 100007408").fetchone()
    assert rebel["name"] == "Rebel Sergeant", "catalog name lookup: %r" % (rebel and rebel["name"])

    # v2: settings / MOTD
    assert get_motd(conn) == "", "no MOTD by default"
    set_motd(conn, "Welcome to the server!")
    assert get_motd(conn) == "Welcome to the server!", "MOTD round-trips"

    # v2: news
    nid = add_news(conn, "Season 1 Live", "The first seasonal event has begun.")
    add_news(conn, "Hidden", active=0)
    nl = active_news(conn)
    assert len(nl) == 1 and nl[0][0] == nid and nl[0][1] == "Season 1 Live", "one active news item: %r" % nl
    assert len(list_news(conn)) == 2, "two news rows total"

    # v2: player stats + leaderboard
    record_match(conn, aid, bid)          # aid beats bid
    record_result(conn, aid, "win")       # aid wins again
    sa, sb = get_stats(conn, aid), get_stats(conn, bid)
    assert sa["wins"] == 2 and sa["losses"] == 0 and sa["streak"] == 2, "winner stats %r" % sa
    assert sb["wins"] == 0 and sb["losses"] == 1 and sb["streak"] == -1, "loser stats %r" % sb
    lb = leaderboard_top(conn)
    assert lb[0]["account_id"] == aid and lb[0]["rank"] == 1, "leader is the winner: %r" % lb
    assert lb[1]["account_id"] == bid, "loser ranked below"

    # v2: events + tournaments
    ev = create_event(conn, "Summer Championship", kind="tournament", format="set:3")
    assert len(list_events(conn, active_only=True)) == 1, "one active event"
    tid = create_tournament(conn, "Summer Cup R1", format="set:3", event_id=ev, max_players=2)
    assert join_tournament(conn, tid, aid) == 1, "first entrant"
    assert join_tournament(conn, tid, bid) == 2, "second entrant"
    try:
        cid = create_account(conn, "tester3", "pw3")
        join_tournament(conn, tid, cid); raise AssertionError("should reject: tournament full")
    except ValueError:
        pass
    assert len(list_entries(conn, tid)) == 2, "two entries"
    set_tournament_state(conn, tid, "running")
    assert get_tournament(conn, tid)["state"] == "running", "state transition"

    # round runner: 2-player single round, result feeds standings + global leaderboard
    matches = start_round(conn, tid)
    assert len(matches) == 1 and get_tournament(conn, tid)["round"] == 1, matches
    aid_w0 = get_stats(conn, aid)["wins"]
    r = report_match(conn, tid, 1, aid)                 # aid beats bid at table 1
    assert r["winner"] == aid and r["loser"] == bid, r
    assert round_complete(conn, tid, 1), "round 1 complete after the only match"
    st = tournament_standings(conn, tid)
    assert st[0]["account_id"] == aid and st[0]["wins"] == 1, st
    assert get_stats(conn, aid)["wins"] == aid_w0 + 1, "report_match feeds global stats"

    # bye handling: 3 players -> 1 pairing + 1 bye (auto-win, no global game)
    cid2 = create_account(conn, "tester4", "pw4")
    tid2 = create_tournament(conn, "Bye Cup", max_players=4)
    for x in (aid, bid, cid2):
        join_tournament(conn, tid2, x)
    cid2_global_w0 = get_stats(conn, cid2)["wins"]             # brand-new player -> 0 global wins
    ms = start_round(conn, tid2)
    byes = [m for m in ms if m.get("bye")]
    assert len(ms) == 2 and len(byes) == 1, ms                 # one real match + one bye
    # seed=join order (aid,bid,cid2) -> pair (aid,bid), cid2 gets the bye
    assert byes[0]["player_a"] == cid2, "trailing seed gets the bye: %r" % byes
    # a bye = a TOURNAMENT win (entries) but NOT a global rating game
    assert any(e["account_id"] == cid2 and e["wins"] == 1 for e in list_entries(conn, tid2)), "bye = tourney win"
    assert get_stats(conn, cid2)["wins"] == cid2_global_w0, "bye must not change global stats"

    conn.close()
    print("db selftest OK")
    print("  account+login+ban gate, entitlements (+Staff grant), session create/peek/bind")
    print("  starter 111: %d collection copies, deck avatar %s, 4 quests, %d main" % (total, d["avatar"], main_total))
    print("  two independent accounts (111 vs 113) -> distinct collections+decks")
    print("  card_catalog cache: %d rows; sample 100007408 -> %s" % (n, rebel["name"]))
    print("  v2: MOTD + news(%d active), leaderboard top=%s, event+tournament(2/2 full)"
          % (len(nl), lb[0]["name"]))


if __name__ == "__main__":
    _selftest()
