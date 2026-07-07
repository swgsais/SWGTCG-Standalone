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
    renderCollection(); renderDeck();
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

  function typeBadge(c) { var t = c.type || '?'; var b = el('span', 'badge t-' + t.toLowerCase(), t.slice(0, 2).toUpperCase()); b.title = t; return b; }

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
