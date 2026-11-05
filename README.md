# Roblox Rejoiner

A Windows app for monitoring multiple Roblox accounts independently.
Use the **Page** dropdown in the top bar to switch sections; each section
appears below the bar across the window.

## Run the app

Download [Roblox Rejoiner.exe](https://github.com/imagesent/Roblox-Rejoin/releases/latest/download/Roblox%20Rejoiner.exe)
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

Session cookies are sensitive credentials, so only paste cookies for accounts
you own. On Windows, saved cookies are encrypted with DPAPI.

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

## Support

Having a problem or found a bug? Message me on Discord: [Contact me](https://discord.com/users/244804524043141128)
