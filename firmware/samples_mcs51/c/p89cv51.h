/* p89cv51.h - SFRs and SFR bits of the NXP P89CV51RB2/RC2/RD2 for SDCC (mcs51).
 *
 * Written from the data sheet P89CV51RB2_RC2_RD2 Rev. 03 (25 Aug 2009), Table 4 "Special function registers"
 * (pp. 10-12), the IE/IP/IPH bit tables 42-47 (pp. 54-55) and Table 41 (interrupt sources).  Classic 80C51 bits that
 * Table 4 leaves to the 80C51 definition (TCON.IE0/IT0, TMOD.T0M1/T0M0, SPCR.SPR1/SPR0, T2MOD) follow the data
 * sheet's per-register tables.
 *
 * Interrupt numbers for SDCC's __interrupt(n) (vector = 8n + 3):
 *   0 INT0 0003h, 1 Timer 0 000Bh, 2 INT1 0013h, 3 Timer 1 001Bh, 4 UART/SPI 0023h, 5 Timer 2 002Bh, 6 PCA 0033h.
 * Table 41 prints 003Bh for Timer 2; IE bit 5 (ET2) -> 8 x 5 + 3 = 002Bh, the P89V51RB2/RC2/RD2 data sheet
 * (Rev. 05, Table 43) and every 8052 derivative use 002Bh: see docs/MCS51_CORE.md "Timer 2 vector".
 */
#ifndef P89CV51_H
#define P89CV51_H

/* ---- 80C51 core ---------------------------------------------------------------------------------------------- */
__sfr __at (0x80) P0;
__sfr __at (0x81) SP;
__sfr __at (0x82) DPL;
__sfr __at (0x83) DPH;
__sfr __at (0x87) PCON;
__sfr __at (0x88) TCON;
__sfr __at (0x89) TMOD;
__sfr __at (0x8A) TL0;
__sfr __at (0x8B) TL1;
__sfr __at (0x8C) TH0;
__sfr __at (0x8D) TH1;
__sfr __at (0x90) P1;
__sfr __at (0x98) SCON;
__sfr __at (0x99) SBUF;
__sfr __at (0xA0) P2;
__sfr __at (0xA8) IE;
__sfr __at (0xB0) P3;
__sfr __at (0xB8) IP;
__sfr __at (0xD0) PSW;
__sfr __at (0xE0) ACC;
__sfr __at (0xF0) B;

/* ---- P89CV51 additions (Table 4) ----------------------------------------------------------------------------- */
__sfr __at (0x86) SPDAT;       /* SPI data */
__sfr __at (0x8E) AUXR;        /* - - - - - - EXTRAM AO */
__sfr __at (0x8F) CKCON;       /* SPIX2 WDX2 PCAX2 SIX2 T2X2 T1X2 T0X2 X2 */
__sfr __at (0xA2) AUXR1;       /* - - ENBOOT - GF2 0 - DPS */
__sfr __at (0xA6) WDTRST;
__sfr __at (0xA9) SADDR;
__sfr __at (0xAA) SPSR;        /* SPIF WCOL - - - - - - */
__sfr __at (0xB7) IPH;         /* - PPCH PT2H PSH PT1H PX1H PT0H PX0H */
__sfr __at (0xB9) SADEN;
__sfr __at (0xC8) T2CON;
__sfr __at (0xC9) T2MOD;       /* - - - - - - T2OE DCEN */
__sfr __at (0xCA) RCAP2L;
__sfr __at (0xCB) RCAP2H;
__sfr __at (0xCC) TL2;
__sfr __at (0xCD) TH2;
__sfr __at (0xD5) SPCR;        /* SPIE SPEN DORD MSTR CPOL CPHA SPR1 SPR0 */
__sfr __at (0xD8) CCON;
__sfr __at (0xD9) CMOD;        /* CIDL WDTE - - - CPS1 CPS0 ECF */
__sfr __at (0xDA) CCAPM0;
__sfr __at (0xDB) CCAPM1;
__sfr __at (0xDC) CCAPM2;
__sfr __at (0xDD) CCAPM3;
__sfr __at (0xDE) CCAPM4;
__sfr __at (0xE9) CL;
__sfr __at (0xEA) CCAP0L;
__sfr __at (0xEB) CCAP1L;
__sfr __at (0xEC) CCAP2L;
__sfr __at (0xED) CCAP3L;
__sfr __at (0xEE) CCAP4L;
__sfr __at (0xF9) CH;
__sfr __at (0xFA) CCAP0H;
__sfr __at (0xFB) CCAP1H;
__sfr __at (0xFC) CCAP2H;
__sfr __at (0xFD) CCAP3H;
__sfr __at (0xFE) CCAP4H;

