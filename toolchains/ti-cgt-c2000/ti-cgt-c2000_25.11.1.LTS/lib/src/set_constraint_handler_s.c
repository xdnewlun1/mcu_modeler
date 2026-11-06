/*-
 * SET_CONSTRAINT_HANDLER_S.C
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
 *
 * Changelog:
 * - Using TI-specific mutex API instead of that assumed by FREEBSD
 *   implementation - Todd Snider: 25 Mar 2024
 * - Rename variables for readability - Todd Snider: 25 Mar 2024
 * 
 */

#include <sys/cdefs.h>
#include <sys/types.h>
#include <errno.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <_mutex.h>
#include <_data_synch.h>
#include "_c11_secure_private.h"

/*---------------------------------------------------------------------------*/
/* LIBC runtime library maintains a pointer to the currently registered      */
/* constraint handler function.                                              */
/*---------------------------------------------------------------------------*/
static constraint_handler_t _curr_ch_func = &ignore_handler_s;

/*****************************************************************************/
/* SET_CONSTRAINT_HANDLER_S()                                                */
/*    Register a function to be called when a C11 security violation is      */
/*    detected.                                                              */
/*****************************************************************************/
constraint_handler_t set_constraint_handler_s(constraint_handler_t handler)
{
  constraint_handler_t new_ch = handler;
  constraint_handler_t old_ch;

  /*-------------------------------------------------------------------------*/
  /* An incoming NULL handler argument will register the default constraint  */
  /* handler as the new constraint handler.                                  */
  /*-------------------------------------------------------------------------*/
  if (new_ch == NULL) 
    new_ch = (constraint_handler_t)&ignore_handler_s;

  /*-------------------------------------------------------------------------*/
  /* Guard access to the pointer to the current constraint handler while     */
  /* doing the actual registration. _curr_ch_func is a global resource, so   */
  /* access to it is relegated to a critical section.                        */
  /*-------------------------------------------------------------------------*/
  __TI_resource_lock(__TI_LOCK_CONSTRAINT_HANDLER);
  old_ch = _curr_ch_func;
  _curr_ch_func = new_ch;
  __TI_data_synch_WBINV(&_curr_ch_func, sizeof(_curr_ch_func));
  __TI_resource_unlock(__TI_LOCK_CONSTRAINT_HANDLER);

  /*-------------------------------------------------------------------------*/
  /* The return value is the pointer to the previously registered handler.   */
  /*-------------------------------------------------------------------------*/
  return (old_ch);
}

/*****************************************************************************/
/* __THROW_CONSTRAINT_HANDLER_S()                                            */
/*    When a C11 secure function constraint violation is detected, this      */
/*    function is called to in turn call the currently registered constraint */
/*    handler function.                                                      */
/*****************************************************************************/
void __throw_constraint_handler_s(const char * restrict msg, errno_t error)
{
  constraint_handler_t curr_ch;

  /*-------------------------------------------------------------------------*/
  /* Guard access to the pointer to the pointer to the registered handler.   */
  /*-------------------------------------------------------------------------*/
  __TI_resource_lock(__TI_LOCK_CONSTRAINT_HANDLER);
  curr_ch = _curr_ch_func;
  __TI_resource_unlock(__TI_LOCK_CONSTRAINT_HANDLER);

  /*-------------------------------------------------------------------------*/
  /* If there is a registered constraint handler function, ring it up.       */
  /*-------------------------------------------------------------------------*/
  if (curr_ch != NULL) curr_ch(msg, NULL, error);
}

/*****************************************************************************/
/* ABORT_HANDLER_S()                                                         */
/*    Write the constraint violation diagnostic to STDERR and abort() the    */
/*    current application. The C11 K annex prescribes that the user can      */
/*    legally register this function via set_constraint_handler_s().         */
/*****************************************************************************/
void abort_handler_s(const char * restrict msg,
                     void * restrict ptr,
                     errno_t error)
{
  static const char ahs[] = "abort_handler_s : ";

  fputs(ahs, stderr);
  if (msg) fputs(msg, stderr);
  fputs("\n", stderr);
  abort();
}

/*****************************************************************************/
/* IGNORE_HANDLER_S()                                                        */
/*    Default constraint handler function. Does nothing. The C11 K annex     */
/*    prescribes that the user can legally register this function via        */
/*    set_constraint_handler_s().                                            */
/*****************************************************************************/
void ignore_handler_s(const char * restrict msg, 
                      void * restrict ptr,
                      errno_t error)
{ }

