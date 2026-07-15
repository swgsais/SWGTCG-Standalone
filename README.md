# THIS REPO IS NO LONGER MAINTAINED
Visit https://tcg.galaxiesreborn.com to download the latest launcher.


# SWGTCG-Standalone

A self-contained, **offline single-player** setup for the retail *Star Wars Galaxies:
Trading Card Game* Standalone client. It plays entirely on your machine &mdash; no
internet, no real account, no install &mdash; and includes a browser-based collection
& deck manager.

> **The retail game client is NOT included** (it is copyrighted and not distributed
> here). You supply it yourself &mdash; see [Getting the client](#getting-the-client).

---

## Community

For the latest TCG updates and other Star Wars Galaxies projects:

- **[Galaxies Reborn Discord](https://discord.gg/CEwKVvKxK5)**
- **[r/GalaxiesReborn on Reddit](https://www.reddit.com/r/GalaxiesReborn/)**

## Getting the client

Everything under [`TCGStandalone/`](TCGStandalone/) is intentionally excluded from this
repo except a placeholder README. Obtain the *SWG TCG Standalone* client (search the
Internet Archive for TCGStandalone, or ask other community members) and copy its root contents into
`TCGStandalone/` so that `TCGStandalone/SWGTCGGame.exe` exists. See
[`TCGStandalone/README.md`](TCGStandalone/README.md) for the expected file list.

## Why there is a "login" step

The retail client greys out every game mode until it has logged in. So this package
ships a tiny **local login server** (in `_ext/`, with its own bundled Python &mdash;
nothing to install). The launcher starts it and launches the client already
"logged in", which bypasses the Login screen and makes **PLAY** usable. The games
(Tutorials / Scenarios / Skirmish) then run locally against the AI.

## One-time setup

The client's login host must resolve to `127.0.0.1`. Pick one:

- **Easy:** run **`Add Hosts Entries.cmd`** and accept the admin prompt (idempotent).
- **Manual:** edit `C:\Windows\System32\drivers\etc\hosts` (as Administrator) and add:

  ```
  127.0.0.1   sdkccg-02-04.station.sony.com
  127.0.0.1   sdkccg-02-11.station.sony.com
  ```

Without these lines the client cannot reach the local server and PLAY stays greyed out.
To undo, delete those two lines.

## Play

1. Do the one-time setup above (and place the client &mdash; see above).
2. Run **`Play SWGTCG.cmd`** &rarr; **[1] Play**.
3. When the lobby appears, move to the **left edge** &rarr; the Navigator slides out.
   Click **PLAY &rarr; Tutorials / Scenarios**, then pick a mode:
   - **Tutorials** &mdash; guided lessons (ships its own decks)
   - **Scenarios** &mdash; story battles vs the AI
   - **Skirmish** &mdash; *"Test your newest deck designs"* vs four AI opponents

## Collection & Deck Manager

Your owned cards and decks live in the account database `_ext/server/swgtcg.db`
(served to the client at login). The browser tool in `collectionmanager/` edits it
&mdash; no web server needed.

1. Launcher **[2] Collection & Deck Manager** (stops the login server so the DB is free,
   and opens the tool).
2. **Open Account DB** &rarr; `_ext/server/swgtcg.db`; pick the account (default
   `tester1`); edit owned cards + build decks.
3. **Save to game.** If the browser downloads `swgtcg.db` instead of saving in place,
   use launcher **[6] Apply edited DB from Downloads**.
4. **[1] Play** &mdash; changes are read at login.

## Layout

```
Play SWGTCG.cmd        launcher entry (double-click)
launcher.ps1           menu launcher (starts server + auto-login client)
Add Hosts Entries.cmd  one-time hosts setup (self-elevating)
Collection Manager.cmd opens the deck/collection tool
_ext/                  bundled login server + embeddable Python + swgtcg.db
collectionmanager/     offline HTML tool (sql.js) that edits the account DB
TCGStandalone/         <-- you place the retail client here (not distributed)
```

## Requirements / notes

- 64-bit Windows. Bundled runtimes (embeddable Python; the client ships Qt 4 + VC++ 2005).
- The hosts entries are the only system change; remove them anytime.
- R&D / preservation project. The bundled account database contains only throwaway
  local test accounts (`tester1`&hellip;`u2`); the local login does not validate credentials.

## Attributions & Credits

Thanks to the community members who helped make this possible:

| Who | Contribution |
|-----|--------------|
| **/u/Tosteto** (Reddit) | Uploaded *The Price of Victory* rulebook (Version 8) to the Internet Archive &mdash; the rules source for this project. |
| **Carbonitex** | Shared retail TCG client files and a custom launcher project for research and development. |
| **Metasharp** | Shared client resources/data from the final live version. |
| **/u/Cigaran** (Reddit) | Provided the Standalone client used to research and develop this project. |
