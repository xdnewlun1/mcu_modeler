/*-
 * MEMSET_S.C
 *
 * Copyright (c) 2017 Juniper Networks.  All rights reserved.
 *
 * Copyright (c) 2024 Texas Instruments Incorporated
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS'' AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE.
 */

#include <sys/cdefs.h>
#include <stdlib.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include "_c11_secure_private.h"

/****************************************************************************/
/* MEMSET_S() - Write count characters (ch) to destination buffer (dest).   */
/*                                                                          */
/* This C11 secure version of memset enforces constraints specified in      */
/* ISO/IEC 9899:2011 K.3.7.4.1 (detailed in comments below).                */
/*                                                                          */
/****************************************************************************/
#define MAX_EMSG_SZ	100
errno_t memset_s(void *dest, rsize_t destsz, int ch, rsize_t count)
{
  errno_t ret = EINVAL;
  char emsg[MAX_EMSG_SZ] = "memset_s : ";

  /*------------------------------------------------------------------------*/
  /* Destination buffer pointer cannot be NULL.                             */
  /*------------------------------------------------------------------------*/
  if (dest == NULL)
    strcat(emsg, "dest is NULL");

  /*------------------------------------------------------------------------*/
  /* Indicated destination buffer size cannot be > RSIZE_MAX.               */
  /*------------------------------------------------------------------------*/
  else if (destsz > RSIZE_MAX) 
    strcat(emsg, "destsz > RSIZE_MAX");

  /*------------------------------------------------------------------------*/
  /* Indicated count of characters to be written cannot be > RSIZE_MAX.     */
  /*------------------------------------------------------------------------*/
  else if (count > RSIZE_MAX)
    strcat(emsg, "count > RSIZE_MAX");

  /*------------------------------------------------------------------------*/
  /* Indicated count cannot be > indicated destination buffer size.         */
  /*------------------------------------------------------------------------*/
  else if (count > destsz)
    strcat(emsg, "count > destsz");

  /*------------------------------------------------------------------------*/
  /* All security constraints satisfied.                                    */
  /*------------------------------------------------------------------------*/
  else  ret = 0;

  /*------------------------------------------------------------------------*/
  /* If dest and destsz are legit, then some characters will be written     */
  /* to dest. If a constraint violation has been detected, then the         */
  /* standard requires that destsz characters be written to dest.           */
  /*------------------------------------------------------------------------*/
  if (dest && (destsz <= RSIZE_MAX)) 
    memset(dest, ch, ret ? destsz : count);

  /*------------------------------------------------------------------------*/
  /* If a constraint violation was detected, trigger a call to the          */
  /* registered constraint handler function (if there is one).              */
  /*------------------------------------------------------------------------*/
  if (ret)
    __throw_constraint_handler_s(emsg, ret);

  return (ret);
}
