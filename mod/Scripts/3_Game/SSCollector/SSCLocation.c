// --- JSON data classes for $profile:SSCollector/locations.json ---
//
// Expected file format:
// {
//     "locations": [
//         {
//             "position":   { "x": 6500.0, "y": 200.0, "z": 6500.0 },
//             "cameraYaw":  45.0,
//             "cameraPitch": -10.0,
//             "timeOfDay":  12.0,
//             "weather":    { "overcast": 0.3, "fog": 0.0, "rain": 0.0 }
//         }
//     ]
// }

class SSCLocationPos
{
    float x = 0.0;
    float y = 0.0;
    float z = 0.0;
}

class SSCLocationWeather
{
    float overcast = 0.0;
    float fog      = 0.0;
    float rain     = 0.0;
}

class SSCLocation
{
    ref SSCLocationPos     position;
    float                  cameraYaw   = 0.0;
    float                  cameraPitch = 0.0;
    float                  timeOfDay   = 12.0;  // 0.0 – 24.0 (hour + fractional minutes)
    ref SSCLocationWeather weather;
}

// Wrapper required by JsonFileLoader (cannot deserialise a bare array at root).
class SSCLocationList
{
    ref array<ref SSCLocation> locations;
}
