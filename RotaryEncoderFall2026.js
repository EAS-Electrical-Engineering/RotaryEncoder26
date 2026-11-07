/*

* Read a US Digital MA3-P12-125-B PWM encoder

* Encoder signal connected to P9_42.

*

* The MA3 output must be reduced to 3.3 V

* before connecting it to the BeagleBone.
*use a voltage divider, 3.3kohms and 1.8kohms
*use common gnd, p9_1 or p9_2
*
*delay is on account of javas method of processing data

*/
 
var b = require("bonescript");
 
var encoderPin = "P9_42";
 
var risingTime = 0;

var previousRisingTime = 0;

var period = 0;
 
var filteredPosition = 0;

var lastPrintedPosition = -1;

var firstReading = true;
 
console.log("Starting encoder program");
 
// Configure P9_42 as a GPIO input

b.pinMode(

    encoderPin,

    b.INPUT,

    7,

    "pulldown",

    "fast",

    attachEncoder

);
 
function attachEncoder(result) {

    if (result.err) {

        console.log("Pin setup error: " + result.err);

        return;

    }
 
    console.log("Pin configured: " + encoderPin);
 
    b.attachInterrupt(

        encoderPin,

        true,

        b.CHANGE,

        encoderChanged

    );

}
 
function getTimeNanoseconds() {

    var time = process.hrtime();
 
    return (time[0] * 1000000000) + time[1];

}
 
function encoderChanged(event) {

    if (event.attached) {

        console.log("Interrupt attached");

        return;

    }
 
    var now = getTimeNanoseconds();
 
    // Rising edge starts a PWM cycle

    if (event.value == 1) {

        previousRisingTime = risingTime;

        risingTime = now;
 
        if (previousRisingTime != 0) {

            period = risingTime - previousRisingTime;

        }
 
        return;

    }
 
    // Falling edge ends the high pulse

    if (event.value == 0) {

        if (risingTime == 0 || period == 0) {

            return;

        }
 
        var pulseWidth = now - risingTime;
 
        // Convert nanoseconds to microseconds

        var highTime = pulseWidth / 1000;

        var totalTime = period / 1000;
 
        // Calculate the 12-bit encoder position

        var position = ((highTime * 4098) / totalTime) - 1;
 
        if (position < 0) {

            position = 0;

        }
 
        if (position > 4095) {

            position = 4095;

        }
 
        // Smooth the position

        if (firstReading) {

            filteredPosition = position;

            firstReading = false;

        } else {

            filteredPosition =

                (0.8 * filteredPosition) +

                (0.2 * position);

        }
 
        var roundedPosition = Math.round(filteredPosition);
 
        // Print only changes of at least 5 counts

        if (

            lastPrintedPosition == -1 ||

            Math.abs(roundedPosition - lastPrintedPosition) >= 5

        ) {

            console.log(

                "Position: " + roundedPosition +

                " / 4095, duty cycle: " +

                ((highTime / totalTime) * 100).toFixed(2) + "%"

            );
 
            lastPrintedPosition = roundedPosition;

        }

    }

}
 
