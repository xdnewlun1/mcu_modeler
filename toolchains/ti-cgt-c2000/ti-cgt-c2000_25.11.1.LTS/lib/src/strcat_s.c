/****************************************************************************/
/* strcat_s.c                                                               */
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
/* STRCAT_S()                                                               */
/*    Concatentate the source string on to the back of the destination      */
/*    string. Enforce C11 secure function constraints as prescribed in the  */
/*    C11 language standard. See comments below for information about what  */
/*    constraints are checked and reported.                                 */
/****************************************************************************/
#define MAX_EMSG_SZ	100
errno_t
strcat_s(char * __restrict dest, rsize_t destsz, const char * __restrict src)
{
  char    emsg[MAX_EMSG_SZ] = "strcat_s : ";

  /*-------------------------------------------------------------------------*/
  /* Destination buffer pointer cannot be NULL.                              */
  /*-------------------------------------------------------------------------*/
  if (dest == NULL)
    strcat(emsg, "dest is NULL");

  /*-------------------------------------------------------------------------*/
  /* Source buffer pointer cannot be NULL.                                   */
  /*-------------------------------------------------------------------------*/
  else if (src == NULL)
    strcat(emsg, "src is NULL");

  /*-------------------------------------------------------------------------*/
  /* Specified destination buffer size must be in [1,RSIZE_MAX].             */
  /*-------------------------------------------------------------------------*/
  else if (destsz == 0)
    strcat(emsg, "destsz is 0");
  else if (destsz > RSIZE_MAX)
    strcat(emsg, "destsz > RSIZE_MAX");

  /*-------------------------------------------------------------------------*/
  /* There must be a null terminator character within the first destsz       */
  /* bytes of the destination string buffer.                                 */
  /*-------------------------------------------------------------------------*/
  else if (strnlen_s(dest, destsz) == destsz)
    strcat(emsg, "no null terminator in first destsz bytes of dest");

  /*-------------------------------------------------------------------------*/
  /* The length of the string in the source buffer should not overrun the    */
  /* specified size of the destination buffer when the source string is      */
  /* concatenated to the incoming string in the destination buffer.          */
  /*-------------------------------------------------------------------------*/
  else if (destsz <= (strnlen_s(dest,destsz) + strnlen_s(src,destsz)))
    strcat(emsg, "src will be truncated");

  /*-------------------------------------------------------------------------*/
  /* Src and dest string buffers may not overlap.                            */
  /*-------------------------------------------------------------------------*/
  else if (((src >= dest) && (src < (dest + destsz))) ||
           ((src <= dest) && (dest < (src + strnlen_s(src,destsz)))))
    strcat(emsg, "src and dest overlap");

  /*-------------------------------------------------------------------------*/
  /* All C11 secure function constraints for strcat_s have been satisfied.   */
  /* Do the concatenation.                                                   */
  /*-------------------------------------------------------------------------*/
  else {
    strcat(dest, src);
    return 0;
  }

  /*-------------------------------------------------------------------------*/
  /* If dest != NULL and destsz is in [1,RSIZE_MAX], write a zero to         */
  /* dest[0].                                                                */
  /*-------------------------------------------------------------------------*/
  if (dest && (destsz > 0) && (destsz <= RSIZE_MAX))
     dest[0] = 0;

  /*-------------------------------------------------------------------------*/
  /* A constraint violation was detected, initiate a call to the             */
  /* constraint handler function (if one was registered).                    */
  /*-------------------------------------------------------------------------*/
  __throw_constraint_handler_s(emsg, EINVAL);
  return EINVAL;
}
