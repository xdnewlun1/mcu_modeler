/*****************************************************************************/
/*  _tz                                                                      */
/*                                                                           */
/* Copyright (c) 1993 Texas Instruments Incorporated                         */
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
#include <time.h>

/*****************************************************************************/
/* This global variable defines the local time zone.  It is used by          */
/* mktime(), strftime(), localtime(), and gmtime().  If you want to change   */
/* the local time zone, you must assign appropriate new values to each of    */
/* the four fields; the TI library does not provide time zone lookup         */
/* tables.                                                                   */
/*                                                                           */
/* daylight is used by mktime() to set struct tm member tm_isdst.  tm_isdst  */
/* will be set to the same value as _tz.daylight                             */
/*                                                                           */
/* timezone is a signed integer representing the number of seconds west of   */
/* UTC.  For example, Central Standard Time (UTC+6) is 21600 seconds west    */
/* of UTC.  This value should be negative for timezones east of UTC.         */
/*                                                                           */
/* tzname is the abbreviated name of the local time zone.  This string will  */
/* be used verbatim by strftime() for the %Z conversion.                     */
/*                                                                           */
/* dstname is currently unused.                                              */
/*****************************************************************************/

_DATA_ACCESS TZ _tz =
{
   -1,                      /* daylight */
   21600,                   /* timezone (seconds WEST of UTC) */
   "CST",                   /* tzname   */
   "DST",                   /* dstname  */
};

