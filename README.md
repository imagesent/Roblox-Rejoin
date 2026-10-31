# Roblox Auto-Rejoiner

A Windows app that monitors Roblox and tries to rejoin your chosen game after a crash or disconnect.

## Run the app

For a ready-to-run Windows app, download `Roblox Auto-Rejoiner.exe` from the
latest [GitHub Release](https://github.com/imagesent/Roblox-Rejoin/releases)
and run it. Python and separate package installation are not required.

1. Download or clone this project.
2. Double-click `Setup and Run.cmd`.
   - If the packaged app is present, it opens directly.
   - If it needs to build the app, install Python 3.10 or newer and run the script again.
3. In the app, search for a game or enter its Place ID.
4. Set the rejoin delay and click **Save settings**.
5. Click **Join + Start Monitor** to join the game and start monitoring, or **Start Monitor** to monitor an already-open Roblox client.

The monitor resolves the selected Place ID to its experience (Universe ID), so
teleports between places in that same experience are not mistaken for a
disconnect.

## Support

Having a problem or found a bug? Message me on Discord: [Contact me](https://discord.com/users/244804524043141128)
