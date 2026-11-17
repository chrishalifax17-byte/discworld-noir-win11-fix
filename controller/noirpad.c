/*
 * noirpad - native XInput (Xbox controller) support for Discworld Noir.
 *
 * Freestanding 32-bit code linked at a fixed address inside the .noirstk
 * section that patch_noir.py adds to tin3_dxd.exe.  It is called from a hook
 * on the game's PeekMessageA import thunk, so it runs in every message loop
 * (menus, gameplay and video playback).
 *
 * It only uses functions the game already imports (called through the game's
 * import address table) plus XInput, which it loads at runtime.
 *
 *   Left stick ........ move cursor (analogue, accelerating)
 *   D-pad ............. move cursor slowly (precise)
 *   Left trigger ...... hold for slow cursor
 *   A / Right trigger . left click   (walk / look / talk / select)
 *   B ................. right click  (notebook / cancel)
 *   X ................. double click (run)
 *   Start / Back ...... Esc          (game menu / skip)
 *
 * Build: see build.sh (produces noirpad_blob.py used by patch_noir.py).
 */

typedef unsigned int   u32;
typedef unsigned short u16;
typedef short          s16;
typedef unsigned char  u8;

typedef struct { u32 packet; u16 buttons; u8 lt, rt; s16 lx, ly, rx, ry; } XSTATE;
typedef struct { int x, y; } POINT32;

/* Game import address table entries (tin3_dxd.exe, image base 0x400000). */
typedef void *(__stdcall *GetModuleHandleA_t)(const char *);
typedef void *(__stdcall *GetProcAddress_t)(void *, const char *);
typedef void *(__stdcall *LoadLibraryA_t)(const char *);
typedef int   (__stdcall *GetCursorPos_t)(POINT32 *);
typedef int   (__stdcall *SetCursorPos_t)(int, int);
typedef int   (__stdcall *PostMessageA_t)(void *, u32, u32, u32);
typedef void *(__stdcall *GetForegroundWindow_t)(void);
typedef u32   (__stdcall *timeGetTime_t)(void);

#define IAT(addr, type) (*(type *)(addr))
#define pGetModuleHandleA    IAT(0x5CA3D0, GetModuleHandleA_t)
#define pGetProcAddress      IAT(0x5CA3C4, GetProcAddress_t)
#define pGetCursorPos        IAT(0x5CA46C, GetCursorPos_t)
#define pSetCursorPos        IAT(0x5CA4B8, SetCursorPos_t)
#define pPostMessageA        IAT(0x5CA4E8, PostMessageA_t)
#define pGetForegroundWindow IAT(0x5CA458, GetForegroundWindow_t)
#define pTimeGetTime         IAT(0x5CA424, timeGetTime_t)

/* Game globals. */
#define GAME_HWND (*(void *volatile *)0x5BDF90)
#define DEST_L    (*(volatile int *)0x5BDF7C)   /* picture rectangle, screen coords */
#define DEST_T    (*(volatile int *)0x5BDF80)
#define DEST_R    (*(volatile int *)0x5BDF84)
#define DEST_B    (*(volatile int *)0x5BDF88)

typedef u32 (__stdcall *XInputGetState_t)(u32, XSTATE *);

/* Persistent state in the zero-filled tail of the .noirstk section's first page. */
struct padstate {
    int inited;
    XInputGetState_t get;
    u32 last_ms;
    u32 retry_ms;
    u32 slot;
    int connected;
    u16 prev;
    u8 prev_rt;
    int acc_x, acc_y;
};
#define ST ((struct padstate *)0x5D6F00)

#define XB_DUP    0x0001
#define XB_DDOWN  0x0002
#define XB_DLEFT  0x0004
#define XB_DRIGHT 0x0008
#define XB_START  0x0010
#define XB_BACK   0x0020
#define XB_A      0x1000
#define XB_B      0x2000
#define XB_X      0x4000

#define WM_KEYDOWN      0x100
#define WM_KEYUP        0x101
#define WM_LBUTTONDOWN  0x201
#define WM_LBUTTONUP    0x202
#define WM_LBUTTONDBLCLK 0x203
#define WM_RBUTTONDOWN  0x204
#define WM_RBUTTONUP    0x205

static void init(struct padstate *s)
{
    static const char *const dlls[] = { "xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll" };
    void *k32 = pGetModuleHandleA("kernel32.dll");
    LoadLibraryA_t load = (LoadLibraryA_t)pGetProcAddress(k32, "LoadLibraryA");
    s->inited = 1;
    s->last_ms = pTimeGetTime();
    if (!load)
        return;
    for (int i = 0; i < 3 && !s->get; i++) {
        void *x = load(dlls[i]);
        if (x)
            s->get = (XInputGetState_t)pGetProcAddress(x, "XInputGetState");
    }
}

