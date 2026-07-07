"""Platform configuration (leaf module; imports nothing from the project).

All values overridable via environment variables so the same code runs for
same-machine testing (127.0.0.1) and a real LAN/host deployment.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# --- Network -------------------------------------------------------------
# The server's reachable IP, handed to the client as the lobby host in the
# gateway reply. 127.0.0.1 for same-machine testing; the LAN IP for remote clients.
SERVER_IP  = os.environ.get("SWGTCG_SERVER_IP", "127.0.0.1")
GW_PORT    = int(os.environ.get("SWGTCG_GW_PORT", "16782"))
LOBBY_PORT = int(os.environ.get("SWGTCG_LOBBY_PORT", "16783"))

# Gateway hostname the client is launched with (--host=); must resolve to this
# server on each client machine (hosts file / DNS). The one truly forced constraint.
GW_HOSTNAME = os.environ.get("SWGTCG_GW_HOSTNAME", "sdkccg-02-04.station.sony.com")

# --- Persistence ---------------------------------------------------------
DB_PATH = os.environ.get("SWGTCG_DB_PATH", os.path.join(_HERE, "swgtcg.db"))

# --- Auth ----------------------------------------------------------------
SESSION_TTL_SECONDS = int(os.environ.get("SWGTCG_SESSION_TTL", str(30 * 60)))
PW_ITERATIONS       = int(os.environ.get("SWGTCG_PW_ITERS", "600000"))

# Default entitlements granted to a new account. Must match the wire StringList the
# client gates Casual Games / Scenarios on (attr 0xfc5). "Staff"/"WorldsApart" are
# admin/special and granted explicitly, not by default.
DEFAULT_ENTITLEMENTS = ("RegisteredUser", "SubscriptionMember", "ScenarioEnabled", "StationAccessUser")

# Default starter deck id seeded to a new account on first login (standalone_cards: 111-114).
DEFAULT_STARTER_DECK = int(os.environ.get("SWGTCG_DEFAULT_STARTER", "111"))
