/* f28335_fpu.c -- FPU32 check on the TMS320F28335 (CHIP_EM interpreter vs IEEE single precision, no peripherals).
 *
 * Built with --float_support=fpu32: float add/sub/mul, conversions, compares (CMPF32 + MOVST0), MINF32/MAXF32,
 * ABSF32 and the dot-product / FIR loops (RPTB + RPT || MACF32) are FPU instructions; float division and sqrtf go
 * through the FPU32 run-time library.  f28335_fpu_asm.asm adds STF.RND32 = 0 truncation, the LVF/LUF flags (STF and
 * MOVST0) and an RPTB with an immediate count.  Every result goes into results[] as a 32-bit word (float bits or
 * integer); tests/test_c2000_f2833x_fpu.py recomputes them on the host with IEEE single precision (Python struct
 * round trips; round to nearest even, which TI's _c_int00 selects with SETFLG RNDF32=1) and compares one by one.
 * Inputs are volatile so the compiler cannot fold the operations.  `fpu_done` = 0x600D when all results are written.
 * Run: chipem run firmware/samples_c2000_c/build/f28335_fpu.out --chip tms320f28335 --for 2ms */
#include <math.h>
#include "f28335.h"

#define NRES 80
unsigned long results[NRES];
volatile unsigned int fpu_done;
volatile unsigned int nresults;

volatile float f1 = 3.25f, f2 = -1.5f, f3 = 1.0e-3f, f4 = 0.1f, f5 = 7.0f / 3.0f;
volatile float big = 3.0e38f, tiny = 1.0e-30f, three_e9 = 3.0e9f, neg = -2.75f, two = 2.0f;
volatile long l1 = -1234567890L, l2 = 16777217L, l3 = 16777219L;
volatile unsigned long u1 = 4000000000ul;
volatile int i1 = -321;
volatile int nsel[6] = {1, 2, 5, 16, 31, 0};

float xs[40], ys[40], coef[8], firout[32];
const float poly[6] = {0.5f, -1.25f, 2.0f, 0.375f, -3.0f, 1.0f};

extern float fpu_mul_trunc(float a, float b);
extern unsigned long fpu_stf_after_mul(float a, float b);
extern unsigned long fpu_st0_after_mul(float a, float b);
extern float fpu_poly_rptb(const float *c, float x);

static unsigned int n;
static void put(unsigned long v) { results[n++] = v; }
static unsigned long fb(float f) { union { float f; unsigned long u; } c; c.f = f; return c.u; }

float dot(const float *a, const float *b, int k)
{
    float s = 0.0f;
    int i;
    for (i = 0; i < k; i++)
        s += a[i] * b[i];
    return s;
}

void fir8(float *o, const float *in, int k)          /* RPTB over the outputs, RPT || MACF32 over the 8 taps */
{
    int i, j;
    for (i = 0; i < k; i++) {
        float s = 0.0f;
        for (j = 0; j < 8; j++)
            s += coef[j] * in[i + j];
        o[i] = s;
    }
}

static float maxf(float a, float b) { return a > b ? a : b; }
static float minf(float a, float b) { return a < b ? a : b; }

static float horner(const float *c, float x)
{
    float acc = c[0];
    int i;
    for (i = 1; i < 6; i++)
        acc = acc * x + c[i];
    return acc;
}

void main(void)
{
    int i;
    float s;

    n = 0;
    fpu_done = 0;
    /* 0-7: arithmetic, run-time library division and square root */
    put(fb(f1 * f2));
    put(fb(f1 + f2));
    put(fb(f1 - f3));
    put(fb(f1 / f2));                   /* FS$$DIV (SUBCU quotient, same code as the soft-float RTS) */
    put(fb(f5 * f4 + f3));
    put(fb(sqrtf(two)));
    put(fb(fabsf(neg)));
    put(fb(-f4));
    /* 8-15: conversions */
    put((unsigned long)(long)(f1 * 1000.0f));
    put((unsigned long)(long)(neg * 100.7f));           /* F32TOI32 truncates toward zero */
    put((unsigned long)three_e9);                       /* F32TOUI32 */
    put(fb((float)l1));                                 /* I32TOF32 rounds (RNDF32 = 1) */
    put(fb((float)l2));                                 /* 2^24 + 1: tie, rounds to even */
    put(fb((float)l3));                                 /* 2^24 + 3: tie, rounds up to even */
    put(fb((float)u1));                                 /* UI32TOF32 */
    put((unsigned long)(int)(f2 * 3.0f) & 0xFFFFul);    /* F32TOI16 */
    /* 16-23: compares (CMPF32 + MOVST0 ZF,NF), MINF32/MAXF32, int16 to float */
    put(f1 < f2);
    put(f1 >= f2);
    put(f2 == f2);
    put(f3 != f3);
    put(fb(maxf(f1, f2)));
    put(fb(minf(f1, f2)));
    put(fb((float)i1));                                 /* I16TOF32 */
    put(fb(f5));
    /* 24-29: rounding mode, overflow / underflow flags (STF bit 0 LVF, bit 1 LUF), MOVST0 -> ST0.V (bit 6) */
    put(fb(fpu_mul_trunc(f5, f4)));                     /* RND32 = 0: truncated product */
    put(fb(f5 * f4));                                   /* RND32 = 1: rounded product */
    put(fb(big * big));                                 /* overflow: +inf */
    put(fpu_stf_after_mul(big, big));
    put(fpu_stf_after_mul(tiny, tiny));                 /* underflow: +0, LUF */
    put(fpu_st0_after_mul(big, -big));
    /* 30-35: RPTB dot products (RC from AR6, n - 1 = 0 .. 30), exact data so any summation order gives the result */
    for (i = 0; i < 40; i++) {
        xs[i] = (float)(i % 7) - 3.0f;
        ys[i] = 0.5f * (float)(i % 5) + 0.25f;
    }
    for (i = 0; i < 6; i++)
        put(fb(nsel[i] ? dot(xs, ys, nsel[i]) : 0.0f));
    /* 36-41: FIR, 8 taps over 32 outputs; outputs 0, 7, 31 and their sum */
    for (i = 0; i < 8; i++)
        coef[i] = (float)(i + 1) * 0.125f;
    fir8(firout, xs, 32);
    put(fb(firout[0]));
    put(fb(firout[7]));
    put(fb(firout[31]));
    s = 0.0f;
    for (i = 0; i < 32; i++)
        s += firout[i];
    put(fb(s));
    /* 40-43: Horner (inexact, every step rounded) in C and with the asm RPTB #4 routine */
    put(fb(horner(poly, f5)));
    put(fb(fpu_poly_rptb(poly, f5)));
    put(fb(horner(poly, f4)));
    put(fb(fpu_poly_rptb(poly, f4)));

    nresults = n;
    fpu_done = 0x600D;
    for (;;) { }
}
