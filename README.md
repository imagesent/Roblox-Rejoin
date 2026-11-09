# Roblox Rejoiner

A Windows app for monitoring multiple Roblox accounts independently.
Use the **Page** dropdown in the top bar to switch sections; each section
appears below the bar across the window.

## Run the app

<!-- The release asset name uses a dot, not a space: Roblox.Rejoiner.exe -->
Download [Roblox Rejoiner.exe](https://github.com/imagesent/Roblox-Rejoin/releases/latest/download/Roblox.Rejoiner.exe)
from the latest release and run it on Windows. The standalone app does not
require Python or a separate package installation.

To run the app from source or build the standalone executable:

1. Download or clone this project.
2. Install Python 3.10 or newer.
3. Double-click `Setup and Run.cmd`. It installs dependencies and builds and
   launches the app; subsequent runs open the built app directly.
   The default appearance is Graphite; choose another palette under **Rejoiner
   Setup** → **Appearance**.
   The interface now uses your system's native window controls with a flatter,
   desktop-style layout instead of the previous macOS-inspired frame.
4. Open **Manage Accounts** → **Add Roblox account**. Paste that account's
   `.ROBLOSECURITY` session cookie into the dialog; optionally add an alias,
   and note. Repeat for each account. Accounts are selected for
   monitoring by default; use their switches to choose a subset.
5. In **Rejoiner Setup** → **Find a game**, search by name and click the desired
   result. The app saves the experience's
   Universe ID for monitoring and its root Place ID for launching; sub-place
   teleports within that same experience are treated as the same game. Game
   icons appear beside search results, and the selected icon is saved locally
   so it remains visible after restarting the app until you choose a new game.
6. Open **Account Monitor** and click **Start**.
7. Each account is tracked independently. Every way an account can be
   removed from an instance is detected and handled on its own: client
   crashes and forced closes, native kick/disconnect dialogs (the real
   reason and Error Code are read from the dialog), frozen clients,
   kicks where the client stays open, connection loss, clients stuck on
   the loading screen, joins landing in the wrong game, and joins
   landing in a different server than a requested private server. If
   multiple clients crash, each affected account is rejoined separately.
   Relaunches are queued and staggered to avoid mixing up Roblox client
   processes.

## Private servers and specific servers

**Rejoiner Setup** → **Join a specific server** decides where monitored accounts
are sent. Paste a link from **any** of the shapes Roblox uses:

| What you have | Example |
| --- | --- |
| The link on your private server | `https://www.roblox.com/games/123/Game?privateServerLinkCode=…` |
| A share link | `https://www.roblox.com/share?code=…&type=Server` |
| One specific public server | `https://www.roblox.com/games/123/Game?gameInstanceId=…` (or a job id) |
| A game, any server | `https://www.roblox.com/games/123/Game` or a bare Place ID |

**Which accounts** the link applies to is one compact row - *"Send these
accounts to the private server"* - that opens a dropdown listing your accounts.
Ticked ones are sent to the private server, unticked ones use the game they
were set up with (their **Game / place** row, or the default place on Rejoiner
Setup). All / None clears the set in one click, the row's summary counts them
("1 of 3 accounts sent to the private server"), and the same switch lives on
each account's own row under **Manage Accounts** - both write to the same
setting. So sending exactly one account into the private server while the rest
keep farming their own games is a single click, whichever page you are on.

**Pick a server…** searches Roblox by game name — the same search the Setup
page uses, icons and all — then lists *that game's* private servers, or every
private server the account owns with *All my private servers*, and fills in the
join link for you so links never have to be copied out of a browser. The list
only ever contains servers the account actually owns; when the game you picked
has none, it says so instead of showing every server you have.

Press **Check link** before starting: the app reports what the link points at
(private server, one specific server, or the game itself) and which place and
server Roblox actually resolved.

If a link cannot be resolved, the account waits with the reason on screen
instead of being launched into public matchmaking — the failure that used to
dump whole batches into random servers. After five failed attempts the account
stops and says the link needs fixing.

While monitoring, each join is verified against the server the link pointed at:
a client that lands elsewhere is rejoined (up to three times), after which the
reason is logged and the account is left where Roblox put it — private servers
get a new job id whenever Roblox restarts them, so the engine believes the
majority of accounts over a stale lookup.

Discord embeds are branded with the app's own icon rather than the game's
thumbnail, so notifications from the tool are recognisable at a glance.

The same settings are available to the Discord bot through the state/command
bridge: `join_server` accepts `on`, `off`, `all`, `none`, a number of accounts,
or a link; `account_join` ticks the private server for one account, or for a
comma-separated list of names.

Session cookies are sensitive credentials, so only paste cookies for accounts
you own. On Windows, saved cookies are encrypted with DPAPI.

The **Settings** page holds the Roblox installation folder, the Discord webhook and the app's automatic self-update settings in one place.

Optional Discord notifications are sent as color-coded embeds — one clean
card per event (launch, crash, kick, disconnect, server move, stop) with the
account, place, server and reason spelled out, and multi-account summaries
list each account on its own spaced block. Only usernames and events are
sent, never cookies.

Errors are classified by Roblox's error codes: moderation kicks and
disconnects rejoin immediately, temporarily-unavailable games (updating,
shut down, full) back off two minutes, session-overlap codes wait for
Roblox presence to clear, and conditions rejoining cannot fix (no
permission, expired cookie, platform locks) stop that account and ask for
attention instead of retrying forever.

The standalone exe updates itself: it checks GitHub releases shortly after
launch and every six hours, downloads a newer release automatically, swaps
it in, and restarts when the app is idle (or offers "Restart & update" on
the Setup page while monitoring is active).

The monitor resolves a selected Place ID to its experience (Universe ID), so
teleports between places in that same experience are not mistaken for a
disconnect.

Failed joins are retried until the monitor is stopped or a join succeeds. The
retry delay increases up to 60 seconds between attempts.

Multi-account settings and encrypted cookies are stored under
`%APPDATA%\Roblox Rejoin`.

## Development

Run the test suite from the repository root:

```
python -m unittest discover -s tests -p "test_*.py" -v
python tests/smoke_ui.py
```

The unit tests cover link parsing and launch-URI building, share-link
resolution, and the engine's private-server targets (including that a
link which will not resolve holds its account back instead of falling back
to a public server). `tests/smoke_ui.py` builds the whole interface against
a stubbed Roblox API and checks the join-link settings, the per-account
overrides, and the bot bridge commands. Neither needs a network connection
or an account.

## Support

Having a problem or found a bug? Message me on Discord: [Contact me](https://discord.com/users/244804524043141128)
