# Discworld Noir — Windows 10/11 fix

A small binary patch that lets the original 1999 PC release of **Discworld Noir** run
natively on Windows 10 and 11, without a virtual machine, ScummVM or admin rights.

It fixes two engine bugs that only show up on modern Windows:

| Symptom | Cause | Fix |
|---|---|---|
| Game quits with **`Cannot change directory to C:\Discworld Noir`** (or exit code 21) | The game opens its registry settings under `HKEY_LOCAL_MACHINE` with full write access. Windows refuses that for normal users, so the game falls back to a hard-coded folder that doesn't exist. | Settings are read from and written to `HKEY_CURRENT_USER` instead. |
| Random crashes with **`ProcessSleep() error 2`**, `ProcessKill() error 3`, `ProcessDispatch() error 2`, etc. | Every script coroutine in the Tinsel engine runs on a ~2 KB private stack. Windows 9x fit in that; Windows 10/11 system calls overflow it and trip the engine's stack-guard assert. | Each of the 72 script processes gets its own 256 KB stack. |

See [TECHNICAL.md](TECHNICAL.md) for the reverse-engineering details.

**This repository contains no game files.** You need your own installed copy of the game.

## Requirements

- Discworld Noir installed (Full install recommended) with this `tin3_dxd.exe`:
  - size **560,160 bytes**
  - SHA-256 `ae0bcdd0d083f5682d42e11e3046a9ecb4b0b2ac2807cb0dca2703966568ead0`

  The patcher checks this and refuses to touch any other build. If yours differs,
  please [open an issue](../../issues) and include the hash it prints.
- [Python 3](https://www.python.org/downloads/) (any recent version, standard library only).
- One of the game discs, or a disc image (`.iso`). The game checks for a CD, and
  Windows 10/11 can mount an `.iso` as a virtual drive.

This patch does **not** remove or bypass copy protection.

## Install

1. **Patch the executable** (a backup is saved as `tin3_dxd.exe.orig`):

   ```bat
   py -3 patch_noir.py "C:\DiscNoir\tin3_dxd.exe"
   ```

2. **Create per-user settings, a launcher and shortcuts**:

   ```bat
   powershell -ExecutionPolicy Bypass -File setup.ps1 -GameDir "C:\DiscNoir" -DiscImage "C:\Path\To\DWN_ENG_1.iso" -Shortcuts
   ```

   | Option | Meaning |
   |---|---|
   | `-GameDir` | Folder containing `tin3_dxd.exe` (required). |
   | `-DiscImage` | Your CD1 `.iso`. The launcher mounts it automatically. Leave it out if you use a real disc. |
   | `-Windowed` | Start in a 640×480 window instead of fullscreen. |
   | `-Shortcuts` | Add "Discworld Noir" shortcuts to the Desktop and Start menu. |

3. Start the game from the shortcut, or run `Play Discworld Noir.bat` in the game folder.

## Notes

- **Fullscreen** runs at the game's native 640×480. Your GPU or monitor scales it.
  To keep the 4:3 aspect ratio on a widescreen display, enable "Maintain aspect ratio"
  scaling in your GPU control panel (e.g. NVIDIA: *Adjust desktop size and position*).
- If you previously installed a `ddraw.dll` wrapper in the game folder that forces
  windowed mode, rename or remove it to get fullscreen.
- Saves made with the unpatched game may not load after patching. Starting a new game
  is the most reliable.
- Settings now live in `HKEY_CURRENT_USER\SOFTWARE\Perfect Entertainment\Discworld Noir`.

## Uninstall

```bat
py -3 patch_noir.py --restore "C:\DiscNoir\tin3_dxd.exe"
```

Then delete the `HKEY_CURRENT_USER\SOFTWARE\Perfect Entertainment` key, the shortcuts,
and `Play Discworld Noir.bat` if you like.

## Status

Tested on Windows 11 (64-bit): boots, main menu, new game, intro cutscene, walking and
talking in the first scene, fullscreen and windowed. Reports from longer playthroughs
are welcome.

## Legal

Discworld Noir © 1999 Perfect Entertainment / TWG. "Discworld" is a trademark of the
Pratchett estate. This project is unofficial and unaffiliated. It ships only a patch
script and documentation under the MIT license.