/* ---- bit-addressable SFR bits -------------------------------------------------------------------------------- */
/* P0 80h */
__sbit __at (0x80) P0_0; __sbit __at (0x81) P0_1; __sbit __at (0x82) P0_2; __sbit __at (0x83) P0_3;
__sbit __at (0x84) P0_4; __sbit __at (0x85) P0_5; __sbit __at (0x86) P0_6; __sbit __at (0x87) P0_7;
/* TCON 88h */
__sbit __at (0x88) IT0; __sbit __at (0x89) IE0; __sbit __at (0x8A) IT1; __sbit __at (0x8B) IE1;
__sbit __at (0x8C) TR0; __sbit __at (0x8D) TF0; __sbit __at (0x8E) TR1; __sbit __at (0x8F) TF1;
/* P1 90h: T2 T2EX ECI CEX0 CEX1/SS CEX2/MOSI CEX3/MISO CEX4/SPICLK */
__sbit __at (0x90) P1_0; __sbit __at (0x91) P1_1; __sbit __at (0x92) P1_2; __sbit __at (0x93) P1_3;
__sbit __at (0x94) P1_4; __sbit __at (0x95) P1_5; __sbit __at (0x96) P1_6; __sbit __at (0x97) P1_7;
/* SCON 98h (bit 7 = FE when PCON.SMOD0 = 1) */
__sbit __at (0x98) RI; __sbit __at (0x99) TI; __sbit __at (0x9A) RB8; __sbit __at (0x9B) TB8;
__sbit __at (0x9C) REN; __sbit __at (0x9D) SM2; __sbit __at (0x9E) SM1; __sbit __at (0x9F) SM0;
/* P2 A0h */
__sbit __at (0xA0) P2_0; __sbit __at (0xA1) P2_1; __sbit __at (0xA2) P2_2; __sbit __at (0xA3) P2_3;
__sbit __at (0xA4) P2_4; __sbit __at (0xA5) P2_5; __sbit __at (0xA6) P2_6; __sbit __at (0xA7) P2_7;
/* IE A8h (Table 42) */
__sbit __at (0xA8) EX0; __sbit __at (0xA9) ET0; __sbit __at (0xAA) EX1; __sbit __at (0xAB) ET1;
__sbit __at (0xAC) ES; __sbit __at (0xAD) ET2; __sbit __at (0xAE) EC; __sbit __at (0xAF) EA;
/* P3 B0h */
__sbit __at (0xB0) P3_0; __sbit __at (0xB1) P3_1; __sbit __at (0xB2) P3_2; __sbit __at (0xB3) P3_3;
__sbit __at (0xB4) P3_4; __sbit __at (0xB5) P3_5; __sbit __at (0xB6) P3_6; __sbit __at (0xB7) P3_7;
/* IP B8h (Table 44) */
__sbit __at (0xB8) PX0; __sbit __at (0xB9) PT0; __sbit __at (0xBA) PX1; __sbit __at (0xBB) PT1;
__sbit __at (0xBC) PS; __sbit __at (0xBD) PT2; __sbit __at (0xBE) PPC;
/* T2CON C8h */
__sbit __at (0xC8) CP_RL2; __sbit __at (0xC9) C_T2; __sbit __at (0xCA) TR2; __sbit __at (0xCB) EXEN2;
__sbit __at (0xCC) TCLK; __sbit __at (0xCD) RCLK; __sbit __at (0xCE) EXF2; __sbit __at (0xCF) TF2;
/* PSW D0h */
__sbit __at (0xD0) P; __sbit __at (0xD1) F1; __sbit __at (0xD2) OV; __sbit __at (0xD3) RS0;
__sbit __at (0xD4) RS1; __sbit __at (0xD5) F0; __sbit __at (0xD6) AC; __sbit __at (0xD7) CY;
/* CCON D8h */
__sbit __at (0xD8) CCF0; __sbit __at (0xD9) CCF1; __sbit __at (0xDA) CCF2; __sbit __at (0xDB) CCF3;
__sbit __at (0xDC) CCF4; __sbit __at (0xDE) CR; __sbit __at (0xDF) CF;

/* ---- bit masks of byte-only SFRs ----------------------------------------------------------------------------- */
#define PCON_SMOD1 0x80
#define PCON_SMOD0 0x40
#define PCON_POF   0x10
#define PCON_GF1   0x08
#define PCON_GF0   0x04
#define PCON_PD    0x02
#define PCON_IDL   0x01
#define AUXR_EXTRAM 0x02
#define AUXR_AO     0x01
#define AUXR1_ENBOOT 0x20
#define AUXR1_GF2    0x08
#define AUXR1_DPS    0x01
#define SPCR_SPIE 0x80
#define SPCR_SPEN 0x40
#define SPCR_DORD 0x20
#define SPCR_MSTR 0x10
#define SPCR_CPOL 0x08
#define SPCR_CPHA 0x04
#define SPCR_SPR1 0x02
#define SPCR_SPR0 0x01
#define SPSR_SPIF 0x80
#define SPSR_WCOL 0x40
#define CMOD_CIDL 0x80
#define CMOD_WDTE 0x40
#define CMOD_CPS1 0x04
#define CMOD_CPS0 0x02
#define CMOD_ECF  0x01
#define CCAPM_ECOM 0x40
#define CCAPM_CAPP 0x20
#define CCAPM_CAPN 0x10
#define CCAPM_MAT  0x08
#define CCAPM_TOG  0x04
#define CCAPM_PWM  0x02
#define CCAPM_ECCF 0x01
#define T2MOD_T2OE 0x02
#define T2MOD_DCEN 0x01

/* ---- interrupt numbers for __interrupt(n) -------------------------------------------------------------------- */
#define IE0_VECTOR  0   /* 0003h external interrupt 0 */
#define TF0_VECTOR  1   /* 000Bh Timer 0 */
#define IE1_VECTOR  2   /* 0013h external interrupt 1 */
#define TF1_VECTOR  3   /* 001Bh Timer 1 */
#define SI0_VECTOR  4   /* 0023h UART (RI/TI) and SPI (SPIF) */
#define TF2_VECTOR  5   /* 002Bh Timer 2 (TF2/EXF2) */
#define PCA_VECTOR  6   /* 0033h PCA (CF/CCFn) */

/* ---- IAP entry (sec. 6.3.8, Table 12): LCALL 0FFF0h with R1 = function, DPTR / ACC = parameters ------------- */
#define PGM_MTP 0xFFF0

#endif
