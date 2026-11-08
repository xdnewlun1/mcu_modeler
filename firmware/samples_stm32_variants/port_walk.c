/* port_walk: drives every bit of GPIOA..GPIOI as a push-pull output and toggles all 16 bits of one port after the
 * other (high, low, WALK_TOGGLES times), then repeats the walk forever. The die has the same nine ports in every
 * package, but only the bonded ones reach a pin: the set of pins that toggle is exactly the package's GPIO list
 * (LQFP64: 51, WLCSP90: 72, LQFP100: 82, LQFP144: 114, LQFP176/UFBGA176: 140 GPIOs, DS8626 Table 2).
 * 16 MHz HSI, no clock setup. `walks` counts completed walks. */
#include "variants.h"

#define WALK_TOGGLES 2u

volatile uint32_t walks;

static GPIO_TypeDef *const ports[] = {GPIOA, GPIOB, GPIOC, GPIOD, GPIOE, GPIOF, GPIOG, GPIOH, GPIOI};

int main(void)
{
    RCC_AHB1ENR |= RCC_AHB1ENR_GPIOALL;
    for (unsigned p = 0; p < sizeof ports / sizeof ports[0]; p++) {
        ports[p]->ODR = 0;
        ports[p]->MODER = 0x55555555u;             /* all 16 pins: general-purpose output */
    }
    for (;;) {
        for (unsigned p = 0; p < sizeof ports / sizeof ports[0]; p++) {
            for (unsigned n = 0; n < WALK_TOGGLES; n++) {
                ports[p]->ODR = 0xFFFFu;
                ports[p]->ODR = 0;
            }
        }
        walks++;
    }
}
