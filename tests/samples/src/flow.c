// v4.0 fixtures: jumps and calls through encoded tables of code pointers (Hikari-style indirect branches
// and calls), an identity wrapper, and the shapes D-810 must leave alone. Tables are writable statics, as
// obfuscators emit them. KEEP hands a table's address to an empty asm that may write memory: otherwise
// clang's globalopt makes a never-written static table constant at -O2. A C initializer can only add or
// subtract a constant from an address, so the decodes are ADD, SUB, a key global and an MBA form of SUB.
#include <stdint.h>
#define B(v) __asm__ volatile("" : "+r"(v))
#define KEEP(p) __asm__ volatile("" : : "r"(p) : "memory")
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#define EXPORT_DATA __declspec(dllexport)
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#define EXPORT_DATA __attribute__((visibility("default")))
#endif
#define KEY 0x13371337
#define ENC(p) ((intptr_t)(p) + KEY)
typedef intptr_t (*fn_t)(intptr_t);

EXPORT intptr_t f_a(intptr_t x) { B(x); return x + 0x100; }
EXPORT intptr_t f_b(intptr_t x) { B(x); return x * 7; }
EXPORT void sink_ptr(void *p) { B(p); }

// ---- Jumps through code-pointer tables (labels as values) ----

EXPORT int tbl_jump_const(int x) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1)};
    KEEP(tbl);
    goto *(void *)(tbl[1] - KEY);
L0: return x ^ 0xa0;
L1: return x ^ 0xa1;
}

EXPORT int tbl_jump_sub(int x) {
    static intptr_t tbl[] = {(intptr_t)&&L0 - KEY, (intptr_t)&&L1 - KEY};
    KEEP(tbl);
    goto *(void *)(tbl[1] + KEY);
L0: return x ^ 0xa0;
L1: return x ^ 0xa1;
}

EXPORT int tbl_jump_var(int x) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1), ENC(&&L2), ENC(&&L3)};
    KEEP(tbl);
    goto *(void *)(tbl[x & 3] - KEY);
L0: return x ^ 0xa0;
L1: return x ^ 0xa1;
L2: return x ^ 0xa2;
L3: return x ^ 0xa3;
}

static intptr_t jump_key = KEY;  // never written
EXPORT int tbl_jump_keyglobal(int x) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1)};
    KEEP(tbl);
    KEEP(&jump_key);
    goto *(void *)(tbl[1] - jump_key);
L0: return x ^ 0xa0;
L1: return x ^ 0xa1;
}

EXPORT int tbl_jump_mbadecode(int x) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1)};
    KEEP(tbl);
    intptr_t t = tbl[1], k = -KEY;
    B(k);
    goto *(void *)((t ^ k) + 2 * (t & k));  // t + k, that is t - KEY
L0: return x ^ 0xa0;
L1: return x ^ 0xa1;
}

static uint32_t index_key = 0x51;  // never written; the index is computed from it
EXPORT int tbl_jump_keyindex(int x) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1)};
    KEEP(tbl);
    KEEP(&index_key);
    goto *(void *)(tbl[(int32_t)(0x52 - index_key)] - KEY);
L0: return x ^ 0xa0;
L1: return x ^ 0xa1;
}

EXPORT int mba_jump(int x, int y) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1)};
    KEEP(tbl);
    int a = x, b = y;
    B(a);
    B(b);
    int i = (a ^ b) + 2 * (a & b) - (a + b) + 1;  // always 1
    goto *(void *)(tbl[i] - KEY);
L0: return x ^ 0xa0;
L1: return x ^ 0xa1;
}

// ---- Calls through code-pointer tables ----

static intptr_t ctab[] = {ENC(f_a), ENC(f_b)};
EXPORT intptr_t tbl_call_const(intptr_t x) { KEEP(ctab); return ((fn_t)(ctab[1] - KEY))(x); }
EXPORT intptr_t tbl_call_shared(intptr_t x) { KEEP(ctab); return ((fn_t)(ctab[1] - KEY))(x) + 1; }

static intptr_t stab[] = {(intptr_t)f_a - KEY, (intptr_t)f_b - KEY};
EXPORT intptr_t tbl_call_sub(intptr_t x) { KEEP(stab); return ((fn_t)(stab[0] + KEY))(x) + 2; }

static intptr_t utab[] = {ENC(f_a), ENC(f_b)};
EXPORT intptr_t tbl_call_user(intptr_t x) { KEEP(utab); return ((fn_t)(utab[1] - KEY))(x) + 3; }

static intptr_t mtab[] = {ENC(f_a), ENC(f_b)};
EXPORT intptr_t mba_call(intptr_t x, int y) {
    KEEP(mtab);
    int a = (int)x, b = y;
    B(a);
    B(b);
    int i = (a ^ b) + 2 * (a & b) - (a + b) + 1;  // always 1
    return ((fn_t)(mtab[i] - KEY))(x);
}

static fn_t rtab[] = {f_a, f_b};  // plain function pointers, indexed from the key global
EXPORT intptr_t tbl_call_keyindex(intptr_t x) {
    KEEP(rtab);
    KEEP(&index_key);
    return rtab[(int32_t)(0x52 - index_key)](x);
}

// ---- An identity wrapper over a one-entry code pointer ----

EXPORT void *ident(void *p) { B(p); return p; }
static void *off_fa = (void *)f_a;
EXPORT intptr_t ident_call(intptr_t x) { KEEP(&off_fa); return ((fn_t)ident(off_fa))(x) + 4; }

// ---- Must stay as they are ----

static intptr_t wtab[] = {ENC(f_a), ENC(f_b)};
EXPORT intptr_t written_call(intptr_t x) { KEEP(wtab); return ((fn_t)(wtab[1] - KEY))(x) + 5; }
EXPORT void written_store(intptr_t v) { KEEP(wtab); wtab[1] = v; }

EXPORT int written_table(int x) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1)};
    KEEP(tbl);
    if (x == 99)
        tbl[1] = ENC(&&L0);
    goto *(void *)(tbl[1] - KEY);
L0: return x ^ 0xb0;
L1: return x ^ 0xb1;
}

static intptr_t etab[] = {ENC(f_a), ENC(f_b)};
EXPORT intptr_t escaped_call(intptr_t x) { sink_ptr(etab); return ((fn_t)(etab[1] - KEY))(x) + 6; }

EXPORT int escaped_table(int x) {
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1)};
    sink_ptr(tbl);
    goto *(void *)(tbl[1] - KEY);
L0: return x ^ 0xc0;
L1: return x ^ 0xc1;
}

static const char not_code[16] = "data, not code";
EXPORT int bad_entry_table(int x) {  // two labels: with one, clang jumps to it directly
    static intptr_t tbl[] = {ENC(&&L0), ENC(&&L1), ENC(not_code), ENC(&&L0)};
    KEEP(tbl);
    goto *(void *)(tbl[x & 3] - KEY);
L0: return x ^ 0xd0;
L1: return x ^ 0xd1;
}

static uint32_t config_flag = 7;  // never written; read in a condition, not as a code pointer
EXPORT int config_test(int x) {
    KEEP(&config_flag);
    if (config_flag == 7)
        return x + 1;
    return x - 1;
}

EXPORT_DATA uint32_t wrapper_effect;
EXPORT void *not_ident(void *p) { B(p); wrapper_effect = 1; return p; }
static void *off_fb = (void *)f_b;
EXPORT intptr_t not_identity_call(intptr_t x) { KEEP(&off_fb); return ((fn_t)not_ident(off_fb))(x) + 7; }
