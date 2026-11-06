/* selftest.c -- exercises TI-compiled C and the RTS on the F28232 (CHIP_EM interpreter check, no peripherals).
 *
 * Every result goes into results[] as a 64-bit value (low word first); tests/test_c2000_compiled.py recomputes
 * the expected values on the host with C28x C semantics (int 16 bit, long 32, long long 64, float IEEE single;
 * double = 64 bit in EABI, 32 bit in the COFF ABI) and compares them one by one.  Inputs are volatile so the
 * compiler cannot fold the operations: the RTS helpers (16/32/64-bit division, 64-bit multiply and shifts,
 * soft-float add/mul/div/compare/convert, memcpy/memset/strlen/strcmp/qsort/snprintf) really run.
 * `selftest_done` = 0x600D when all results are written.
 * Done after ~4 ms at the 10 MHz reset clock.  Run: chipem run firmware/samples_c2000_c/build/selftest.out --chip tms320f28232 --for 10ms */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "f28232.h"

#define NRES 64
unsigned long long results[NRES];
volatile unsigned int selftest_done;
volatile unsigned int nresults;
char text[40];

volatile int a16 = -12345, b16 = 77;
volatile unsigned int ua16 = 50000u, ub16 = 123u;
volatile long a32 = -1234567890L, b32 = 98765L;
volatile unsigned long ua32 = 4000000000ul, ub32 = 65537ul;
volatile long long a64 = -123456789012345LL, b64 = 987654321LL;
volatile unsigned long long ua64 = 18000000000000000000ull;
volatile float f1 = 3.25f, f2 = -1.5f, f3 = 1.0e-3f;
volatile double d1 = 2.5, d2 = 0.1;
volatile int sel = 5;
volatile unsigned int shift = 13;

static unsigned int n;
static void put(unsigned long long v) { results[n++] = v; }
static unsigned long fbits(float f) { unsigned long u; memcpy(&u, &f, sizeof u); return u; }

static int cmp_int(const void *x, const void *y)
{
    int a = *(const int *)x, b = *(const int *)y;
    return (a > b) - (a < b);
}

static int sw(int k)
{
    switch (k) {                      /* dense: a .switch jump table */
    case 0: return 11;
    case 1: return 22;
    case 2: return 33;
    case 3: return 44;
    case 4: return 55;
    case 5: return 66;
    case 6: return 77;
    case 7: return 88;
    default: return -1;
    }
}

static long fib(int k) { return k < 2 ? k : fib(k - 1) + fib(k - 2); }

struct S { int a; long b; char c[5]; };
static int twice(int x) { return 2 * x; }
static int (*volatile fp)(int) = twice;

void main(void)
{
    EALLOW;
    WDCR = WDCR_DISABLE;
    EDIS;
    n = 0;

    /* 16 bit */
    put((unsigned int)(a16 / b16));               /*  0 */
    put((unsigned int)(a16 % b16));               /*  1 */
    put(ua16 / ub16);                             /*  2 */
    put(ua16 % ub16);                             /*  3 */
    put((unsigned int)(a16 * b16));               /*  4 */
    put((unsigned int)(a16 >> 3));                /*  5 */
    /* 32 bit */
    put((unsigned long)(a32 / b32));              /*  6 */
    put((unsigned long)(a32 % b32));              /*  7 */
    put(ua32 / ub32);                             /*  8 */
    put(ua32 % ub32);                             /*  9 */
    put((unsigned long)(a32 * b32));              /* 10 */
    put(ua32 >> shift);                           /* 11 */
    put((unsigned long)(a32 >> shift));           /* 12 */
    put(ua32 << shift);                           /* 13 */
    put((unsigned long)((long)a16 * b32));        /* 14 */
    /* 64 bit */
    put((unsigned long long)(a64 / b64));         /* 15 */
    put((unsigned long long)(a64 % b64));         /* 16 */
    put(ua64 / ub32);                             /* 17 */
    put(ua64 % ub32);                             /* 18 */
    put((unsigned long long)(a64 * b64));         /* 19 */
    put((unsigned long long)(a64 >> shift));      /* 20 */
    put(ua64 >> shift);                           /* 21 */
    put(ua64 << shift);                           /* 22 */
    put(a64 < b64);                               /* 23 */
    put(ua64 > (unsigned long long)b64);          /* 24 */
    /* float (IEEE single, soft-float RTS on the F28232) */
    put(fbits(f1 * f2));                          /* 25 */
    put(fbits(f1 + f2));                          /* 26 */
    put(fbits(f1 / f2));                          /* 27 */
    put(fbits(f1 - f3));                          /* 28 */
    put((unsigned long)(long)(f1 * 1000.0f));     /* 29 */
    put(fbits((float)a32));                       /* 30 */
    put(fbits((float)ua32));                      /* 31 */
    put((unsigned long)(long)(f2 * 100.0f));      /* 32 */
    put(f1 > f2);                                 /* 33 */
    put(fbits((float)(d1 * d2)));                 /* 34 (double: 64 bit in EABI, 32 bit in COFF) */
    put((unsigned long)(long)(d1 * 1000.0 + d2)); /* 35 */
    /* library and language */
    {
        static const char src[] = "hello, c28x";
        char buf[16];
        struct S s1 = {7, 123456L, "abcd"}, s2;
        int arr[7] = {5, -3, 9, 0, 2, -8, 4};
        memset(buf, 0, sizeof buf);
        memcpy(buf, src, sizeof src);
        put(strlen(buf));                         /* 36 */
        put((unsigned int)strcmp(buf, "hello") > 0); /* 37 */
        put(strcmp(buf, src) == 0);               /* 38 */
        s2 = s1;
        put((unsigned long)s2.b + s2.a + s2.c[3]);/* 39 */
        put((unsigned int)sw(sel));               /* 40 */
        put((unsigned int)sw(sel + 9));           /* 41 */
        put((unsigned long)fib(15));              /* 42 */
        put((unsigned int)fp(21));                /* 43 */
        qsort(arr, 7, sizeof arr[0], cmp_int);
        put(((unsigned long long)(unsigned int)arr[0] << 48) | ((unsigned long long)(unsigned int)arr[3] << 32)
            | ((unsigned long)(unsigned int)arr[5] << 16) | (unsigned int)arr[6]);   /* 44 */
        put((unsigned long)atol("-98765"));       /* 45 */
        put((unsigned int)snprintf(text, sizeof text, "%d|%ld|%u|%x", a16, a32, ua16, 0xBEEF));  /* 46 */
    }
    nresults = n;
    selftest_done = 0x600D;
    for (;;) { }
}
