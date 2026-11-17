# Technical notes

Analysis was done in Ghidra on `tin3_dxd.exe` (Borland C++ 5, image base `0x400000`,
SHA-256 `ae0bcdd0…ead0`). Addresses below are virtual addresses in that build.

## 1. Script-process stack overflow (`ProcessSleep() error 2`)

The Tinsel 3 engine (`..\source\process.c`, `..\pc\winsched.c`) runs scripts as
cooperative coroutines built on `setjmp`/`longjmp` (`setjmp` is `0x41abd8`, `longjmp` is `0x41abf5`).

- **Pool init** (`0x45c0a0`): allocates `"processList"`, 0x258C0 bytes, filled with `'S'`
  and split into **72 slots of 0x858 bytes**. The pool pointer is stored at `0x5c8a18`.
- **ProcessCreate** (`0x45c310`): takes a free slot, writes a guard byte `'w'` at
  `pProc+0x58`, calls `setjmp(pProc+4)`, then sets the process up via `0x43fdb4`.
- **Stack set-up** (`0x43fcf0`): stores the coroutine's initial ESP into the jmp_buf:

  ```asm
  43fd57  lea  eax, [ebx+0x857]
  43fd5d  and  eax, 0xfffffffc
  43fd60  mov  [ebx+0x20], eax      ; jmp_buf.esp
  ```

  So the stack grows down from the top of the 0x858-byte slot toward the header,
  leaving about **2 KB**.
- **ProcessCheckStack** (`0x45c80c`) returns `pProc[0x58] == 'w'`. It is called by
  ProcessSleep, ProcessKill, ProcessDispatch, etc. When the stack overflows into the
  header the guard is gone and the engine asserts, e.g. `ProcessSleep() error 2`.

Win9x `user32`/`kernel32` thunks were shallow enough to fit. On Windows 10/11, calls
made from script processes into `user32`, `ntdll`, DirectSound and DirectDraw use far
more stack, so the guard gets trampled.

**Patch:** append a section `.noirstk` (RWX, 0x200 bytes raw plus about 18.3 MB
uninitialised). It holds a code cave, and the 12 bytes at `0x43fd57` become
`jmp cave; nop×7`. The cave does:

```asm
push edx / push ecx
mov  eax, ebx
sub  eax, [0x5c8a18]          ; offset into pool
xor  edx, edx
mov  ecx, 0x858
div  ecx                       ; slot index 0..71
inc  eax
shl  eax, 18                   ; (index+1) * 256 KB
add  eax, stacks_base - 16
mov  [ebx+0x20], eax
pop  ecx / pop edx
jmp  0x43fd63
```

Each process now gets a private 256 KB stack. The guard byte is never touched again
because the stack no longer lives inside the slot. Nothing else refers to a process's
stack location: saves store process state, not stacks.

## 2. Registry access (`Cannot change directory to C:\Discworld Noir`)

Three helpers (read `0x43f814`, write `0x43f704`, and the `\Stats` helper) call

```c
RegCreateKeyExA(HKEY_LOCAL_MACHINE, "SOFTWARE\\Perfect Entertainment\\Discworld Noir",
                0, "", 0, KEY_ALL_ACCESS, NULL, &hKey, NULL);
```

A standard user can't get `KEY_ALL_ACCESS` on HKLM, and registry virtualisation
doesn't reliably cover it. The read then returns the default `"Moving Targets"`.
The path set-up routine (`0x440320`) treats that as "no HD Path", writes and uses the
built-in `C:\Discworld Noir`, fails `SetCurrentDirectoryA`, shows the message box and
exits.

**Patch:** the three `push 0x80000002` instructions become `push 0x80000001`
(`HKEY_CURRENT_USER`). `setup.ps1` creates the matching per-user key.

## 3. Display: borderless scaled fullscreen

`0x47b24c` is the fullscreen flag (1 = exclusive 640×480, 0 = windowed). In exclusive
mode the desktop switches to 640×480×16. After a focus change on Windows 10/11 the game
restores its surfaces but keeps writing 16-bit pixels into what is now a 32-bit
surface. The result is a magenta, half-width picture, and Windows also squashes other
windows to fit 640×480.

