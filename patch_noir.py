"""
Discworld Noir (1999, PC) - Windows 10/11 compatibility patch for tin3_dxd.exe.

Fix 1 - "ProcessSleep() error 2" / random asserts and crashes
  The Tinsel engine's scheduler runs every script "process" as a
  setjmp/longjmp coroutine whose stack lives *inside* its 0x858-byte slot of
  the "processList" pool (72 slots), i.e. roughly 2 KB of stack each.  A guard
  byte 'w' at pProc+0x58 is checked by ProcessCheckStack; when it is
  overwritten the game asserts ("ProcessSleep() error 2", "ProcessKill()
  error 3", ...).  Windows 9x system DLLs fit in 2 KB; Windows 10/11 call
  chains (user32/ntdll/dsound/ddraw) do not.
  -> A new PE section ".noirstk" is appended holding a small code cave and a
     large uninitialised region, and the initial-ESP computation at 0x43fd57
     is redirected so each process gets its own 256 KB stack.

Fix 2 - "Cannot change directory to C:\\Discworld Noir"
  The game opens HKLM\\SOFTWARE\\Perfect Entertainment\\Discworld Noir with
  KEY_ALL_ACCESS.  For a normal (non-admin) user on modern Windows this fails,
  the game falls back to the hard-coded default "C:\\Discworld Noir" and quits.
  -> The three registry helpers are changed to use HKEY_CURRENT_USER.
     Run setup.ps1 afterwards to create the per-user settings.

Fix 3 - fullscreen corruption (magenta / half-width picture after alt-tab)
  Exclusive 640x480 fullscreen switches the desktop to 16-bit; when focus is
  lost and regained the game keeps writing 16-bit pixels to a 32-bit surface.
  -> The game always runs in its windowed renderer, but the window is made
     borderless and screen-sized, the picture is scaled to the largest
     centred 640:432 rectangle with black bars, mouse coordinates are mapped
     back, and Alt+Enter no longer switches to exclusive fullscreen.
     Needs dolomite's ddraw.dll for correct colours in windowed mode.

Usage:
  py -3 patch_noir.py "C:\\Path\\To\\Game\\tin3_dxd.exe"            apply
  py -3 patch_noir.py --restore "C:\\Path\\To\\Game\\tin3_dxd.exe"  undo

Only the exact build listed in ORIG_SHA256 is patched.  The untouched
original is kept next to it as tin3_dxd.exe.orig.
"""
import hashlib
import os
import shutil
import struct
import sys

ORIG_SHA256 = "ae0bcdd0d083f5682d42e11e3046a9ecb4b0b2ac2807cb0dca2703966568ead0"
ORIG_SIZE = 560160
IMAGE_BASE = 0x400000
PATCH_VA = 0x43FD57          # LEA EAX,[EBX+0x857] / AND EAX,-4 / MOV [EBX+0x20],EAX
PATCH_ORIG = bytes.fromhex("8d8357080000" "83e0fc" "894320")
RETURN_VA = 0x43FD63         # POP EBX ...
POOL_PTR = 0x5C8A18          # global pointer to the processList pool
SLOT_SIZE = 0x858
NUM_SLOTS = 0x258C0 // SLOT_SIZE   # 72
STACK_SHIFT = 18             # 256 KB per process
SECT_NAME = b".noirstk"
HKLM_PUSH = b"\x68\x02\x00\x00\x80"   # push 0x80000002 (HKEY_LOCAL_MACHINE)


def align(v, a):
    return (v + a - 1) // a * a


