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
7. Each account is tracked independently. If one client crashes, that account
   is rejoined; if multiple clients crash, each affected account is rejoined
   separately. Relaunches are queued and staggered to avoid mixing up Roblox
   client processes.

Session cookies are sensitive credentials, so only paste cookies for accounts
you own. On Windows, saved cookies are encrypted with DPAPI.

The monitor resolves a selected Place ID to its experience (Universe ID), so
teleports between places in that same experience are not mistaken for a
disconnect.

Failed joins are retried until the monitor is stopped or a join succeeds. The
retry delay increases up to 60 seconds between attempts.

Multi-account settings and encrypted cookies are stored under
`%APPDATA%\Roblox Rejoin`.

## Support

Having a problem or found a bug? Message me on Discord: [Contact me](https://discord.com/users/244804524043141128)
