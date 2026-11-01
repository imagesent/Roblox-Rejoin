# Roblox Auto-Rejoiner

A Windows app that monitors Roblox and tries to rejoin your chosen game after a crash or disconnect.

## Run the app

Download **[Roblox Auto-Rejoiner.exe](https://github.com/imagesent/Roblox-Rejoin/releases/latest/download/Roblox.Auto-Rejoiner.exe)**
from the latest release and run it on Windows. The standalone app does not
require Python or a separate package installation.

To run from source or build locally:

1. Download or clone this project.
2. Install Python 3.10 or newer.
3. Double-click `Setup and Run.cmd`. It installs dependencies and builds and
   launches the app; subsequent runs open the built app directly.
4. In the app, search for a game or enter its Place ID.
5. Set the rejoin delay and click **Save settings**.
6. Click **Join + Start Monitor** to join the game and start monitoring, or
   **Start Monitor** to monitor an already-open Roblox client.

The monitor resolves the selected Place ID to its experience (Universe ID), so
teleports between places in that same experience are not mistaken for a
disconnect.

Failed joins are retried until the monitor is stopped or a join succeeds. The
retry delay increases up to 60 seconds between attempts.

## Support

Having a problem or found a bug? Message me on Discord: [Contact me](https://discord.com/users/244804524043141128)
