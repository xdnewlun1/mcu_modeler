/*****************************************************************************/
/* GETS_S.C                                                                  */
/*                                                                           */
/* Copyright (c) 2024 Texas Instruments Incorporated                         */
/* http://www.ti.com/                                                        */
/*                                                                           */
/*  Redistribution and  use in source  and binary forms, with  or without    */
/*  modification,  are permitted provided  that the  following conditions    */
/*  are met:                                                                 */
/*                                                                           */
/*     Redistributions  of source  code must  retain the  above copyright    */
/*     notice, this list of conditions and the following disclaimer.         */
/*                                                                           */
/*     Redistributions in binary form  must reproduce the above copyright    */
/*     notice, this  list of conditions  and the following  disclaimer in    */
/*     the  documentation  and/or   other  materials  provided  with  the    */
/*     distribution.                                                         */
/*                                                                           */
/*     Neither the  name of Texas Instruments Incorporated  nor the names    */
/*     of its  contributors may  be used to  endorse or  promote products    */
/*     derived  from   this  software  without   specific  prior  written    */
/*     permission.                                                           */
/*                                                                           */
/*  THIS SOFTWARE  IS PROVIDED BY THE COPYRIGHT  HOLDERS AND CONTRIBUTORS    */
/*  "AS IS"  AND ANY  EXPRESS OR IMPLIED  WARRANTIES, INCLUDING,  BUT NOT    */
/*  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR    */
/*  A PARTICULAR PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT    */
/*  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,    */
/*  SPECIAL,  EXEMPLARY,  OR CONSEQUENTIAL  DAMAGES  (INCLUDING, BUT  NOT    */
/*  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,    */
/*  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY    */
/*  THEORY OF  LIABILITY, WHETHER IN CONTRACT, STRICT  LIABILITY, OR TORT    */
/*  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE    */
/*  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.     */
/*                                                                           */
/*****************************************************************************/

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <_mutex.h>
#include <_data_synch.h>
#include <file.h>
#include "_c11_secure_private.h"

extern _CODE_ACCESS int  __TI_rd_ok(FILE *_fp);
extern _CODE_ACCESS void __TI_buff_read(FILE *_fp);

