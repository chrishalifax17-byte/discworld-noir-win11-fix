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

## 3. CD check (not patched)

If `CD Path` isn't set, `0x440320` scans drive letters C–Z for `DRIVE_CDROM` and looks
for the disc's `volume` file. A mounted `.iso` satisfies this. The launcher created by
`setup.ps1` mounts one automatically.
