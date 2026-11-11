/* math.c - long and float arithmetic through the SDCC runtime library (_mullong, _divslong, _modulong, _fsadd,
 * _fsmul, _fsdiv, sqrtf, sinf, expf, logf, conversions).  Results as raw 32-bit words in `out[]` (IEEE 754 single
 * for floats), compared with the host by tests/test_mcs51_sdcc.py.  No SFRs (also run on ucsim's 8052). */
#include <math.h>

#define N 20
union w { unsigned long u; long l; float f; };
__xdata union w out[N];
volatile unsigned char done;
volatile long la = 123456789L, lb = -9876L;
volatile float fa = 3.14159265f, fb = -2.5f;

void finish(void)
{
    for (;;)
        ;
}

void main(void)
{
    out[0].l = la * lb;
    out[1].l = la / lb;
    out[2].l = la % lb;
    out[3].u = (unsigned long)la / 1000u;
    out[4].u = 0xDEADBEEFul >> 7;
    out[5].l = -la >> 3;
    out[6].f = fa + fb;
    out[7].f = fa - fb;
    out[8].f = fa * fb;
    out[9].f = fa / fb;
    out[10].f = sqrtf(fa);
    out[11].f = sinf(fa / 6.0f);
    out[12].f = expf(1.0f);
    out[13].f = logf(10.0f);
    out[14].l = (long)(fa * 1000.0f);
    out[15].f = (float)la;
    out[16].f = fabsf(fb) * 0.1f;
    out[17].f = atanf(1.0f) * 4.0f;
    out[18].u = (unsigned long)(65535.0f * 65537.0f);
    out[19].f = powf(2.0f, 10.0f);
    done = 0xA5;
    finish();
}
