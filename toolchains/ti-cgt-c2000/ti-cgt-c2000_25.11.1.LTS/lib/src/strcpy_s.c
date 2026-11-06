/****************************************************************************/
/* strcpy_s.c                                                               */
/*                                                                          */
/* Copyright (c) 2024 Texas Instruments Incorporated                        */
/* http://www.ti.com/                                                       */
/*                                                                          */
/*  Redistribution and  use in source  and binary forms, with  or without   */
/*  modification,  are permitted provided  that the  following conditions   */
/*  are met:                                                                */
/*                                                                          */
/*     Redistributions  of source  code must  retain the  above copyright   */
/*     notice, this list of conditions and the following disclaimer.        */
/*                                                                          */
/*     Redistributions in binary form  must reproduce the above copyright   */
/*     notice, this  list of conditions  and the following  disclaimer in   */
/*     the  documentation  and/or   other  materials  provided  with  the   */
/*     distribution.                                                        */
/*                                                                          */
/*     Neither the  name of Texas Instruments Incorporated  nor the names   */
/*     of its  contributors may  be used to  endorse or  promote products   */
/*     derived  from   this  software  without   specific  prior  written   */
/*     permission.                                                          */
/*                                                                          */
/*  THIS SOFTWARE  IS PROVIDED BY THE COPYRIGHT  HOLDERS AND CONTRIBUTORS   */
/*  "AS IS"  AND ANY  EXPRESS OR IMPLIED  WARRANTIES, INCLUDING,  BUT NOT   */
/*  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR   */
/*  A PARTICULAR PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT   */
/*  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,   */
/*  SPECIAL,  EXEMPLARY,  OR CONSEQUENTIAL  DAMAGES  (INCLUDING, BUT  NOT   */
/*  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,   */
/*  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY   */
/*  THEORY OF  LIABILITY, WHETHER IN CONTRACT, STRICT  LIABILITY, OR TORT   */
/*  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE   */
/*  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.    */
/*                                                                          */
/****************************************************************************/

#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include "_c11_secure_private.h"

/****************************************************************************/
/* STRCPY_S()                                                               */
/*    Copy a source string into the specified destination buffer. This      */
/*    being a C11 secure variant of the strcpy() function, C11 secure       */
/*    function constraints will be enforced as detailed in the comments     */
/*    below and prescribed by the C11 language standard.                    */
/****************************************************************************/
#define MAX_EMSG_SZ	100

errno_t
strcpy_s(char * __restrict dest, rsize_t destsz, const char * __restrict src)
{
  errno_t ret = EINVAL;
  char    emsg[MAX_EMSG_SZ] = "strcpy_s : ";

  /*------------------------------------------------------------------------*/
  /* The destination string buffer pointer cannot be NULL.                  */
  /*------------------------------------------------------------------------*/
  if (dest == NULL)
    strcat(emsg, "dest is NULL");

  /*------------------------------------------------------------------------*/
  /* The source string buffer pointer cannot be NULL.                       */
  /*------------------------------------------------------------------------*/
  else if (src == NULL)
    strcat(emsg, "src is NULL");

  /*------------------------------------------------------------------------*/
  /* The specified destination buffer size must be in [1,RSIZE_MAX].        */
  /*------------------------------------------------------------------------*/
  else if (destsz == 0)
    strcat(emsg, "destsz is 0");
  else if (destsz > RSIZE_MAX)
    strcat(emsg, "destsz > RSIZE_MAX");

  /*------------------------------------------------------------------------*/
  /* The length of the source string cannot be > the specified size of the  */
  /* destination buffer.                                                    */
  /*------------------------------------------------------------------------*/
  else if (destsz <= strnlen_s(src,destsz))
    strcat(emsg, "src will be truncated");

  /*------------------------------------------------------------------------*/
  /* The source and destination string buffers may not overlap.             */
  /*------------------------------------------------------------------------*/
  else if (((src >= dest) && (src < (dest + destsz))) ||
           ((src <= dest) && (dest < (src + strnlen_s(src, destsz)))))
    strcat(emsg, "src and dest overlap");

  /*------------------------------------------------------------------------*/
  /* All constraint checks have been cleared. Do the copy.                  */
  /*------------------------------------------------------------------------*/
  else {
    strcpy(dest, src);
    return 0;
  }

  /*------------------------------------------------------------------------*/
  /* If dest and destsz are valid, write a zero to dest[0] on error.        */
  /*------------------------------------------------------------------------*/
  if (dest && (destsz > 0) && (destsz <= RSIZE_MAX))
    dest[0] = 0;

  /*------------------------------------------------------------------------*/
  /* A C11 secure function constraint was detected, initiate a call to the  */
  /* constraint handler function (if one was registered).                   */
  /*------------------------------------------------------------------------*/
  __throw_constraint_handler_s(emsg, ret);
  return ret;
}
