#include <Arduino_RouterBridge.h>


const uint8_t CONTROL_PIN = D6;


void set_cart_direction(String direction)
{
    if (direction == "LEFT")
    {
        // 0% PWM: 0 V
        analogWrite(CONTROL_PIN, 0);
    }
    else if (direction == "RIGHT")
    {
        // 100% PWM: 3.3 V
        analogWrite(CONTROL_PIN, 255);
    }
    else
    {
        // 50% PWM: approximately 1.65 V average
        analogWrite(CONTROL_PIN, 128);
    }
}


void setup()
{
    pinMode(CONTROL_PIN, OUTPUT);

    // Start centered.
    set_cart_direction("CENTER");

    Bridge.begin();

    Bridge.provide(
        "set_cart_direction",
        set_cart_direction
    );
}


void loop(){}