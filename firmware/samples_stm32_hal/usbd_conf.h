/* usbd_conf.h for the ST USB Device Library (usb_cdc_hal.c): ST's usbd_conf_template.h reduced to what the CDC class
 * needs; static allocation (no heap), no debug printf. */
#ifndef USBD_CONF_H
#define USBD_CONF_H

#include "stm32f4xx_hal.h"
#include <stdint.h>
#include <stddef.h>

void *memset(void *d, int c, size_t n);
void *memcpy(void *d, const void *s, size_t n);

#define USBD_MAX_NUM_INTERFACES        2U
#define USBD_MAX_NUM_CONFIGURATION     1U
#define USBD_MAX_STR_DESC_SIZ          0x100U
#define USBD_SELF_POWERED              1U
#define USBD_DEBUG_LEVEL               0U
#define USBD_LPM_ENABLED               0U
#define USBD_SUPPORT_USER_STRING_DESC  0U
#define USBD_CLASS_USER_STRING_DESC    0U
#define USBD_CLASS_BOS_ENABLED         0U

#define DEVICE_FS 0

void *USBD_static_malloc(uint32_t size);
void USBD_static_free(void *p);

#define USBD_malloc   (void *)USBD_static_malloc
#define USBD_free     USBD_static_free
#define USBD_memset   memset
#define USBD_memcpy   memcpy
#define USBD_Delay    HAL_Delay

#define USBD_UsrLog(...)   do {} while (0)
#define USBD_ErrLog(...)   do {} while (0)
#define USBD_DbgLog(...)   do {} while (0)

#endif /* USBD_CONF_H */
