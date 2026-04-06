// Pending free-camera command written by PlayerBase.OnRPC (4_World) and
// consumed by MissionGameplay.OnUpdate (5_Mission), where FreeDebugCamera lives.
class SSCCameraCommand
{
    static bool  pending = false;
    static float x       = 0;
    static float y       = 0;
    static float z       = 0;
    static float yaw     = 0;
    static float pitch   = 0;
}

class SSCRpc
{
    static const int CAPTURE_META    = 20001;
    static const int NAVIGATE        = 20002;  // client → server: int delta (+1 / -1)
    static const int SET_CAMERA      = 20003;  // server → client: float yaw, float pitch
    static const int ADD_LOCATION    = 20004;  // client → server: int yawCount
    static const int CLEAR_LOCATIONS = 20005;  // client → server: no payload
    static const int GENERATE_GRID   = 20006;  // client → server: int step, int yawCount
    static const int RELOAD          = 20007;  // client → server: no payload
    static const int TOGGLE_GOD      = 20008;  // client → server: no payload
}