The windowed path works (with dolomite's `ddraw.dll` providing the colour
conversion), and it already copies the 640×432 back buffer to a destination rectangle
`{L,T,R,B}` at `0x5bdf7c`, recomputed on `WM_MOVE`/`WM_SIZE` (WndProc `0x448234`) from
`GetClientRect` + `ClientToScreen`. DirectDraw `Blt` stretches to that rectangle.

Patches:

| Address | Original | Patched |
|---|---|---|
| `0x47b24c` (data) | `1` | `0`: always start in the windowed renderer |
| `0x446744` | `and eax,0x7fffffff; or eax,0xc60000` | `and eax,0xff00ffff; or eax,0x80000000`: `WS_POPUP`, no caption or frame |
| `0x44675f` | `push 432; push 640` (before `SetRect`) | `call cave`: pushes `GetSystemMetrics(SM_CYSCREEN/SM_CXSCREEN)` |
| `0x4467c8` | `push 0x16` (SetWindowPos flags) | `push 0x34`: move to 0,0 and `SWP_FRAMECHANGED` |
| `0x446855` | `push 0x15` (work-area adjust) | `push 0x17`: keep the window at 0,0 |
| `0x448258` | `jmp 0x448737` | `jmp cave`: shrink the rectangle to the largest centred 640:432 area |
| `0x43fb61` | `ScreenToClient(hwnd, &cursor)` | `jmp cave`: `cursor = (screen − origin) × 640/432 ÷ extent`, clamped |
| `0x448506` | `mov [0x47b24c], edx` (Alt+Enter toggle) | NOPs: never enter exclusive mode |
| `0x4487db` | `WNDCLASS.hbrBackground = NULL` | `= COLOR_WINDOWTEXT+1`: black bars |

The cursor is read in one place (`0x43fb3c`, `GetCursorPos` → `0x479fd8/0x479fdc`), and
clicks use the same stored position, so one mapping covers all mouse input.

## 4. Controller support (XInput)

The game only reads input through Windows messages (`WM_MOUSE*`, `WM_KEY*`) and
`GetCursorPos`. Both message loops (`0x43f597` main loop, `0x447f19`) call `PeekMessageA`
through the import thunk at `0x464b9d` (`jmp [0x5ca4f0]`). The patch replaces that
thunk with `jmp pad_hook`, which saves all registers, calls `pad_poll`, restores the
registers and jumps on to the real `PeekMessageA`.

`pad_poll` is written in C ([controller/noirpad.c](controller/noirpad.c)), compiled
freestanding for i386 and linked at `0x5d6200` inside `.noirstk`
([controller/build.sh](controller/build.sh) generates `noirpad_blob.py`). It:

- calls only functions the game already imports, through its IAT (`GetModuleHandleA`,
  `GetProcAddress`, `GetCursorPos`, `SetCursorPos`, `PostMessageA`,
  `GetForegroundWindow`, `timeGetTime`), and resolves `LoadLibraryA` and
  `XInputGetState` (`xinput1_4` / `1_3` / `9_1_0`) at runtime;
- keeps its state in the zero-filled tail of the section's first page (`0x5d6f00`);
- moves the real cursor with `SetCursorPos`, time-based and clamped to the picture
  rectangle, so the game's own mouse code (and the display patch's mapping) applies;
- posts `WM_LBUTTON*` / `WM_RBUTTON*` / `WM_LBUTTONDBLCLK` / `WM_KEYDOWN VK_ESCAPE` to the
  game window on button edges. The game's handlers use the stored cursor position,
  not `lParam`;
- only acts while the game window has focus, and polls a disconnected controller at
  most once a second, since XInput is slow on empty slots.

## 5. CD check (not patched)

If `CD Path` isn't set, `0x440320` scans drive letters C–Z for `DRIVE_CDROM` and looks
for the disc's `volume` file. A mounted `.iso` satisfies this. The launcher created by
`setup.ps1` mounts one automatically.
