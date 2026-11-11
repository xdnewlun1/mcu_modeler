/* models.c - the same computation built for SDCC's memory models (build.sh: small, medium, large, and small/large
 * with --stack-auto): recursive and reentrant functions, struct copies, generic pointers to every space, an array
 * in the model's default space (data / pdata / xdata), a function pointer table.  No SFRs are used, so the image is
 * also run on ucsim's 8052 by the differential tests.  Results in `res`, `done` = 0xA5 at the end; `finish()` is the
 * final self-loop (tests stop there). */
#include <string.h>

struct pt { int x; int y; char tag; };

struct results {
    unsigned int fib;          /* fib(15) = 610, recursive */
    unsigned long fact;        /* 12! = 479001600, reentrant */
    int dot;                   /* struct copy + dot product */
    unsigned int crc;          /* CRC-16/CCITT of "123456789" = 0x29B1 */
    unsigned char sorted_ok;
    unsigned char ptr_sum;     /* generic pointers into data, idata, xdata/pdata, code */
    unsigned int fsum;         /* function pointer table */
} res;
volatile unsigned char done;

static unsigned int fib(unsigned char n) __reentrant
{
    return n < 2 ? n : fib(n - 1) + fib(n - 2);
}

static unsigned long fact(unsigned char n) __reentrant
{
    return n ? n * fact(n - 1) : 1;
}

static int dot(struct pt a, struct pt b)
{
    return a.x * b.x + a.y * b.y;
}

static unsigned int crc16(const char *p, unsigned char n)
{
    unsigned int crc = 0xFFFF;
    unsigned char i;
    while (n--) {
        crc ^= (unsigned int)(unsigned char)*p++ << 8;
        for (i = 0; i < 8; i++)
            crc = crc & 0x8000 ? (crc << 1) ^ 0x1021 : crc << 1;
    }
    return crc;
}

static unsigned int f_add(unsigned int a) { return a + 7; }
static unsigned int f_mul(unsigned int a) { return a * 3; }
static unsigned int f_neg(unsigned int a) { return ~a; }
static unsigned int (* const ftab[3])(unsigned int) = { f_add, f_mul, f_neg };

static unsigned char arr[24];               /* default space of the model */
__data unsigned char d1 = 1;
__idata unsigned char i2 = 2;
__xdata unsigned char x3 = 3;
__code const unsigned char c4 = 4;

void finish(void)
{
    for (;;)
        ;
}

void main(void)
{
    struct pt a = { 3, -4, 'a' }, b;
    unsigned char i, j, t;
    unsigned char *gp[4];
    unsigned int v;

    res.fib = fib(15);
    res.fact = fact(12);
    b = a;
    b.x = 5;
    res.dot = dot(a, b);
    res.crc = crc16("123456789", 9);
    for (i = 0; i < sizeof arr; i++)
        arr[i] = (unsigned char)(i * 37 + 11);
    for (i = 0; i < sizeof arr; i++)              /* bubble sort */
        for (j = 0; j + 1 < sizeof arr - i; j++)
            if (arr[j] > arr[j + 1]) {
                t = arr[j];
                arr[j] = arr[j + 1];
                arr[j + 1] = t;
            }
    res.sorted_ok = 1;
    for (i = 0; i + 1 < sizeof arr; i++)
        if (arr[i] > arr[i + 1])
            res.sorted_ok = 0;
    gp[0] = &d1;
    gp[1] = &i2;
    gp[2] = &x3;
    gp[3] = (unsigned char *)&c4;
    res.ptr_sum = *gp[0] + *gp[1] + *gp[2] + *gp[3];
    v = 1000;
    for (i = 0; i < 3; i++)
        v = ftab[i](v);
    res.fsum = v;
    done = 0xA5;
    finish();
}
