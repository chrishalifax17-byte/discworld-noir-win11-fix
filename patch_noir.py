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

    raw_size = align(len(code), file_align)
    raw_ptr = align(len(data), file_align)
    data += b"\0" * (raw_ptr - len(data))
    data += code + b"\0" * (raw_size - len(code))

    struct.pack_into("<8sIIIIIIHHI", data, new_hdr, SECT_NAME, vsize, new_rva,
                     raw_size, raw_ptr, 0, 0, 0, 0, 0xE0000060)
    struct.pack_into("<H", data, pe + 6, nsec + 1)
    struct.pack_into("<I", data, opt + 56, align(new_rva + vsize, sect_align))  # SizeOfImage

    rel = cave_va - (PATCH_VA + 5)
    data[poff:poff + 12] = b"\xe9" + struct.pack("<i", rel) + b"\x90" * 7

    # --- Fix 2: HKLM -> HKCU ---------------------------------------------
    for i in hits:
        data[i + 1] = 0x01

    return data


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
