/* Minimal ARM EABI run-time helpers for the clang/lld build (no libgcc / compiler-rt / libc in this checkout):
 * clang lowers struct zeroing to __aeabi_memclr4 and the HAL's 64-bit baud/PLL arithmetic to __aeabi_uldivmod. */
#include <stddef.h>
#include <stdint.h>

void *memset(void *d, int c, size_t n);
void *memcpy(void *d, const void *s, size_t n);

void __aeabi_memclr(void *d, size_t n) { memset(d, 0, n); }
void __aeabi_memclr4(void *d, size_t n) __attribute__((alias("__aeabi_memclr")));
void __aeabi_memclr8(void *d, size_t n) __attribute__((alias("__aeabi_memclr")));
void __aeabi_memset(void *d, size_t n, int c) { memset(d, c, n); }
void __aeabi_memset4(void *d, size_t n, int c) __attribute__((alias("__aeabi_memset")));
void __aeabi_memset8(void *d, size_t n, int c) __attribute__((alias("__aeabi_memset")));
void __aeabi_memcpy(void *d, const void *s, size_t n) { memcpy(d, s, n); }
void __aeabi_memcpy4(void *d, const void *s, size_t n) __attribute__((alias("__aeabi_memcpy")));
void __aeabi_memcpy8(void *d, const void *s, size_t n) __attribute__((alias("__aeabi_memcpy")));

/* Restoring shift-subtract division; d == 0 returns quotient ~0 and remainder n. */
uint64_t __udivmoddi4(uint64_t n, uint64_t d, uint64_t *rem)
{
    uint64_t q = 0, r = 0;
    if (d == 0) {
        if (rem)
            *rem = n;
        return ~(uint64_t)0;
    }
    for (int i = 63; i >= 0; i--) {
        r = (r << 1) | ((n >> i) & 1u);
        if (r >= d) {
            r -= d;
            q |= (uint64_t)1 << i;
        }
    }
    if (rem)
        *rem = r;
    return q;
}

/* {r0:r1 = n / d, r2:r3 = n % d} for n in r0:r1, d in r2:r3 (RTABI 4.2). */
__attribute__((naked)) void __aeabi_uldivmod(void)
{
    __asm volatile(
        "push {r4, lr}\n"
        "sub  sp, sp, #16\n"
        "add  r4, sp, #8\n"
        "str  r4, [sp]\n"            /* 5th argument: &rem */
        "bl   __udivmoddi4\n"
        "ldr  r2, [sp, #8]\n"
        "ldr  r3, [sp, #12]\n"
        "add  sp, sp, #16\n"
        "pop  {r4, pc}\n");
}
