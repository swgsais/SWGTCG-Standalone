/* Account-database layer for the Collection & Deck Manager.
 *
 * The logged-in client reads the player's owned cards + decks from the bundled
 * account database  _ext\server\swgtcg.db  (served on login). This module opens
 * that SQLite file in the browser via sql.js (WASM, bundled) and edits it, so
 * changes here show up in the game's Skirmish deck/collection on the next launch.
 *
 * Schema (relevant tables):
 *   accounts(id, username, display_name, ...)
 *   collections(account_id, catalog_id, qty)                 PK(account_id,catalog_id)
 *   decks(id, account_id, name, wire_deck_id, avatar_catalog_id, is_starter, ...)
 *   deck_cards(deck_id, catalog_id, qty, slot)  slot in {main,quest}  PK(deck_id,catalog_id,slot)
 */
(function (global) {
  'use strict';
  var SQLMod = null, db = null;

  function b64ToBytes(b64) {
    var bin = atob(b64), n = bin.length, a = new Uint8Array(n);
    for (var i = 0; i < n; i++) a[i] = bin.charCodeAt(i);
    return a;
  }
  async function ensure() {
    if (SQLMod) return SQLMod;
    if (!global.SQL_WASM_B64) throw new Error('sql-wasm-b64.js not loaded');
    if (!global.initSqlJs) throw new Error('sql-wasm.js not loaded');
    SQLMod = await global.initSqlJs({ wasmBinary: b64ToBytes(global.SQL_WASM_B64) });
    return SQLMod;
  }

  async function open(bytes) {
    await ensure();
    if (db) { try { db.close(); } catch (e) {} }
    db = new SQLMod.Database(new Uint8Array(bytes));
    // sanity: must have the expected tables
    var t = all("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('accounts','collections','decks','deck_cards')");
    if (t.length < 4) { db.close(); db = null; throw new Error("this file is not a SWGTCG account database (missing tables)"); }
    return true;
  }
  function isOpen() { return !!db; }

  function all(sql, params) {
    var out = [], st = db.prepare(sql);
    if (params) st.bind(params);
    while (st.step()) out.push(st.getAsObject());
    st.free();
    return out;
  }
  function run(sql, params) { db.run(sql, params || []); }
  function scalar(sql, params) {
    var r = all(sql, params);
    return r.length ? r[0][Object.keys(r[0])[0]] : null;
  }

  function accounts() { return all('SELECT id, username, display_name FROM accounts ORDER BY id'); }

  function collection(acct) {
    var m = {};
    all('SELECT catalog_id, qty FROM collections WHERE account_id=?', [acct]).forEach(function (r) { m[r.catalog_id] = r.qty; });
    return m;
  }
  function setOwned(acct, catId, qty) {
    qty = Math.max(0, qty | 0);
    if (qty > 0) run('INSERT INTO collections(account_id,catalog_id,qty) VALUES(?,?,?) ' +
                     'ON CONFLICT(account_id,catalog_id) DO UPDATE SET qty=excluded.qty', [acct, catId, qty]);
    else run('DELETE FROM collections WHERE account_id=? AND catalog_id=?', [acct, catId]);
  }
  function grantAll(acct, catIds, qty) {
    db.run('BEGIN');
    try {
      var st = db.prepare('INSERT INTO collections(account_id,catalog_id,qty) VALUES(?,?,?) ' +
                          'ON CONFLICT(account_id,catalog_id) DO UPDATE SET qty=excluded.qty');
      catIds.forEach(function (id) { st.run([acct, id, qty]); });
      st.free();
      db.run('COMMIT');
    } catch (e) { db.run('ROLLBACK'); throw e; }
  }
  function clearCollection(acct) { run('DELETE FROM collections WHERE account_id=?', [acct]); }
  function collectionTotal(acct) { return scalar('SELECT COALESCE(SUM(qty),0) FROM collections WHERE account_id=?', [acct]); }

  function decks(acct) { return all('SELECT id, name, avatar_catalog_id, is_starter FROM decks WHERE account_id=? ORDER BY name', [acct]); }
  function deck(id) {
    var d = all('SELECT id, account_id, name, avatar_catalog_id FROM decks WHERE id=?', [id])[0];
    if (!d) return null;
    var main = [], quests = [];
    all('SELECT catalog_id, qty, slot FROM deck_cards WHERE deck_id=? ORDER BY slot, catalog_id', [id]).forEach(function (r) {
      (r.slot === 'quest' ? quests : main).push({ id: r.catalog_id, qty: r.qty });
    });
    return { id: d.id, accountId: d.account_id, name: d.name, avatar: d.avatar_catalog_id, main: main, quests: quests };
  }
  /* dk: {id?, name, avatar, main:[{id,qty}], quests:[{id,qty}]}. Returns the deck id. */
  function saveDeck(acct, dk) {
    db.run('BEGIN');
    try {
      var id = dk.id;
      if (id) {
        run("UPDATE decks SET name=?, avatar_catalog_id=?, updated_at=datetime('now') WHERE id=?", [dk.name, dk.avatar, id]);
      } else {
        var wire = 'deck_mgr_' + (global.Date && Date.now ? Date.now() : Math.floor(Math.random() * 1e9));
        run('INSERT INTO decks(account_id,name,wire_deck_id,avatar_catalog_id,is_starter) VALUES(?,?,?,?,0)', [acct, dk.name, wire, dk.avatar]);
        id = scalar('SELECT last_insert_rowid()');
      }
      run('DELETE FROM deck_cards WHERE deck_id=?', [id]);
      var st = db.prepare('INSERT INTO deck_cards(deck_id,catalog_id,qty,slot) VALUES(?,?,?,?)');
      (dk.main || []).forEach(function (r) { if (r.qty > 0) st.run([id, r.id, r.qty, 'main']); });
      (dk.quests || []).forEach(function (r) { if (r.qty > 0) st.run([id, r.id, r.qty, 'quest']); });
      st.free();
      db.run('COMMIT');
      return id;
    } catch (e) { db.run('ROLLBACK'); throw e; }
  }
  function deleteDeck(id) { run('DELETE FROM decks WHERE id=?', [id]); }

  function exportBytes() { return db.export(); }

  /* ---- campaign / scenario progress ----
   * The server persists each scenario progress frame in campaign_progress and replays it
   * at login. Here we let the user view + reset it, and (experimentally) grant a scenario
   * by writing the raw progress frame the app builds. The table may be absent in an older
   * db (created by the server on first boot) -- guard reads, auto-create before a write. */
  function tableExists(name) {
    return all("SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name]).length > 0;
  }
  function ensureCampaignTable() {
    if (!tableExists('campaign_progress'))
      run("CREATE TABLE IF NOT EXISTS campaign_progress (" +
          "account_id INTEGER NOT NULL, cid INTEGER NOT NULL, item_key TEXT NOT NULL, " +
          "payload BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')), " +
          "PRIMARY KEY(account_id, cid, item_key))");
  }
  function campaignProgress(acct) {
    if (!tableExists('campaign_progress')) return [];
    return all("SELECT cid, item_key, updated_at FROM campaign_progress WHERE account_id=? ORDER BY cid, item_key", [acct]);
  }
  /* payload = Uint8Array (the raw command body). Latest-wins per (acct,cid,item_key). */
  function grantScenario(acct, cid, itemKey, payload) {
    ensureCampaignTable();
    run("INSERT INTO campaign_progress(account_id,cid,item_key,payload,updated_at) " +
        "VALUES(?,?,?,?, datetime('now')) " +
        "ON CONFLICT(account_id,cid,item_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
        [acct, cid, itemKey, payload]);
  }
  function resetCampaign(acct) { if (tableExists('campaign_progress')) run("DELETE FROM campaign_progress WHERE account_id=?", [acct]); }
  function resetAllCampaign() { if (tableExists('campaign_progress')) run("DELETE FROM campaign_progress"); }

  /* ---- structured scenario completion (the state the server turns into account property 0x1054) ---- */
  function ensureCompletionTable() {
    if (!tableExists('scenario_completion'))
      run("CREATE TABLE IF NOT EXISTS scenario_completion (" +
          "account_id INTEGER NOT NULL, node_id INTEGER NOT NULL, archetype_id INTEGER NOT NULL, " +
          "difficulty INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')), " +
          "PRIMARY KEY(account_id, node_id, archetype_id, difficulty))");
  }
  function scenarioCompletion(acct) {
    if (!tableExists('scenario_completion')) return [];
    return all("SELECT node_id, archetype_id, difficulty FROM scenario_completion WHERE account_id=? " +
               "ORDER BY node_id, archetype_id, difficulty", [acct]);
  }
  /* learned scenario-string -> nodeId map (populated server-side by playing a scenario once). */
  function scenarioNodemap() {
    if (!tableExists('scenario_nodemap')) return {};
    var m = {}; all("SELECT scenario_id, node_id FROM scenario_nodemap").forEach(function (r) { m[r.scenario_id] = r.node_id; });
    return m;
  }
  function grantCompletion(acct, nodeId, archId, difficulty) {
    ensureCompletionTable();
    run("INSERT OR REPLACE INTO scenario_completion(account_id,node_id,archetype_id,difficulty,updated_at) " +
        "VALUES(?,?,?,?, datetime('now'))", [acct, nodeId, archId, difficulty]);
  }
  function clearCompletion(acct) { if (tableExists('scenario_completion')) run("DELETE FROM scenario_completion WHERE account_id=?", [acct]); }

  /* ---- booster packs (read card_catalog, add pulls to the collection) ---- */
  function cardSets() {
    if (!tableExists('card_catalog')) return [];
    return all("SELECT set_num AS setNum, COUNT(*) AS n FROM card_catalog " +
               "WHERE is_card=1 AND type IS NOT NULL AND rarity IS NOT NULL AND set_num > 0 " +
               "GROUP BY set_num ORDER BY set_num");
  }
  function cardsInSet(setNum) {
    if (!tableExists('card_catalog')) return [];
    return all("SELECT catalog_id AS id, name, type, rarity, cost, attack, defense, " +
               "health_or_level AS hp FROM card_catalog " +
               "WHERE is_card=1 AND type IS NOT NULL AND rarity IS NOT NULL AND set_num = ?", [setNum | 0]);
  }
  function addOwned(acct, catId, qty) {
    run("INSERT INTO collections(account_id,catalog_id,qty) VALUES(?,?,?) " +
        "ON CONFLICT(account_id,catalog_id) DO UPDATE SET qty = qty + excluded.qty", [acct, catId, Math.max(1, qty | 0)]);
  }

  global.DB = {
    open: open, isOpen: isOpen, accounts: accounts,
    collection: collection, collectionTotal: collectionTotal, setOwned: setOwned, grantAll: grantAll, clearCollection: clearCollection,
    decks: decks, deck: deck, saveDeck: saveDeck, deleteDeck: deleteDeck,
    campaignProgress: campaignProgress, grantScenario: grantScenario, resetCampaign: resetCampaign, resetAllCampaign: resetAllCampaign,
    scenarioCompletion: scenarioCompletion, scenarioNodemap: scenarioNodemap, grantCompletion: grantCompletion, clearCompletion: clearCompletion,
    cardSets: cardSets, cardsInSet: cardsInSet, addOwned: addOwned,
    exportBytes: exportBytes
  };
})(window);
