# Place your TCG Standalone game files here

**Place the root contents of your *Star Wars Galaxies: Trading Card Game* Standalone
client in this folder.**

You can find this client by searching the Internet Archive or by asking other
community members. **The Standalone Client is not distributed with these files.**

When placed correctly, this folder should contain (directly, not in a subfolder):

```
SWGTCGGame.exe
QtCore4.dll, QtGui4.dll, QtNetwork4.dll, ... (Qt runtime DLLs)
msvcr80.dll, msvcp80.dll, msvcm80.dll (VC++ 2005 runtime)
cards.rcc, effects.rcc, foil.rcc, resources.rcc, sounds.rcc
host.svr
data\  (archetypes, collections, decks, tutorial, campaign.dat, ...)
locale\
```

Once the files are in place, run `Play SWGTCG.cmd` from the parent folder.
