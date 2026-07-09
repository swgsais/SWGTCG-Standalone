/* SWGTCG Collection & Deck Manager -- edits the account database (swgtcg.db)
 * the logged-in client reads. Uses sql.js (bundled) + the File System Access API,
 * with an Import/Export fallback for any browser. No server needed.
 */
(function () {
  'use strict';

  var CAT = window.CATALOG || { cards: [] };
  var CARDS = CAT.cards;
  var PLAYABLE_TYPES = { Avatar: 1, Unit: 1, Ability: 1, Item: 1, Tactic: 1, Quest: 1 };
  var MAIN_TYPES = { Unit: 1, Ability: 1, Item: 1, Tactic: 1 };
  var MAX_COPIES = 4;

  function isPlayable(c) { return c.id >= 100007000 && PLAYABLE_TYPES[c.type] && c.name && c.name !== c.type; }
  var PLAYABLE = CARDS.filter(isPlayable);
  var byId = {}; PLAYABLE.forEach(function (c) { byId[c.id] = c; });
  var AVATARS = PLAYABLE.filter(function (c) { return c.is_avatar; }).sort(function (a, b) { return a.name.localeCompare(b.name); });

  var state = {
    fh: null,               // FileSystemFileHandle for swgtcg.db (or null in import mode)
    acct: null,             // active account id
    colMap: {},             // catalog_id -> qty for the active account
    deck: null,             // working deck {id?, name, avatar, main:[{id,qty}], quests:[id...]}
    dirty: false,
    supported: !!window.showOpenFilePicker
  };

  function $(id) { return document.getElementById(id); }
  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (m) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]; }); }
  function log(msg, kind) {
    var box = $('log'); var line = el('div', 'logline' + (kind ? ' ' + kind : ''), msg);
    box.appendChild(line); box.scrollTop = box.scrollHeight;
    while (box.childNodes.length > 60) box.removeChild(box.firstChild);
  }
  function debounce(fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; }
  function download(name, bytes) {
    var blob = new Blob([bytes], { type: 'application/octet-stream' });
    var url = URL.createObjectURL(blob); var a = el('a'); a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }
  function markDirty() { state.dirty = true; $('connStatus').textContent = 'unsaved changes'; $('connStatus').classList.add('dirty'); }
  function markClean() { state.dirty = false; $('connStatus').textContent = (state.fh ? state.fh.name : 'imported DB') + ' (saved)'; $('connStatus').classList.remove('dirty'); }

  // ---- open / save the database ----
  async function openDb() {
    if (state.supported) {
      try {
        var picked = await window.showOpenFilePicker({
          types: [{ description: 'SWGTCG account database', accept: { 'application/x-sqlite3': ['.db', '.sqlite', '.sqlite3'] } }]
        });
        var fh = picked[0];
        var file = await fh.getFile();
        await DB.open(await file.arrayBuffer());
        state.fh = fh;
        afterOpen(fh.name);
        return;
      } catch (e) {
        if (e && e.name === 'AbortError') return;    // user cancelled
        log('Direct open failed (' + e.message + '); use Import instead.', 'warn');
        $('importDb').click();
      }
    } else {
      $('importDb').click();
    }
  }
  async function importDbFile(file) {
    try { await DB.open(await file.arrayBuffer()); state.fh = null; afterOpen(file.name + ' (import)'); }
    catch (e) { log('Open failed: ' + e.message, 'err'); }
  }
  function afterOpen(label) {
    var sel = $('acct'); sel.innerHTML = '';
    DB.accounts().forEach(function (a) {
      var o = el('option', null, a.username + '  (id ' + a.id + ')'); o.value = String(a.id); sel.appendChild(o);
    });
    sel.disabled = false; $('btnSave').disabled = false;
    state.acct = parseInt(sel.value, 10);
    log('Opened ' + label + '. Accounts: ' + DB.accounts().map(function (a) { return a.username; }).join(', '), 'ok');
    initBooster();
    selectAccount(state.acct);
    markClean();
    $('connStatus').classList.add('ok');
  }
  async function saveDb() {
    if (!DB.isOpen()) { log('Open a database first.', 'warn'); return; }
    var bytes = DB.exportBytes();
    if (state.fh) {
      try { var w = await state.fh.createWritable(); await w.write(bytes); await w.close();
            log('Saved ' + state.fh.name + ' (' + bytes.length + ' bytes). Relaunch the game to see changes.', 'ok'); markClean(); }
      catch (e) { log('Save failed: ' + e.message + ' -- downloading instead.', 'warn'); download('swgtcg.db', bytes); }
    } else {
      download('swgtcg.db', bytes);
      log('Downloaded swgtcg.db -- copy it over _ext\\server\\swgtcg.db.', 'ok'); markClean();
    }
  }

  function selectAccount(acct) {
    state.acct = acct;
    state.colMap = DB.collection(acct);
    refreshDeckList();
    state.deck = newDeck();
    renderCollection(); renderDeck(); renderScenarios();
    log('Account ' + acct + ': ' + DB.collectionTotal(acct) + ' cards, ' + DB.decks(acct).length + ' decks.', 'ok');
  }

  function newDeck() { return { id: null, name: 'New Deck', avatar: (AVATARS[0] ? AVATARS[0].id : 0), main: [], quests: [] }; }
  function ownedQty(id) { return state.colMap[id] || 0; }

  // ---- collection ----
  function renderCollection() {
    var meta = $('colMeta'), list = $('colList');
    if (!DB.isOpen()) { meta.textContent = 'No database open. Click "Open Account DB".'; list.innerHTML = ''; return; }
    meta.innerHTML = 'Account <b>' + esc(acctName()) + '</b> &nbsp; ' + DB.collectionTotal(state.acct) + ' cards owned';
    var q = ($('colSearch').value || '').toLowerCase(), type = $('colType').value, ownedOnly = $('colOwnedOnly').checked;
    var frag = document.createDocumentFragment(), shown = 0;
    for (var i = 0; i < PLAYABLE.length; i++) {
      var c = PLAYABLE[i];
      if (type && c.type !== type) continue;
      if (q && c.name.toLowerCase().indexOf(q) < 0) continue;
      var owned = ownedQty(c.id);
      if (ownedOnly && owned === 0) continue;
      frag.appendChild(colRow(c, owned));
      if (++shown >= 900) break;
    }
    list.innerHTML = ''; list.appendChild(frag);
    if (shown >= 900) list.appendChild(el('div', 'more', 'Showing first 900 -- refine the search.'));
  }
  function colRow(c, owned) {
    var row = el('div', 'row'); row.appendChild(typeBadge(c));
    var nm = el('span', 'name', c.name); if (c.is_avatar) nm.appendChild(el('span', 'tag', 'avatar')); row.appendChild(nm);
    var ctl = el('span', 'qtyctl');
    var minus = el('button', 'sm', '−'), val = el('input', 'qty'), plus = el('button', 'sm', '+');
    val.type = 'number'; val.min = '0'; val.value = owned;
    function set(v) { v = Math.max(0, v | 0); val.value = v; DB.setOwned(state.acct, c.id, v); state.colMap[c.id] = v; markDirty(); }
    minus.onclick = function () { set((parseInt(val.value, 10) || 0) - 1); };
    plus.onclick = function () { set((parseInt(val.value, 10) || 0) + 1); };
    val.onchange = function () { set(parseInt(val.value, 10) || 0); };
    ctl.appendChild(minus); ctl.appendChild(val); ctl.appendChild(plus); row.appendChild(ctl);
    return row;
  }
  function grantAll() {
    if (!DB.isOpen()) { log('Open a database first.', 'warn'); return; }
    DB.grantAll(state.acct, PLAYABLE.map(function (c) { return c.id; }), MAX_COPIES);
    state.colMap = DB.collection(state.acct); markDirty();
    log('Granted ' + MAX_COPIES + ' of every playable card to ' + acctName() + '.', 'ok'); renderCollection(); renderDeck();
  }
  function clearAll() {
    if (!DB.isOpen()) return;
    DB.clearCollection(state.acct); state.colMap = {}; markDirty();
    log('Cleared all owned cards for ' + acctName() + '.', 'ok'); renderCollection(); renderDeck();
  }
  function acctName() { var a = DB.accounts().filter(function (x) { return x.id === state.acct; })[0]; return a ? a.username : ('#' + state.acct); }

  // ---- deck builder ----
  function refreshDeckList() {
    var sel = $('deckFile'); sel.innerHTML = '';
    var ds = DB.isOpen() ? DB.decks(state.acct) : [];
    if (!ds.length) { var o = el('option', null, '(no decks)'); o.value = ''; sel.appendChild(o); }
    ds.forEach(function (d) { var o = el('option', null, d.name + (d.is_starter ? '  *' : '')); o.value = String(d.id); sel.appendChild(o); });
  }
  function deckMainCount() { var t = 0; state.deck.main.forEach(function (r) { t += r.qty; }); return t; }
  function deckMainRow(id) { for (var k = 0; k < state.deck.main.length; k++) if (state.deck.main[k].id === id) return state.deck.main[k]; return null; }
  function addToDeck(id) {
    var c = byId[id]; if (!c) return;
    if (c.type === 'Quest') { if (state.deck.quests.indexOf(id) < 0) state.deck.quests.push(id); }
    else { var r = deckMainRow(id); if (r) { if (r.qty < MAX_COPIES) r.qty++; } else state.deck.main.push({ id: id, qty: 1 }); }
    renderDeck();
  }
  function removeFromDeck(id, isQuest) {
    if (isQuest) state.deck.quests = state.deck.quests.filter(function (x) { return x !== id; });
    else { var r = deckMainRow(id); if (r) { r.qty--; if (r.qty <= 0) state.deck.main = state.deck.main.filter(function (x) { return x.id !== id; }); } }
    renderDeck();
  }
  function renderDeckPool() {
    var q = ($('deckSearch').value || '').toLowerCase(), filt = $('deckType').value, ownedOnly = $('deckOwnedOnly').checked;
    var list = $('deckPool'), frag = document.createDocumentFragment(), shown = 0;
    for (var i = 0; i < PLAYABLE.length; i++) {
      var c = PLAYABLE[i];
      if (c.is_avatar) continue;
      if (filt === 'main') { if (!MAIN_TYPES[c.type]) continue; } else if (c.type !== filt) continue;
      if (q && c.name.toLowerCase().indexOf(q) < 0) continue;
      if (ownedOnly && ownedQty(c.id) === 0) continue;
      frag.appendChild(poolRow(c)); if (++shown >= 900) break;
    }
    list.innerHTML = ''; list.appendChild(frag);
    if (shown >= 900) list.appendChild(el('div', 'more', 'Showing first 900 -- refine the search.'));
  }
  function poolRow(c) {
    var row = el('div', 'row'); row.appendChild(typeBadge(c));
    var nm = el('span', 'name', c.name); var own = ownedQty(c.id); if (own) nm.appendChild(el('span', 'tag own', 'x' + own)); row.appendChild(nm);
    var add = el('button', 'sm add', c.type === 'Quest' ? 'Quest +' : 'Add'); add.onclick = function () { addToDeck(c.id); }; row.appendChild(add);
    return row;
  }
  function renderDeck() {
    if (!state.deck) state.deck = newDeck();
    $('deckAvatar').value = String(state.deck.avatar);
    $('deckName').value = state.deck.name;
    var qWrap = $('deckQuests'); qWrap.innerHTML = '';
    state.deck.quests.forEach(function (id) {
      var c = byId[id] || { name: '#' + id, type: 'Quest' }; var row = el('div', 'drow');
      row.appendChild(typeBadge(c)); row.appendChild(el('span', 'name', c.name));
      var rm = el('button', 'sm', 'x'); rm.onclick = function () { removeFromDeck(id, true); }; row.appendChild(rm); qWrap.appendChild(row);
    });
    $('questCount').textContent = state.deck.quests.length;
    var mWrap = $('deckMain'); mWrap.innerHTML = '';
    state.deck.main.slice().sort(byTypeName).forEach(function (r) {
      var c = byId[r.id] || { name: '#' + r.id, type: '?' }; var row = el('div', 'drow');
      row.appendChild(typeBadge(c)); row.appendChild(el('span', 'name', c.name));
      var ctl = el('span', 'qtyctl');
      var minus = el('button', 'sm', '−'); minus.onclick = function () { removeFromDeck(r.id, false); };
      var qv = el('span', 'qn', 'x' + r.qty);
      var plus = el('button', 'sm', '+'); plus.onclick = function () { addToDeck(r.id); };
      ctl.appendChild(minus); ctl.appendChild(qv); ctl.appendChild(plus); row.appendChild(ctl); mWrap.appendChild(row);
    });
    $('mainCount').textContent = deckMainCount();
    renderDeckStats(); renderDeckPool();
  }
  function byTypeName(a, b) { var ca = byId[a.id] || {}, cb = byId[b.id] || {}; return ((ca.type || '') + (ca.name || '')).localeCompare((cb.type || '') + (cb.name || '')); }
  function renderDeckStats() {
    var av = null; for (var i = 0; i < AVATARS.length; i++) if (AVATARS[i].id === state.deck.avatar) { av = AVATARS[i]; break; }
    var main = deckMainCount(), quests = state.deck.quests.length;
    $('deckStats').innerHTML = 'Main <b>' + main + '</b> &middot; Quests <b>' + quests + '</b>' + (av ? ' &middot; Avatar <b>' + esc(av.name) + '</b>' : '');
    var warns = [];
    if (!av) warns.push('Pick an avatar.');
    if (main !== 50) warns.push('Main deck is ' + main + ' (Standard is 50).');
    if (quests > 4) warns.push(quests + ' quests (max 4).');
    state.deck.main.forEach(function (r) { if (r.qty > MAX_COPIES) warns.push((byId[r.id] ? byId[r.id].name : r.id) + ' has ' + r.qty + ' copies.'); });
    if (av && DB.isOpen() && ownedQty(av.id) === 0) warns.push('You do not own avatar ' + av.name + ' (grant it in Collection).');
    var box = $('deckValidate');
    if (warns.length) { box.className = 'validate warn'; box.innerHTML = '&#9888; ' + warns.map(esc).join('<br>&#9888; '); }
    else { box.className = 'validate ok'; box.textContent = 'Deck looks legal.'; }
  }
  function loadDeck(id) {
    if (!id) return; var d = DB.deck(parseInt(id, 10)); if (!d) { log('Deck not found.', 'err'); return; }
    state.deck = { id: d.id, name: d.name, avatar: d.avatar, main: d.main, quests: d.quests.map(function (q) { return q.id; }) };
    log('Loaded deck "' + d.name + '" (main ' + deckMainCount() + ', quests ' + state.deck.quests.length + ').', 'ok'); renderDeck();
  }
  function saveDeck() {
    if (!DB.isOpen()) { log('Open a database first.', 'warn'); return; }
    var d = state.deck; if (!d.name) d.name = 'My Deck';
    try {
      var qrows = d.quests.map(function (id) { return { id: id, qty: 1 }; });
      var id = DB.saveDeck(state.acct, { id: d.id, name: d.name, avatar: d.avatar, main: d.main, quests: qrows });
      d.id = id; markDirty(); refreshDeckList(); $('deckFile').value = String(id);
      log('Saved deck "' + d.name + '" (id ' + id + '). Click "Save to game" to persist.', 'ok');
    } catch (e) {
      log('Save deck failed: ' + (e.message.indexOf('UNIQUE') >= 0 ? 'a deck with that name already exists for this account.' : e.message), 'err');
    }
  }
  function deleteDeck() {
    if (!DB.isOpen() || !state.deck.id) { log('Load a saved deck to delete it.', 'warn'); return; }
    DB.deleteDeck(state.deck.id); markDirty(); log('Deleted deck.', 'ok'); state.deck = newDeck(); refreshDeckList(); renderDeck();
  }

  // ---- booster packs -----------------------------------------------------
  var SET_NAMES = {
    1: 'Champions of the Force', 2: 'Squadrons Over Corellia', 3: 'Galactic Hunters',
    4: 'Agents of Deception', 5: 'The Shadow Syndicate', 6: "The Nightsister's Revenge",
    7: 'Threat of the Conqueror', 8: 'The Price of Victory'
  };
  var RARITY = {
    C: { name: 'Common', color: '#9aa6b2' }, U: { name: 'Uncommon', color: '#3fb950' },
    R: { name: 'Rare', color: '#2f81f7' }, F: { name: 'Foil', color: '#a371f7' },
    M: { name: 'Mythic', color: '#ff8c39' }, P: { name: 'Promo', color: '#f4c430' },
    A: { name: 'Alt Art', color: '#39c5cf' }
  };
  var allById = {}; CARDS.forEach(function (c) { allById[c.id] = c; });
  var _artAvail = null;                                      // null=unknown, else true/false (probed once)
  function withArt(cb) {
    if (_artAvail !== null) { cb(_artAvail); return; }
    var im = new Image();
    im.onload = function () { _artAvail = true; var n = $('artNote'); if (n) n.style.display = 'none'; cb(true); };
    im.onerror = function () { _artAvail = false; var n = $('artNote'); if (n) n.style.display = 'block'; cb(false); };
    im.src = 'art/images/card/100007401.jpg';                // a known set-1 card face
  }
  function rnd(a) { return a[Math.floor(Math.random() * a.length)]; }
  function statChips(c) {                                    // cost/atk/def/hp chips, or null if none apply
    var chips = [];
    if (c.cost != null) chips.push(['cost', c.cost]);
    if (c.attack != null) chips.push(['atk', c.attack]);
    if (c.defense != null) chips.push(['def', c.defense]);
    if (c.hp != null) chips.push([c.type === 'Avatar' ? 'lvl' : 'hp', c.hp]);
    if (!chips.length) return null;
    var row = el('span', 'gstats');
    chips.forEach(function (kv) {
      var s = el('span', 'gstat s-' + kv[0]); s.appendChild(el('b', null, String(kv[1])));
      s.appendChild(document.createTextNode(kv[0])); row.appendChild(s);
    });
    return row;
  }

  function initBooster() {
    var sel = $('boosterSet'); if (!sel) return; sel.innerHTML = '';
    var sets = DB.isOpen() ? DB.cardSets() : [];
    if (!sets.length) { var o = el('option', null, '(open a database)'); o.value = ''; sel.appendChild(o); return; }
    sets.forEach(function (s) {
      var o = el('option', null, (SET_NAMES[s.setNum] || ('Set ' + s.setNum)) + '  (' + s.n + ' cards)');
      o.value = String(s.setNum); sel.appendChild(o);
    });
  }
  function openPack(setNum, count) {
    if (!DB.isOpen()) { log('Open a database first.', 'warn'); return; }
    setNum = setNum | 0; count = count || 1;
    var pool = DB.cardsInSet(setNum);
    if (!pool.length) { log('No cards found for that set.', 'warn'); return; }
    var byR = {}; pool.forEach(function (c) { (byR[c.rarity] = byR[c.rarity] || []).push(c); });
    function pull(r) { return (byR[r] && byR[r].length) ? rnd(byR[r]) : rnd(byR.C || byR.U || byR.R || pool); }
    var all = [];
    for (var p = 0; p < count; p++) {
      for (var i = 0; i < 7; i++) all.push(pull('C'));
      for (var j = 0; j < 3; j++) all.push(pull('U'));
      var r = Math.random();                                   // rare slot, with premium upgrades
      if (r < 0.10 && byR.F) all.push(pull('F'));
      else if (r < 0.13 && byR.M) all.push(pull('M'));
      else if (r < 0.15 && byR.P) all.push(pull('P'));
      else all.push(pull('R'));
    }
    all.forEach(function (c) { DB.addOwned(state.acct, c.id, 1); });
    state.colMap = DB.collection(state.acct); markDirty();
    log('Opened ' + count + ' ' + (SET_NAMES[setNum] || ('set ' + setNum)) + ' pack' + (count > 1 ? 's' : '') +
        ': +' + all.length + ' cards to ' + acctName() + '.', 'ok');
    withArt(function (has) { renderPack(all, has); });
  }
  function boosterCard(c) {
    var meta = allById[c.id] || {}, rc = (c.rarity || 'C');
    var rar = RARITY[rc] || { name: rc, color: '#8b97a6' };
    var card = el('div', 'gcard r-' + rc.toLowerCase());
    card.style.setProperty('--glow', rar.color);
    var art = el('div', 'gart');
    var img = el('img'); img.loading = 'lazy'; img.alt = meta.name || c.name;
    img.src = 'art/images/card/' + c.id + '.jpg';
    img.onerror = function () { art.classList.add('noart'); art.textContent = (c.type || '?'); };
    art.appendChild(img); card.appendChild(art);
    card.appendChild(el('div', 'gname', meta.name || c.name));
    var mrow = el('div', 'gmeta');
    mrow.appendChild(el('span', 'gtype', c.type));
    var rp = el('span', 'grar', rar.name); rp.style.color = rar.color; mrow.appendChild(rp);
    card.appendChild(mrow);
    var st = statChips(c); if (st) card.appendChild(st);
    if (meta.text) card.appendChild(el('div', 'gtext', meta.text));
    return card;
  }
  function rowCard(c) {                                      // no-art fallback: one detailed row per card
    var meta = allById[c.id] || {}, rc = (c.rarity || 'C');
    var rar = RARITY[rc] || { name: rc, color: '#8b97a6' };
    var row = el('div', 'crow'); row.style.setProperty('--glow', rar.color);
    row.appendChild(typeBadge({ type: c.type }));
    row.appendChild(el('span', 'crname', meta.name || c.name));
    var rp = el('span', 'crrar', rar.name); rp.style.color = rar.color; row.appendChild(rp);
    var st = statChips(c); if (st) row.appendChild(st);
    if (meta.text) row.appendChild(el('span', 'crtext', meta.text));
    return row;
  }
  function renderPack(cards, hasArt) {
    var box = $('packResult'); if (!box) return; box.innerHTML = '';
    box.className = hasArt ? 'packgrid' : 'packrows';
    var order = { M: 0, P: 1, F: 2, A: 2, R: 3, U: 4, C: 5 };
    cards.slice().sort(function (a, b) { return (order[a.rarity] == null ? 9 : order[a.rarity]) - (order[b.rarity] == null ? 9 : order[b.rarity]); })
      .forEach(function (c, i) {
        var node = hasArt ? boosterCard(c) : rowCard(c);
        node.style.animationDelay = (i * (hasArt ? 45 : 15)) + 'ms'; box.appendChild(node);
      });
    $('boosterMeta').textContent = cards.length + ' cards added';
  }

  function typeBadge(c) { var t = c.type || '?'; var b = el('span', 'badge t-' + t.toLowerCase(), t.slice(0, 2).toUpperCase()); b.title = t; return b; }

  // ---- scenarios / campaign progress -------------------------------------
  // The game tracks unlock per CAMPAIGN CHAIN, not per scenario. Live-RE'd (FUN_00882e50 ->
  // account.getProperty(chainNode), mTypeID==2, value = furthest scenario cleared in that chain):
  // each of the 8 campaigns has a Light and a Dark chain of 5 scenarios = 16 unlock nodes total.
  // Granting a campaign sets both chain nodes to full progress, which unlocks all its scenarios.
  var CAMPAIGNS = [
    { label: 'Champions of the Force', nodes: [0x157e6, 0x157e7] },
    { label: 'Squadrons Over Corellia', nodes: [0x157ee, 0x157ef] },
    { label: 'Galactic Hunters', nodes: [0x157f4, 0x157f5] },
    { label: 'Agents of Deception', nodes: [0x157fb, 0x157fc] },
    { label: 'The Shadow Syndicate', nodes: [0x15801, 0x15802] },
    { label: "The Nightsister's Revenge", nodes: [0x15807, 0x15808] },
    { label: 'Threat of the Conqueror', nodes: [0x1580d, 0x1580e] },
    { label: 'The Price of Victory', nodes: [0x15813, 0x15814] }
  ];
  var GRANT_VALUE = 5;                                        // full chain progress = all 5 scenarios cleared

  function renderScenarios() {
    var meta = $('scnMeta'), prog = $('scnProgress'), grant = $('scnGrantList');
    if (!DB.isOpen()) { meta.textContent = 'No database open. Click "Open Account DB".'; prog.innerHTML = ''; grant.innerHTML = ''; return; }
    var comp = DB.scenarioCompletion(state.acct);            // [{node_id, archetype_id, difficulty}]
    var valByNode = {};                                      // chain node -> max stored progress value
    comp.forEach(function (r) { valByNode[r.node_id] = Math.max(valByNode[r.node_id] || 0, r.difficulty); });
    function done(c) { return c.nodes.every(function (n) { return valByNode[n]; }); }
    meta.innerHTML = 'Account <b>' + esc(acctName()) + '</b> &nbsp; ' +
      CAMPAIGNS.filter(done).length + ' / ' + CAMPAIGNS.length + ' campaigns unlocked';
    prog.innerHTML = '';
    var any = false;
    CAMPAIGNS.forEach(function (c) {
      var l = valByNode[c.nodes[0]] || 0, d = valByNode[c.nodes[1]] || 0;
      if (!l && !d) return; any = true;
      var row = el('div', 'row');
      row.appendChild(el('span', 'badge t-quest', 'SC'));
      row.appendChild(el('span', 'name', c.label));
      row.appendChild(el('span', 'tag', 'Light ' + l + '/5 · Dark ' + d + '/5'));
      prog.appendChild(row);
    });
    if (!any) prog.appendChild(el('div', 'more', 'No campaign progress yet. Play, or grant a campaign below.'));
    grant.innerHTML = '';
    CAMPAIGNS.forEach(function (c, idx) {
      var row = el('div', 'row');
      var cb = el('input', 'scnchk'); cb.type = 'checkbox'; cb.value = String(idx);
      var lbl = el('label', 'name', c.label); lbl.style.cursor = 'pointer'; lbl.onclick = function () { cb.checked = !cb.checked; };
      row.appendChild(cb); row.appendChild(lbl);
      if (done(c)) row.appendChild(el('span', 'tag own', 'unlocked'));
      grant.appendChild(row);
    });
  }
  function scnSelected() { return Array.prototype.map.call(document.querySelectorAll('.scnchk:checked'), function (c) { return parseInt(c.value, 10); }); }
  function scnSetAll(v) { Array.prototype.forEach.call(document.querySelectorAll('.scnchk'), function (c) { c.checked = v; }); }
  function grantScenarios() {
    if (!DB.isOpen()) { log('Open a database first.', 'warn'); return; }
    var idxs = scnSelected();
    if (!idxs.length) { log('Tick campaigns to unlock first.', 'warn'); return; }
    idxs.forEach(function (i) {
      CAMPAIGNS[i].nodes.forEach(function (n) { DB.grantCompletion(state.acct, n, 0, GRANT_VALUE); });
    });
    markDirty();
    log('Granted ' + idxs.length + ' campaign(s) complete to ' + acctName() + '. Save to game + relaunch.', 'ok');
    renderScenarios();
  }
  function resetScenariosAcct() {
    if (!DB.isOpen()) { log('Open a database first.', 'warn'); return; }
    DB.clearCompletion(state.acct); markDirty();
    log('Reset scenario progress for ' + acctName() + '. Save to game + relaunch to start fresh.', 'ok');
    renderScenarios();
  }
  function resetScenariosAll() {
    if (!DB.isOpen()) { log('Open a database first.', 'warn'); return; }
    DB.accounts().forEach(function (a) { DB.clearCompletion(a.id); }); markDirty();
    log('Reset scenario progress for ALL accounts. Save to game + relaunch.', 'ok');
    renderScenarios();
  }

  function init() {
    ['Avatar', 'Unit', 'Ability', 'Item', 'Tactic', 'Quest'].forEach(function (t) { var o = el('option', null, t); o.value = t; $('colType').appendChild(o); });
    AVATARS.forEach(function (a) { var o = el('option', null, a.name + '  (' + a.id + ')'); o.value = String(a.id); $('deckAvatar').appendChild(o); });

    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (tb) {
      tb.onclick = function () {
        document.querySelectorAll('.tab').forEach(function (x) { x.classList.remove('active'); });
        document.querySelectorAll('.tabpane').forEach(function (x) { x.classList.remove('active'); });
        tb.classList.add('active'); $('tab-' + tb.dataset.tab).classList.add('active');
      };
    });

    $('btnOpenDb').onclick = openDb;
    $('btnSave').onclick = saveDb;
    $('acct').onchange = function () { selectAccount(parseInt($('acct').value, 10)); };
    $('importDb').onchange = function (ev) { var f = ev.target.files[0]; if (f) importDbFile(f); };

    $('colSearch').oninput = debounce(renderCollection, 150);
    $('colType').onchange = renderCollection;
    $('colOwnedOnly').onchange = renderCollection;
    $('colGrantAll').onclick = grantAll;
    $('colClear').onclick = clearAll;

    $('deckSearch').oninput = debounce(renderDeckPool, 150);
    $('deckType').onchange = renderDeckPool;
    $('deckOwnedOnly').onchange = renderDeckPool;
    $('deckAvatar').onchange = function () { state.deck.avatar = parseInt($('deckAvatar').value, 10) || 0; renderDeckStats(); };
    $('deckName').oninput = function () { state.deck.name = $('deckName').value; };
    $('deckSave').onclick = saveDeck;
    $('deckNew').onclick = function () { state.deck = newDeck(); renderDeck(); log('New deck.', 'ok'); };
    $('deckLoad').onclick = function () { loadDeck($('deckFile').value); };
    $('deckDelete').onclick = deleteDeck;

    $('scnGrant').onclick = grantScenarios;
    $('scnSelAll').onclick = function () { scnSetAll(true); };
    $('scnSelNone').onclick = function () { scnSetAll(false); };
    $('scnResetAcct').onclick = resetScenariosAcct;
    $('scnResetAll').onclick = resetScenariosAll;

    $('openPack').onclick = function () { openPack(parseInt($('boosterSet').value, 10), 1); };
    $('openPack10').onclick = function () { openPack(parseInt($('boosterSet').value, 10), 10); };

    window.addEventListener('beforeunload', function (e) { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } });

    $('fsNote').textContent = state.supported
      ? 'Direct open/save of swgtcg.db is available in this browser.'
      : 'This browser has no direct file access -- use Import, and Save downloads the edited swgtcg.db to copy back.';

    state.deck = newDeck();
    log('Catalog: ' + PLAYABLE.length + ' playable cards, ' + AVATARS.length + ' avatars. Click "Open Account DB" to begin.');
    renderCollection(); renderDeck();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