def patch(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        sys.exit("Not a PE file.")
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    sect_align, file_align = struct.unpack_from("<II", data, opt + 32)
    size_of_headers = struct.unpack_from("<I", data, opt + 60)[0]
    table = opt + opt_size

    sections = []
    for i in range(nsec):
        off = table + i * 40
        vsize, va, rsize, rptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((vsize, va, rsize, rptr))

    new_hdr = table + nsec * 40
    first_raw = min(s[3] for s in sections if s[3])
    if new_hdr + 40 > min(size_of_headers, first_raw):
        sys.exit("No room in PE header for another section.")

    def va_to_off(va):
        rva = va - IMAGE_BASE
        for vs, sva, rs, rp in sections:
            if sva <= rva < sva + max(vs, rs):
                return rp + (rva - sva)
        raise ValueError(hex(va))

    poff = va_to_off(PATCH_VA)
    if bytes(data[poff:poff + 12]) != PATCH_ORIG:
        sys.exit("Patch site bytes differ; not patching.")

    hits = [i for i in range(len(data) - 4) if data[i:i + 5] == HKLM_PUSH]
    if len(hits) != 3:
        sys.exit(f"Expected 3 HKLM references, found {len(hits)}; not patching.")

    last_end = max(va + max(vs, rs) for vs, va, rs, _ in sections)
    new_rva = align(last_end, sect_align)
    cave_va = IMAGE_BASE + new_rva
    stacks_va = cave_va + 0x1000
    vsize = 0x1000 + (NUM_SLOTS + 1) * (1 << STACK_SHIFT)

    # --- Fix 1: per-process stacks --------------------------------------
    code = bytearray()
    code += b"\x52"                                         # push edx
    code += b"\x51"                                         # push ecx
    code += b"\x89\xd8"                                     # mov eax, ebx      (pProc)
    code += b"\x2b\x05" + struct.pack("<I", POOL_PTR)       # sub eax, [pool]
    code += b"\x31\xd2"                                     # xor edx, edx
    code += b"\xb9" + struct.pack("<I", SLOT_SIZE)          # mov ecx, 0x858
    code += b"\xf7\xf1"                                     # div ecx           -> slot index
    code += b"\x40"                                         # inc eax
    code += b"\xc1\xe0" + bytes([STACK_SHIFT])              # shl eax, 18
    code += b"\x05" + struct.pack("<I", stacks_va - 0x10)   # add eax, stacks - 16
    code += b"\x89\x43\x20"                                 # mov [ebx+0x20], eax (jmp_buf ESP)
    code += b"\x59"                                         # pop ecx
    code += b"\x5a"                                         # pop edx
    jmp_from = cave_va + len(code) + 5
    code += b"\xe9" + struct.pack("<i", RETURN_VA - jmp_from)  # jmp back

    raw_size = align(max(len(code), 0x800), file_align)   # caves + controller code
    raw_ptr = align(len(data), file_align)
    data += b"\0" * (raw_ptr - len(data))
    data += code + b"\0" * (raw_size - len(code))

    struct.pack_into("<8sIIIIIIHHI", data, new_hdr, SECT_NAME, vsize, new_rva,
                     raw_size, raw_ptr, 0, 0, 0, 0, 0xE0000060)
    struct.pack_into("<H", data, pe + 6, nsec + 1)
    sections.append((vsize, new_rva, raw_size, raw_ptr))
    struct.pack_into("<I", data, opt + 56, align(new_rva + vsize, sect_align))  # SizeOfImage

    rel = cave_va - (PATCH_VA + 5)
    data[poff:poff + 12] = b"\xe9" + struct.pack("<i", rel) + b"\x90" * 7

    # --- Fix 2: HKLM -> HKCU ---------------------------------------------
    for i in hits:
        data[i + 1] = 0x01

    # --- Fix 3: borderless scaled fullscreen -------------------------------
    patch_display(data, va_to_off, cave_va + len(code))

    # --- Fix 4: Xbox / XInput controller support ---------------------------
    patch_controller(data, va_to_off, cave_va)

    return data


PEEKMESSAGE_THUNK = 0x464B9D       # jmp [PeekMessageA IAT]; called by both message loops
PEEKMESSAGE_THUNK_ORIG = bytes.fromhex("ff25f0a45c00")


def patch_controller(data, va_to_off, cave_va):
    """Copy the compiled controller code (controller/noirpad.c) into the patch
    section and route the PeekMessageA thunk through it."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from noirpad_blob import BASE, HOOK, BLOB
    except ImportError:
        sys.exit("noirpad_blob.py is missing - keep it next to patch_noir.py.")
    if BASE != cave_va + 0x200:
        sys.exit(f"Controller code was built for {BASE:#x}, section is at {cave_va:#x}.")
    if BASE + len(BLOB) > cave_va + 0x800:
        sys.exit("Controller code is too large for the patch section.")
    off = va_to_off(PEEKMESSAGE_THUNK)
    if bytes(data[off:off + 6]) != PEEKMESSAGE_THUNK_ORIG:
        sys.exit("PeekMessageA thunk differs; not patching.")
    boff = va_to_off(BASE)
    data[boff:boff + len(BLOB)] = BLOB
    data[off:off + 6] = b"\xe9" + struct.pack("<i", HOOK - (PEEKMESSAGE_THUNK + 5)) + b"\x90"


# Addresses used by the display patch (see TECHNICAL.md).
FULLSCREEN_FLAG = 0x47B24C   # 1 = exclusive fullscreen, 0 = windowed
CURSOR_X, CURSOR_Y = 0x479FD8, 0x479FDC
DEST_L, DEST_T, DEST_R, DEST_B = 0x5BDF7C, 0x5BDF80, 0x5BDF84, 0x5BDF88
IAT_GETSYSTEMMETRICS = 0x5CA440
GAME_W, GAME_H = 640, 432

DISPLAY_SITES = {
    # va: (original bytes, replacement bytes or None for "jump/call to cave")
    0x446744: bytes.fromhex("81e0ffffff7f" "81c80000c600"),  # style &= ~WS_POPUP; |= caption/border
    0x44675f: bytes.fromhex("68b0010000" "6880020000"),      # SetRect(.., 640, 432)
    0x4467c8: bytes.fromhex("6a16"),                          # SetWindowPos flags (no move)
    0x446855: bytes.fromhex("6a15"),                          # SetWindowPos to work-area corner
    0x448258: bytes.fromhex("e9da040000"),                    # WM_SIZE/WM_MOVE: end of rect calc
    0x43fb61: bytes.fromhex("b8d89f4700" "8b1590df5b00" "5052" "e8fa4f0200"),  # ScreenToClient
    0x448506: bytes.fromhex("89154cb24700"),                  # Alt+Enter: flag = !flag
    0x4487db: bytes.fromhex("33d28955ec"),                    # WNDCLASS.hbrBackground = NULL
}


def patch_display(data, va_to_off, cave_va):
    """Run windowed, borderless, screen-sized, with an aspect-correct picture
    and mouse coordinates mapped back to the 640x432 game screen."""
    for va, orig in DISPLAY_SITES.items():
        off = va_to_off(va)
        if bytes(data[off:off + len(orig)]) != orig:
            sys.exit(f"Display patch site {va:#x} differs; not patching.")

    m = lambda a: struct.pack("<I", a)
    i32 = lambda v: struct.pack("<i", v)

    # Cave D: replaces "push 432; push 640" before SetRect with the screen size.
    cave_d = cave_va
    d = bytearray()
    d += b"\x5e"                                   # pop esi (return address)
    d += b"\x6a\x01\xff\x15" + m(IAT_GETSYSTEMMETRICS)  # GetSystemMetrics(SM_CYSCREEN)
    d += b"\x50"                                   # push eax (bottom)
    d += b"\x6a\x00\xff\x15" + m(IAT_GETSYSTEMMETRICS)  # GetSystemMetrics(SM_CXSCREEN)
    d += b"\x50"                                   # push eax (right)
    d += b"\xff\xe6"                               # jmp esi

    # Cave C: after WM_SIZE/WM_MOVE computes the client rect (screen coords),
    # shrink it to the largest centred 640:432 rectangle.
    cave_c = cave_d + len(d)
    c = bytearray()
    c += b"\x50\x53\x51\x52\x56"                   # push eax/ebx/ecx/edx/esi
    c += b"\x8b\x1d" + m(DEST_R) + b"\x2b\x1d" + m(DEST_L)   # ebx = W
    c += b"\x7e\x00"; j_done1 = len(c) - 1
    c += b"\x8b\x0d" + m(DEST_B) + b"\x2b\x0d" + m(DEST_T)   # ecx = H
    c += b"\x7e\x00"; j_done2 = len(c) - 1
    c += b"\x89\xc8\x69\xc0" + i32(GAME_W) + b"\x99\xbe" + i32(GAME_H) + b"\xf7\xfe"  # eax = H*640/432
    c += b"\x39\xd8"                               # cmp eax, ebx
    c += b"\x7f\x00"; j_letter = len(c) - 1
    c += b"\x89\xda\x29\xc2\xd1\xfa"               # edx = (W - newW) / 2
    c += b"\x01\x15" + m(DEST_L) + b"\x8b\x15" + m(DEST_L) + b"\x01\xc2\x89\x15" + m(DEST_R)
    c += b"\xeb\x00"; j_done3 = len(c) - 1
    letter = len(c)
    c += b"\x89\xd8\x69\xc0" + i32(GAME_H) + b"\x99\xbe" + i32(GAME_W) + b"\xf7\xfe"  # eax = W*432/640
    c += b"\x89\xca\x29\xc2\xd1\xfa"               # edx = (H - newH) / 2
    c += b"\x01\x15" + m(DEST_T) + b"\x8b\x15" + m(DEST_T) + b"\x01\xc2\x89\x15" + m(DEST_B)
    done = len(c)
    c += b"\x5e\x5a\x59\x5b\x58"                   # pop esi/edx/ecx/ebx/eax
    c += b"\xe9" + i32(0x448737 - (cave_c + len(c) + 5))
    for at, target in ((j_done1, done), (j_done2, done), (j_letter, letter), (j_done3, done)):
        assert 0 < target - (at + 1) < 128
        c[at] = target - (at + 1)

    # Cave B: cursor = (screen - rect.origin) * game_size / rect_size, clamped.
    cave_b = cave_c + len(c)
    b = bytearray()
    b += b"\x52\x51"                               # push edx/ecx
    skips = []
    for cur, lo, hi, size in ((CURSOR_X, DEST_L, DEST_R, GAME_W), (CURSOR_Y, DEST_T, DEST_B, GAME_H)):
        b += b"\x8b\x0d" + m(hi) + b"\x2b\x0d" + m(lo)      # ecx = extent
        b += b"\x7e\x00"; skips.append(len(b) - 1)
        b += b"\xa1" + m(cur) + b"\x2b\x05" + m(lo)         # eax = screen - origin
        b += b"\x69\xc0" + i32(size) + b"\x99\xf7\xf9"      # eax = eax*size/extent
        b += b"\x85\xc0\x7d\x02\x31\xc0"                    # if eax < 0: eax = 0
        b += b"\x3d" + i32(size - 1) + b"\x7e\x05\xb8" + i32(size - 1)  # if eax > size-1: clamp
        b += b"\xa3" + m(cur)                               # store
    skip = len(b)
    b += b"\x59\x5a"                               # pop ecx/edx
    b += b"\xe9" + i32(0x43FB73 - (cave_b + len(b) + 5))
    for at in skips:
        assert 0 < skip - (at + 1) < 128
        b[at] = skip - (at + 1)

    blob = d + c + b
    assert (cave_va & 0xFFF) + len(blob) <= 0x200, "caves exceed section raw size"
    blob_off = va_to_off(cave_va)
    data[blob_off:blob_off + len(blob)] = blob

    def put(va, new):
        off = va_to_off(va)
        data[off:off + len(new)] = new

    put(0x446744, bytes.fromhex("81e0ffff00ff" "81c800000080"))  # WS_POPUP, no frame
    put(0x44675f, b"\xe8" + i32(cave_d - (0x44675f + 5)) + b"\x90" * 5)
    put(0x4467c8, bytes.fromhex("6a34"))   # SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED at 0,0
    put(0x446855, bytes.fromhex("6a17"))   # keep window at 0,0 (ignore work area)
    put(0x448258, b"\xe9" + i32(cave_c - (0x448258 + 5)))
    put(0x43fb61, b"\xe9" + i32(cave_b - (0x43fb61 + 5)) + b"\x90" * 13)
    put(0x448506, b"\x90" * 6)             # Alt+Enter no longer switches to exclusive fullscreen
    put(0x4487db, bytes.fromhex("6a098f45ec"))  # hbrBackground = COLOR_WINDOWTEXT+1 (black bars)
    put(FULLSCREEN_FLAG, struct.pack("<I", 0))  # always start windowed (borderless)


def main(argv):
    restore = "--restore" in argv
    args = [a for a in argv if a != "--restore"]
    if len(args) != 1:
        sys.exit(__doc__)
    path = args[0]
    backup = path + ".orig"

    if restore:
        if not os.path.exists(backup):
            sys.exit(f"No backup found at {backup}")
        shutil.copy2(backup, path)
        print(f"Restored original {path}")
        return

    data = bytearray(open(path, "rb").read())
    if SECT_NAME in data[:0x400]:
        print("Already patched - nothing to do.")
        return
    digest = hashlib.sha256(data).hexdigest()
    if digest != ORIG_SHA256:
        sys.exit(
            "This tin3_dxd.exe is not the build this patch was made for.\n"
            f"  expected sha256 {ORIG_SHA256} ({ORIG_SIZE} bytes)\n"
            f"  found    sha256 {digest} ({len(data)} bytes)\n"
            "Nothing was changed. Please open an issue with the hash above.")

    data = patch(data)
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    open(path, "wb").write(data)
    print(f"Patched {path}")
    print(f"  original saved as {backup}")
    print(f"  {NUM_SLOTS} script processes now get {(1 << STACK_SHIFT) // 1024} KB stacks (was ~2 KB)")
    print("  settings now read from HKEY_CURRENT_USER - run setup.ps1 next")


if __name__ == "__main__":
    main(sys.argv[1:])