static void post(u32 msg, u32 wp, u32 lp)
{
    pPostMessageA(GAME_HWND, msg, wp, lp);
}

static u32 cursor_lparam(void)
{
    POINT32 p;
    pGetCursorPos(&p);
    return ((u32)(p.y & 0xFFFF) << 16) | (u32)(p.x & 0xFFFF);
}

static void key(u32 vk)
{
    post(WM_KEYDOWN, vk, 0x00010001u);
    post(WM_KEYUP, vk, 0xC0010001u);
}

/* Stick value -> signed speed 0..1000 with deadzone and a squared curve. */
static int curve(int v)
{
    const int dz = 7849;
    int n = v < 0 ? -v : v;
    if (n <= dz)
        return 0;
    if (n > 32767)
        n = 32767;
    n = (n - dz) * 1000 / (32767 - dz);
    n = n * n / 1000;
    return v < 0 ? -n : n;
}

static int clampi(int v, int lo, int hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

void __cdecl pad_poll(void)
{
    struct padstate *s = ST;
    XSTATE st;

    if (!s->inited)
        init(s);
    if (!s->get)
        return;

    u32 now = pTimeGetTime();
    u32 dt = now - s->last_ms;
    if (dt == 0)
        return;               /* PeekMessage is called many times per ms */
    s->last_ms = now;
    if (dt > 50)
        dt = 50;

    /* Polling a disconnected pad is slow; only search for one once a second. */
    if (!s->connected && (int)(now - s->retry_ms) < 1000)
        return;
    if (s->get(s->slot, &st) != 0) {
        s->connected = 0;
        s->retry_ms = now;
        s->slot = (s->slot + 1) & 3;
        s->prev = 0;
        s->prev_rt = 0;
        return;
    }
    s->connected = 1;

    if (pGetForegroundWindow() != GAME_HWND) {
        s->prev = st.buttons;
        s->prev_rt = st.rt > 100;
        return;
    }

    /* ---- cursor movement ---- */
    int h = DEST_B - DEST_T;
    if (h <= 0)
        h = 432;
    int vx = curve(st.lx), vy = -curve(st.ly);
    if (st.buttons & XB_DLEFT)  vx -= 200;
    if (st.buttons & XB_DRIGHT) vx += 200;
    if (st.buttons & XB_DUP)    vy -= 200;
    if (st.buttons & XB_DDOWN)  vy += 200;
    if (st.lt > 100) {             /* precision mode */
        vx = vx * 35 / 100;
        vy = vy * 35 / 100;
    }
    /* Full deflection crosses the picture height in about one second. */
    s->acc_x += vx * h / 1000 * (int)dt;
    s->acc_y += vy * h / 1000 * (int)dt;
    int dx = s->acc_x / 1000, dy = s->acc_y / 1000;
    s->acc_x -= dx * 1000;
    s->acc_y -= dy * 1000;
    if (!vx) s->acc_x = 0;
    if (!vy) s->acc_y = 0;
    if (dx || dy) {
        POINT32 p;
        pGetCursorPos(&p);
        int l = DEST_L, t = DEST_T, r = DEST_R, b = DEST_B;
        if (r <= l || b <= t) { l = 0; t = 0; r = 640; b = 432; }
        pSetCursorPos(clampi(p.x + dx, l, r - 1), clampi(p.y + dy, t, b - 1));
    }

    /* ---- buttons ---- */
    u16 down = st.buttons & ~s->prev;
    u16 up = s->prev & ~st.buttons;
    int rt = st.rt > 100;
    u32 lp = cursor_lparam();

    if ((down & XB_A) || (rt && !s->prev_rt))
        post(WM_LBUTTONDOWN, 1, lp);
    if ((up & XB_A) || (!rt && s->prev_rt))
        post(WM_LBUTTONUP, 0, lp);
    if (down & XB_B)
        post(WM_RBUTTONDOWN, 2, lp);
    if (up & XB_B)
        post(WM_RBUTTONUP, 0, lp);
    if (down & XB_X) {
        post(WM_LBUTTONDOWN, 1, lp);
        post(WM_LBUTTONUP, 0, lp);
        post(WM_LBUTTONDBLCLK, 1, lp);
        post(WM_LBUTTONUP, 0, lp);
    }
    if (down & (XB_START | XB_BACK))
        key(0x1B);

    s->prev = st.buttons;
    s->prev_rt = (u8)rt;
}

/* Entry point at the start of the blob: called in place of PeekMessageA's
 * import thunk.  Preserves all registers, then tail-jumps to PeekMessageA. */
__asm__(
    ".section .text$a,\"x\"\n"
    ".globl _pad_hook\n"
    "_pad_hook:\n"
    "    pushal\n"
    "    pushfl\n"
    "    cld\n"
    "    call _pad_poll\n"
    "    popfl\n"
    "    popal\n"
    "    jmp *0x5CA4F0\n");
