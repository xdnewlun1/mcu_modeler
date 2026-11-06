/****************************************************************************/
/* memcpy_s.c                                                               */
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
/* MEMCPY_S()                                                               */
/*    Read count characters from a source buffer and write them to a        */
/*    destination buffer. The C11 secure version of memcpy will enforce     */
/*    security constraints as prescribed by the C11 standard. Details of    */
/*    the constraints to be enforced are described in the comments below.   */
/****************************************************************************/
#define MAX_EMSG_SZ	100
errno_t
memcpy_s(void * __restrict dest, rsize_t destsz, 
         const void * __restrict src, rsize_t count)
{
  char emsg[MAX_EMSG_SZ] = "memcpy_s : ";

  /*------------------------------------------------------------------------*/
  /* Destination buffer pointer cannot be NULL.                             */
  /*------------------------------------------------------------------------*/
  if (dest == NULL)
    strcat(emsg, "dest is NULL");

  /*------------------------------------------------------------------------*/
  /* Source buffer pointer cannot be NULL.                                  */
  /*------------------------------------------------------------------------*/
  else if (src == NULL)
    strcat(emsg, "src is NULL");

  /*------------------------------------------------------------------------*/
  /* Indicated destination buffer size cannot be > RSIZE_MAX.               */
  /*------------------------------------------------------------------------*/
  else if (destsz > RSIZE_MAX)
    strcat(emsg, "destsz > RSIZE_MAX");

  /*------------------------------------------------------------------------*/
  /* Indicated count cannot be > RSIZE_MAX.                                 */
  /*------------------------------------------------------------------------*/
  else if (count > RSIZE_MAX)
    strcat(emsg, "count > RSIZE_MAX");

  /*------------------------------------------------------------------------*/
  /* Indicated count cannot be > indicated destination buffer size.         */
  /*------------------------------------------------------------------------*/
  else if (count > destsz)
    strcat(emsg, "count > destsz");

  /*------------------------------------------------------------------------*/
  /* Source and destination buffers may not overlap.                        */
  /*------------------------------------------------------------------------*/
  else if ((((unsigned char *)src >= (unsigned char *)dest) &&
            ((unsigned char *)src < ((unsigned char *)dest + destsz))) ||
           (((unsigned char *)src <= (unsigned char *)dest) &&
            ((unsigned char *)dest < ((unsigned char *)src + count))))
    strcat(emsg, "src and dest overlap");

  /*------------------------------------------------------------------------*/
  /* All constraint violation checks have been cleared. Do the copy.        */
  /*------------------------------------------------------------------------*/
  else {
    memcpy(dest, src, count);
    return (0);
  }

  /*------------------------------------------------------------------------*/
  /* If any constraint violations are detected, the C11 standard prescribes */
  /* that zeroes are written to dest[0] through dest[destsz-1].             */
  /*------------------------------------------------------------------------*/
  if (dest && (destsz <= RSIZE_MAX))
    memset(dest, 0, destsz);

  __throw_constraint_handler_s(emsg, EINVAL);
  return (EINVAL);
}