/*****************************************************************************/
/* GETS_S - Perform main GETS routine, enforcing security constraints        */
/*                                                                           */
/*    This function gets a string from stdin.  It returns a pointer to the   */
/*    string upon success, or NULL upon failure. Security constraints        */
/*    prescribed by the C11 language standard are enforced.                  */
/*                                                                           */
/*****************************************************************************/
#define MAX_EMSG_SZ	80
char *gets_s(char * __restrict _buf, rsize_t _size)
{
   /*------------------------------------------------------------------------*/
   /* Local variables                                                        */
   /*------------------------------------------------------------------------*/
   unsigned char *next_nl     = NULL;
   char          *bufp        = _buf;
   size_t         num_left    = _size -1,
                  num_to_read;

   /*------------------------------------------------------------------------*/
   /* Perform pre-read security checks ...                                   */
   /*------------------------------------------------------------------------*/
   errno_t eval = EINVAL;
   char    emsg[MAX_EMSG_SZ] = "gets_s : ";

   /*------------------------------------------------------------------------*/
   /* Pointer to storage buffer cannot be NULL.                              */
   /*------------------------------------------------------------------------*/
   if (_buf == NULL)
      strcat(emsg, "storage buffer pointer is NULL");

   /*------------------------------------------------------------------------*/
   /* Number of characters to read cannot be zero.                           */
   /*------------------------------------------------------------------------*/
   else if (_size == 0)
      strcat(emsg, "size value == 0");

   /*------------------------------------------------------------------------*/
   /* Number of characters to read cannot be > RSIZE_MAX.                    */
   /*------------------------------------------------------------------------*/
   else if (_size > RSIZE_MAX)
      strcat(emsg, "size value > RSIZE_MAX");

   /*------------------------------------------------------------------------*/
   /* Cleared pre-read security checks.                                      */
   /*------------------------------------------------------------------------*/
   else eval = 0;

   /*------------------------------------------------------------------------*/
   /* The current thread in a multi-threaded application must protect access */
   /* to the file stream. In this case, stdin may be updated, so we must     */
   /* ensure that the local copy of stdin is flushed to shared memory before */
   /* leaving the critical section (invalidated if it is not modified).      */
   /*------------------------------------------------------------------------*/
   __TI_file_lock(stdin);

   /*------------------------------------------------------------------------*/
   /* Make sure that the stream is readable.                                 */
   /*------------------------------------------------------------------------*/
   if (!__TI_rd_ok(stdin) || _STCHK(stdin, _STATEOF)) 
   { 
      __TI_data_synch_INV(stdin, sizeof(FILE));
      __TI_file_unlock(stdin);
      return (NULL);
   }

   /*------------------------------------------------------------------------*/
   /* If the stream is non buffered, read the string in one character at a   */
   /* time, using the lowlevel READ function.                                */
   /*------------------------------------------------------------------------*/
   if (_BUFFMODE(stdin) == _IONBF)
   {
       int errchk = 1;
       char bufch = '\0';
  
       /*--------------------------------------------------------------------*/
       /* Read input from stdin one character at a time via low-level READ.  */
       /*--------------------------------------------------------------------*/
       do 
       {
	   /*----------------------------------------------------------------*/
	   /* Check for input truncation constraint violation.               */
	   /*----------------------------------------------------------------*/
           if ((bufp - _buf) >= (_size - 1))
           {
               strcat(emsg, "input string truncated");
               eval = EINVAL;
           }

           if (_STCHK(stdin, _UNGETC))
           {
               bufch = *(stdin->pos++); 
               _UNSET(stdin, _UNGETC); 
           }
           else if ((errchk = read(stdin->fd, &bufch, 1)) <= 0) break;

	   /*----------------------------------------------------------------*/
	   /* If we've seen a constraint violation, do not write to _buf.    */
	   /*----------------------------------------------------------------*/
           if (!eval) *bufp++ = bufch;

       } while (bufch != '\n'); 
 
       __TI_data_synch_WBINV(stdin, sizeof(FILE));
       __TI_file_unlock(stdin);
 
       switch(errchk)
       {
           case  0 : _SET(stdin, _STATEOF); return NULL;
           case -1 : _SET(stdin, _STATERR); return NULL;
       }
  
       if (!eval) *bufp = '\0';

       /*--------------------------------------------------------------------*/
       /* A constraint violation was detected.                               */
       /*--------------------------------------------------------------------*/
       if (eval) goto constraint_violation_detected;

       return (_buf);
   }

   else
   {
       while (!next_nl)
       {
          /*-----------------------------------------------------------------*/
          /* If the buffer is empty or completely read, fill it up.          */
          /*-----------------------------------------------------------------*/
          if (stdin->pos == stdin->buff_stop) __TI_buff_read(stdin);

          if (_STCHK(stdin, (_STATERR | _STATEOF)))
          {
              /*-------------------------------------------------------------*/
              /* If we've read any characters, fall out of for loop,         */
              /* returning what we've read in.                               */
              /*-------------------------------------------------------------*/
              if (bufp != _buf) break;

              /*-------------------------------------------------------------*/
              /* Otherwise we have not read anything (some kind of error).   */
              /*-------------------------------------------------------------*/
              __TI_data_synch_WBINV(stdin, sizeof(FILE));
              __TI_file_unlock(stdin);
              return (NULL);
          }

          /*-----------------------------------------------------------------*/
          /* Determine the location of the next newline character (NEXT_NL). */
          /*-----------------------------------------------------------------*/
          next_nl = (unsigned char *)memchr(stdin->pos, '\n', 
                                            (stdin->buff_stop - stdin->pos));

          /*-----------------------------------------------------------------*/
          /* Determine how many characters to read from the buffer. Since we */
          /* are a GETS, read until a newline character, EOF character, or   */
          /* the end of the buffer is encountered.                           */
          /*-----------------------------------------------------------------*/
          num_to_read =  stdin->buff_stop - stdin->pos;
          num_to_read =  (next_nl) ? (next_nl - stdin->pos + 1) : num_to_read;

          /*-----------------------------------------------------------------*/
          /* If we haven't seen a constraint violation, copy the stdin       */
          /* buffer to the input storage buffer. Copy the characters; update */
          /* pointers and counters.                                          */
          /*-----------------------------------------------------------------*/
          while (num_to_read)
          {
             if (!eval)
             {
                /*-----------------------------------------------------------*/
                /* Check for input truncation constraint violation.          */
                /*-----------------------------------------------------------*/
                if (!num_left)
                {
                   strcat(emsg, "input string truncated");
                   eval = EINVAL;
                   break;
                }
                else
                {
                   *bufp++ = *(stdin->pos);
                   num_left--;
                }
             }

             stdin->pos += 1;
	     num_to_read--;
          }
       }

       /*--------------------------------------------------------------------*/
       /* Tack on the NULL terminator, clear the _UNGETC flag.               */
       /*--------------------------------------------------------------------*/
       if (!eval) *bufp = '\0';
       _UNSET(stdin, _UNGETC);

       __TI_data_synch_WBINV(stdin, sizeof(FILE));
       __TI_file_unlock(stdin);
   }

   /*------------------------------------------------------------------------*/
   /* If any C11 constraint violations were detected, call the current       */
   /* constraint handler function and return NULL.                           */
   /*------------------------------------------------------------------------*/
   if (eval)
   {
constraint_violation_detected:
      if (_buf && (_size > 0))_buf[0] = '\0';
      __throw_constraint_handler_s(emsg, eval);
      return NULL;
   }

   return(_buf);
}

