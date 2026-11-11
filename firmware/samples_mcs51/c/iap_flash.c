/* iap_flash.c - In-Application Programming from C through the boot ROM entry PGM_MTP (FFF0h, data sheet sec. 6.3.8,
 * Table 12): read the manufacturer ID, erase the 128-byte page at E000h, program "IAP from C" byte by byte, read it
 * back with IAP read and through a __code pointer (MOVC), print it.  AUXR1.ENBOOT maps the boot block.
 * `iap()` is a naked assembler wrapper: R1 = function, DPTR = address, ACC = data, LCALL 0FFF0h, result in ACC. */
#include <stdio.h>
#include <string.h>
#include "uart.h"

#define PAGE 0xE000
static const char text[] = "IAP from C";
__data unsigned char iap_fn, iap_data;
__data unsigned int iap_addr;
unsigned char result, errors, mfr;
char msg[sizeof text];

static unsigned char iap(void) __naked
{
    __asm
        mov  r1, _iap_fn
        mov  dpl, _iap_addr
        mov  dph, (_iap_addr + 1)
        mov  a, _iap_data
        lcall 0xfff0
        mov  dpl, a              ; SDCC returns a char in DPL
        ret
    __endasm;
}

static unsigned char iap_call(unsigned char fn, unsigned int addr, unsigned char d)
{
    iap_fn = fn;
    iap_addr = addr;
    iap_data = d;
    return iap();
}

void main(void)
{
    unsigned char i;
    __code const char *flash = (__code const char *)PAGE;
    uart_init();
    AUXR1 |= AUXR1_ENBOOT;
    mfr = iap_call(0x00, 0x0000, 0);
    errors = iap_call(0x08, PAGE, 0);                        /* erase page */
    for (i = 0; i < sizeof text; i++)
        errors |= iap_call(0x02, PAGE + i, text[i]);         /* program byte */
    result = 0x55;
    for (i = 0; i < sizeof text; i++)
        if (iap_call(0x03, PAGE + i, 0) != text[i] || flash[i] != text[i])
            result = 0xEE;
    AUXR1 &= ~AUXR1_ENBOOT;
    /* copy through the __code pointer: SDCC 4.5 passes a __code pointer cast to a generic one wrongly through
     * printf's varargs (ucsim shows the same output), so the string is printed from RAM */
    for (i = 0; i < sizeof text; i++)
        msg[i] = flash[i];
    printf("%s mfr %02X err %02X %s\r\n", msg, (unsigned int)mfr, (unsigned int)errors,
           result == 0x55 ? "PASS" : "FAIL");
    uart_flush();
    for (;;)
        PCON |= PCON_IDL;
}
