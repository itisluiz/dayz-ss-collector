// SSCNavigator — loads locations.json, manages per-player index, applies each
// location (teleport + weather + time) on the server, and bounces a SET_CAMERA
// RPC back to the requesting client so it can orient the player camera.

class SSCNavigator
{
    static const string LOCATIONS_PATH = "$profile:SSCollector/locations.json";

    protected static ref array<ref SSCLocation> s_Locations;

    // Called once from MissionServer.OnInit().
    static void Init()
    {
        s_Locations = new array<ref SSCLocation>();

        if (!FileExist(LOCATIONS_PATH))
        {
            Print("[SSCollector] SSCNavigator: no locations file at " + LOCATIONS_PATH);
            return;
        }

        SSCLocationList list = new SSCLocationList();
        string error;
        if (!JsonFileLoader<SSCLocationList>.LoadFile(LOCATIONS_PATH, list, error))
        {
            Print("[SSCollector] SSCNavigator: failed to parse locations.json — " + error);
            return;
        }

        if (list.locations)
            s_Locations = list.locations;

        Print("[SSCollector] SSCNavigator: loaded " + s_Locations.Count() + " location(s).");
    }

    static int GetCount()
    {
        if (!s_Locations)
            return 0;
        return s_Locations.Count();
    }

    static SSCLocation GetAt(int index)
    {
        if (!s_Locations || index < 0 || index >= s_Locations.Count())
            return null;
        return s_Locations[index];
    }

    static void AppendLocation(notnull SSCLocation loc)
    {
        if (!s_Locations)
            s_Locations = new array<ref SSCLocation>();
        s_Locations.Insert(loc);
    }

    static void ClearLocations()
    {
        if (!s_Locations)
            s_Locations = new array<ref SSCLocation>();
        else
            s_Locations.Clear();
    }

    static void Save()
    {
        SSCLocationList list = new SSCLocationList();
        list.locations = s_Locations;

        string error;
        if (!JsonFileLoader<SSCLocationList>.SaveFile(LOCATIONS_PATH, list, error))
            Print("[SSCollector] SSCNavigator: save failed — " + error);
        else
            Print("[SSCollector] SSCNavigator: saved " + s_Locations.Count() + " location(s).");
    }

    // Returns true if floorY (from SurfaceRoadY) is significantly above raw terrain,
    // indicating the point is on a building rooftop or tall prop rather than ground level.
    // Roads sit within ~0.3 m of terrain, so the 2 m threshold avoids false positives on roads.
    static const float ROOF_THRESHOLD = 2.0;

    static bool IsOnRoof(float x, float floorY, float z)
    {
        float terrainY = GetGame().SurfaceY(x, z);
        return (floorY - terrainY) > ROOF_THRESHOLD;
    }

    // Returns false if the angle is blocked within minClear meters (facing a wall/cliff).
    static bool IsClearAngle(vector eyePos, float yawDeg, float pitchDeg, float maxDist, float minClear, Object ignore)
    {
        float yawRad   = yawDeg   * Math.DEG2RAD;
        float pitchRad = pitchDeg * Math.DEG2RAD;

        // DayZ convention: X = East = Sin(yaw), Z = North = Cos(yaw), Y = Sin(pitch)
        vector fwd = Vector(Math.Sin(yawRad) * Math.Cos(pitchRad), Math.Sin(pitchRad), Math.Cos(yawRad) * Math.Cos(pitchRad));

        PhxInteractionLayers hitMask = PhxInteractionLayers.BUILDING | PhxInteractionLayers.TERRAIN | PhxInteractionLayers.ROADWAY;

        Object hitObj;
        vector hitPos, hitNormal;
        float hitFraction;
        bool hit = DayZPhysics.RayCastBullet(eyePos, eyePos + fwd * maxDist, hitMask, ignore, hitObj, hitPos, hitNormal, hitFraction);

        return !hit || vector.Distance(eyePos, hitPos) >= minClear;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// PlayerBase additions
//   • m_SSCLocIndex     — current location index, per player, server-side
//   • SSC_Navigate()    — advance index, apply location, reply with camera RPC
//   • SSC_AddLocation() — generate entries at current position via raycast
//   • SSC_GenerateGrid()— generate entries across the full map grid
//   • SSC_ClearLocations() — wipe all saved locations
//   • OnRPC override    — dispatch all SSC RPCs
// ─────────────────────────────────────────────────────────────────────────────

modded class PlayerBase
{
    protected int  m_SSCLocIndex = -1;
    protected bool m_SSCGodMode  = false;

    // Time/weather presets used by both SSC_AddLocation and SSC_GenerateGrid.
    // No fog — it hides landmarks and hurts coordinate regression.
    // Preset order matters: generation loops preset-first so all entries for one
    // time are consecutive, meaning time only changes at group boundaries during
    // playback — avoids per-shot lighting transitions.
    // { pre-dawn, overcast-noon, dusk, night }
    // 5.5 = deep blue pre-dawn; 12 overcast = flat/shadowless; 19 = golden dusk; 22 = night
    static float s_pTime[4]     = {  5.5,  12.0,  19.0,  22.0 };
    static float s_pOvercast[4] = {  0.0,   0.7,   0.0,   0.0 };
    static float s_pFog[4]      = {  0.0,   0.0,   0.0,   0.0 };
    static float s_pRain[4]     = {  0.0,   0.0,   0.0,   0.0 };

    // Server-side: apply the location at `index` — set time/weather and fire
    // a SET_CAMERA RPC to this player's client. Returns false if invalid.
    bool SSC_ApplyLocation(int index)
    {
        SSCLocation loc = SSCNavigator.GetAt(index);
        if (!loc)
        {
            Print("[SSCollector] SSC_ApplyLocation: null entry at index " + index);
            return false;
        }

        // ── Teleport ─────────────────────────────────────────────────────────
        // FreeDebugCamera is positioned independently on the client, so the player
        // does not need to be at the shot location. Teleporting would put the character
        // model inside the camera frustum and clip the shot. The player must simply be
        // within terrain-streaming range (~1 km) of the locations they intend to shoot.
        // (no-op: player position unchanged)

        // ── Weather (server-wide, instantaneous, held for 1 hour) ────────────
        if (loc.weather)
        {
            Weather w = GetGame().GetWeather();
            w.MissionWeather(true);
            w.GetOvercast().Set(loc.weather.overcast, 0, 3600);
            w.GetFog().Set(loc.weather.fog, 0, 3600);
            w.GetRain().Set(loc.weather.rain, 0, 3600);
        }

        // ── Time of day ───────────────────────────────────────────────────────
        // Keep current calendar date; only replace hour + minute.
        int year, month, day, hour, minute;
        GetGame().GetWorld().GetDate(year, month, day, hour, minute);
        int newHour   = (int)loc.timeOfDay;
        int newMinute = (int)((loc.timeOfDay - newHour) * 60.0);
        GetGame().GetWorld().SetDate(year, month, day, newHour, newMinute);

        // ── Camera orientation → client ───────────────────────────────────────
        // Send eye position (floor + 1.5 m) so the client can place FreeDebugCamera
        // at the exact spot without depending on position-replication timing.
        float eyeX = 0, eyeY = 1.5, eyeZ = 0;
        if (loc.position)
        {
            eyeX = loc.position.x;
            eyeY = loc.position.y + 1.5;
            eyeZ = loc.position.z;
        }
        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(eyeX);
        rpc.Write(eyeY);
        rpc.Write(eyeZ);
        rpc.Write(loc.cameraYaw);
        rpc.Write(loc.cameraPitch);
        rpc.Send(this, SSCRpc.SET_CAMERA, true, GetIdentity());

        string posStr = "?";
        if (loc.position)
            posStr = string.Format("(%1, %2, %3)", loc.position.x, loc.position.y, loc.position.z);

        Print(string.Format("[SSCollector] SSC_ApplyLocation: idx=%1/%2  pos=%3  time=%4  yaw=%5  pitch=%6",
            index, SSCNavigator.GetCount() - 1, posStr, loc.timeOfDay, loc.cameraYaw, loc.cameraPitch));
        return true;
    }

    // Server-side: step the index by `delta` (+1 next / -1 prev), apply the
    // resulting location, and fire a SET_CAMERA reply to this player's client.
    void SSC_Navigate(int delta)
    {
        int count = SSCNavigator.GetCount();
        if (count == 0)
        {
            Print("[SSCollector] SSC_Navigate: no locations loaded — drop.");
            return;
        }

        // First press always lands on index 0 regardless of direction.
        if (m_SSCLocIndex < 0)
            m_SSCLocIndex = 0;
        else
            m_SSCLocIndex = (m_SSCLocIndex + delta + count) % count;

        SSC_ApplyLocation(m_SSCLocIndex);
    }

    // Server-side: jump directly to `index`, clamp to valid range, apply location.
    void SSC_GoTo(int index)
    {
        int count = SSCNavigator.GetCount();
        if (count == 0)
        {
            Print("[SSCollector] SSC_GoTo: no locations loaded — drop.");
            return;
        }

        if (index < 0) index = 0;
        if (index >= count) index = count - 1;

        m_SSCLocIndex = index;
        SSC_ApplyLocation(m_SSCLocIndex);
    }

    // Server-side: generate location entries at the player's current position.
    // Samples yawCount evenly-spaced yaw angles, rejects angles with less than
    // 5m forward clearance (wall-facing), writes 4 preset entries per valid angle.
    void SSC_AddLocation(int yawCount)
    {
        vector rawPos  = GetPosition();
        float  floorY  = GetGame().SurfaceRoadY(rawPos[0], rawPos[2]);
        vector snapPos = Vector(rawPos[0], floorY, rawPos[2]);
        vector eyePos  = Vector(rawPos[0], floorY + 1.5, rawPos[2]);

        float pitchDeg = -5.0;
        float yawStep  = 360.0 / yawCount;
        int   added    = 0;

        // Preset is the outer loop so all entries for a given time are consecutive.
        // During playback, time only changes at group boundaries — no per-shot transitions.
        for (int p = 0; p < 4; p++)
        {
            for (int i = 0; i < yawCount; i++)
            {
                float yawDeg = yawStep * i;
                if (!SSCNavigator.IsClearAngle(eyePos, yawDeg, pitchDeg, 100.0, 5.0, this))
                    continue;

                SSCLocation loc      = new SSCLocation();
                loc.position         = new SSCLocationPos();
                loc.position.x       = snapPos[0];
                loc.position.y       = floorY;
                loc.position.z       = snapPos[2];
                loc.cameraYaw        = yawDeg;
                loc.cameraPitch      = pitchDeg;
                loc.timeOfDay        = s_pTime[p];
                loc.weather          = new SSCLocationWeather();
                loc.weather.overcast = s_pOvercast[p];
                loc.weather.fog      = s_pFog[p];
                loc.weather.rain     = s_pRain[p];
                SSCNavigator.AppendLocation(loc);
                added++;
            }
        }

        SSCNavigator.Save();
        Print(string.Format("[SSCollector] SSC_AddLocation: +%1 entries (yaw=%2, total=%3)",
            added, yawCount, SSCNavigator.GetCount()));
    }

    // Server-side: sweep a uniform grid over the map bounds from SSCConfig,
    // applying the same raycast filter and preset expansion as SSC_AddLocation.
    // Scale: step=500 on Chernarus ≈ 900 land points → up to ~28k entries.
    // noRoof: skip points where SurfaceRoadY exceeds raw terrain height by more than
    //         SSCNavigator.ROOF_THRESHOLD (2 m) — filters rooftops and tall props.
    void SSC_GenerateGrid(int step, int yawCount, bool noRoof = false)
    {
        SSCConfig cfg  = SSCConfigManager.Get();
        float minX     = cfg.mapMinX;
        float maxX     = cfg.mapMaxX;
        float minZ     = cfg.mapMinZ;
        float maxZ     = cfg.mapMaxZ;

        float pitchDeg = -5.0;
        float yawStep  = 360.0 / yawCount;
        int   added    = 0;
        int   seaSkip  = 0;
        int   roofSkip = 0;

        // Preset is the outer loop so all entries for a given time are consecutive.
        // During playback, time only changes at group boundaries — no per-shot transitions.
        for (int p = 0; p < 4; p++)
        {
            for (float x = minX; x <= maxX; x += step)
            {
                for (float z = minZ; z <= maxZ; z += step)
                {
                    if (GetGame().SurfaceIsSea(x, z))
                    {
                        if (p == 0) seaSkip++; // count once
                        continue;
                    }

                    float  floorY  = GetGame().SurfaceRoadY(x, z);

                    if (noRoof && SSCNavigator.IsOnRoof(x, floorY, z))
                    {
                        if (p == 0) roofSkip++; // count once
                        continue;
                    }

                    vector snapPos = Vector(x, floorY, z);
                    vector eyePos  = Vector(x, floorY + 1.5, z);

                    for (int i = 0; i < yawCount; i++)
                    {
                        float yawDeg = yawStep * i;
                        if (!SSCNavigator.IsClearAngle(eyePos, yawDeg, pitchDeg, 100.0, 5.0, this))
                            continue;

                        SSCLocation loc      = new SSCLocation();
                        loc.position         = new SSCLocationPos();
                        loc.position.x       = snapPos[0];
                        loc.position.y       = floorY;
                        loc.position.z       = snapPos[2];
                        loc.cameraYaw        = yawDeg;
                        loc.cameraPitch      = pitchDeg;
                        loc.timeOfDay        = s_pTime[p];
                        loc.weather          = new SSCLocationWeather();
                        loc.weather.overcast = s_pOvercast[p];
                        loc.weather.fog      = s_pFog[p];
                        loc.weather.rain     = s_pRain[p];
                        SSCNavigator.AppendLocation(loc);
                        added++;
                    }
                }
            }
        }

        SSCNavigator.Save();
        Print(string.Format("[SSCollector] SSC_GenerateGrid: +%1 entries (step=%2, yaw=%3, seaSkip=%4, roofSkip=%5, total=%6)",
            added, step, yawCount, seaSkip, roofSkip, SSCNavigator.GetCount()));
    }

    // Server-side: scatter `pointCount` random locations inside a circle of `radius` meters
    // centred at (centerX, centerZ). Pitches are spread evenly from PITCH_MIN to PITCH_MAX
    // across pitchCount steps; with pitchCount=1 a single -5° pitch is used.
    // Loop order: preset → pitch → point → yaw, so time changes only 4 times during playback.
    static const float PITCH_MIN = -15.0;
    static const float PITCH_MAX =  10.0;

    void SSC_DenseArea(float centerX, float centerZ, float radius, int pointCount, int yawCount, int pitchCount, bool noRoof)
    {
        float yawStep  = 360.0 / yawCount;
        int   added    = 0;
        int   attempts = 0;
        int   maxAttempts = pointCount * 8;
        float r, angle, x, z, floorY, pitchStep, pitchDeg, yawDeg;
        int   pi, p, pIdx, vi, i;
        vector snapPos, eyePos;

        // Build pitch array
        array<float> pitches = new array<float>();
        if (pitchCount <= 1)
        {
            pitches.Insert(-5.0);
        }
        else
        {
            pitchStep = (PITCH_MAX - PITCH_MIN) / (pitchCount - 1);
            for (pi = 0; pi < pitchCount; pi++)
                pitches.Insert(PITCH_MIN + pitchStep * pi);
        }

        // Generate the random point set first so preset loop runs over a fixed list.
        array<vector> validPoints = new array<vector>();

        while (validPoints.Count() < pointCount && attempts < maxAttempts)
        {
            attempts++;

            // Uniform distribution in a circle: r = radius * sqrt(U[0,1])
            r     = radius * Math.Sqrt(Math.RandomFloat(0, 1));
            angle = Math.RandomFloat(0, Math.PI * 2);
            x     = centerX + r * Math.Sin(angle);
            z     = centerZ + r * Math.Cos(angle);

            if (GetGame().SurfaceIsSea(x, z))
                continue;

            floorY = GetGame().SurfaceRoadY(x, z);

            if (noRoof && SSCNavigator.IsOnRoof(x, floorY, z))
                continue;

            validPoints.Insert(Vector(x, floorY, z));
        }

        // Preset outer loop keeps consecutive time grouping during playback
        for (p = 0; p < 4; p++)
        {
            for (pIdx = 0; pIdx < pitches.Count(); pIdx++)
            {
                pitchDeg = pitches[pIdx];

                for (vi = 0; vi < validPoints.Count(); vi++)
                {
                    snapPos = validPoints[vi];
                    floorY  = snapPos[1];
                    eyePos  = Vector(snapPos[0], floorY + 1.5, snapPos[2]);

                    for (i = 0; i < yawCount; i++)
                    {
                        yawDeg = yawStep * i;
                        if (!SSCNavigator.IsClearAngle(eyePos, yawDeg, pitchDeg, 100.0, 5.0, this))
                            continue;

                        SSCLocation loc      = new SSCLocation();
                        loc.position         = new SSCLocationPos();
                        loc.position.x       = snapPos[0];
                        loc.position.y       = floorY;
                        loc.position.z       = snapPos[2];
                        loc.cameraYaw        = yawDeg;
                        loc.cameraPitch      = pitchDeg;
                        loc.timeOfDay        = s_pTime[p];
                        loc.weather          = new SSCLocationWeather();
                        loc.weather.overcast = s_pOvercast[p];
                        loc.weather.fog      = s_pFog[p];
                        loc.weather.rain     = s_pRain[p];
                        SSCNavigator.AppendLocation(loc);
                        added++;
                    }
                }
            }
        }

        SSCNavigator.Save();
        Print(string.Format("[SSCollector] SSC_DenseArea: +%1 entries (r=%2, points=%3/%4, yaw=%5, pitches=%6, total=%7)",
            added, radius, validPoints.Count(), pointCount, yawCount, pitches.Count(), SSCNavigator.GetCount()));
    }

    // Server-side: re-read locations.json into memory (hot-reload without restart).
    void SSC_Reload()
    {
        SSCNavigator.Init();
        Print("[SSCollector] SSC_Reload: " + SSCNavigator.GetCount() + " location(s) loaded.");
    }

    // Server-side: toggle god mode — blocks damage, pauses modifiers, tops up all stats.
    void SSC_ToggleGod()
    {
        m_SSCGodMode = !m_SSCGodMode;
        SetAllowDamage(!m_SSCGodMode);

        ModifiersManager modMgr = GetModifiersManager();
        if (modMgr)
        {
            if (m_SSCGodMode)
            {
                modMgr.SetModifiers(false);
                modMgr.ResetAll();
            }
            else
            {
                modMgr.SetModifiers(true);
            }
        }

        if (m_SSCGodMode)
        {
            SetHealth("", "Health", GetMaxHealth("", "Health"));
            SetHealth("", "Blood",  GetMaxHealth("", "Blood"));
            SetHealth("", "Shock",  GetMaxHealth("", "Shock"));

            if (GetStatWater())
                GetStatWater().Set(GetStatWater().GetMax());
            if (GetStatEnergy())
                GetStatEnergy().Set(GetStatEnergy().GetMax());

            BleedingSourcesManagerServer bleedMgr = GetBleedingManagerServer();
            if (bleedMgr)
                bleedMgr.RemoveAllSources();
        }

        Print("[SSCollector] God mode: " + m_SSCGodMode);
    }

    // Server-side: teleport player to the map's northwest corner (config minX/minZ + 200m)
    // so they are far from populated areas and won't spawn infected during a capture run.
    void SSC_Exile()
    {
        SSCConfig cfg = SSCConfigManager.Get();
        float x = cfg.mapMinX + 200;
        float z = cfg.mapMinZ + 200;
        float y = GetGame().SurfaceRoadY(x, z);
        SetPosition(Vector(x, y, z));
        Print(string.Format("[SSCollector] SSC_Exile: teleported to (%1, %2, %3)", x, y, z));
    }

    // Server-side: wipe all saved locations and persist the empty list.
    void SSC_ClearLocations()
    {
        SSCNavigator.ClearLocations();
        SSCNavigator.Save();
        Print("[SSCollector] SSC_ClearLocations: all locations cleared.");
    }

    override void OnRPC(PlayerIdentity sender, int rpc_type, ParamsReadContext ctx)
    {
        super.OnRPC(sender, rpc_type, ctx);

        // ── NAVIGATE  (client → server) ───────────────────────────────────────
        if (rpc_type == SSCRpc.NAVIGATE)
        {
            if (!GetGame().IsServer())
                return;

            int delta;
            if (!ctx.Read(delta))
            {
                Print("[SSCollector] OnRPC NAVIGATE: could not read delta");
                return;
            }
            SSC_Navigate(delta);
            return;
        }

        // ── SET_CAMERA  (server → client) ─────────────────────────────────────
        if (rpc_type == SSCRpc.SET_CAMERA)
        {
            if (GetGame().IsServer())
                return;

            float camX, camY, camZ, camYaw, camPitch;
            if (!ctx.Read(camX) || !ctx.Read(camY) || !ctx.Read(camZ) || !ctx.Read(camYaw) || !ctx.Read(camPitch))
            {
                Print("[SSCollector] OnRPC SET_CAMERA: could not read payload");
                return;
            }
            // Use FreeDebugCamera for exact position+orientation control.
            // SetOrientation on PlayerBase only affects the entity transform and
            // does not update the engine's internal aim accumulator, causing flicker.
            // FreeDebugCamera lives in 5_Mission and is not accessible here.
            // Pass the data through the static SSCCameraCommand; MissionGameplay picks it up.
            SSCCameraCommand.x       = camX;
            SSCCameraCommand.y       = camY;
            SSCCameraCommand.z       = camZ;
            SSCCameraCommand.yaw     = camYaw;
            SSCCameraCommand.pitch   = camPitch;
            SSCCameraCommand.pending = true;
            return;
        }

        // ── ADD_LOCATION  (client → server) ──────────────────────────────────
        if (rpc_type == SSCRpc.ADD_LOCATION)
        {
            if (!GetGame().IsServer())
                return;

            int yawCount;
            if (!ctx.Read(yawCount))
            {
                Print("[SSCollector] OnRPC ADD_LOCATION: could not read yawCount");
                return;
            }
            if (yawCount < 1 || yawCount > 72)
            {
                Print("[SSCollector] OnRPC ADD_LOCATION: bad yawCount=" + yawCount);
                return;
            }
            SSC_AddLocation(yawCount);
            return;
        }

        // ── CLEAR_LOCATIONS  (client → server) ───────────────────────────────
        if (rpc_type == SSCRpc.CLEAR_LOCATIONS)
        {
            if (!GetGame().IsServer())
                return;

            SSC_ClearLocations();
            return;
        }

        // ── GENERATE_GRID  (client → server) ─────────────────────────────────
        if (rpc_type == SSCRpc.GENERATE_GRID)
        {
            if (!GetGame().IsServer())
                return;

            int gridStep, gridYawCount, gridNoRoof;
            if (!ctx.Read(gridStep) || !ctx.Read(gridYawCount) || !ctx.Read(gridNoRoof))
            {
                Print("[SSCollector] OnRPC GENERATE_GRID: could not read params");
                return;
            }
            if (gridStep < 50 || gridStep > 5000 || gridYawCount < 1 || gridYawCount > 72)
            {
                Print("[SSCollector] OnRPC GENERATE_GRID: bad params step=" + gridStep + " yaw=" + gridYawCount);
                return;
            }
            SSC_GenerateGrid(gridStep, gridYawCount, gridNoRoof != 0);
            return;
        }

        // ── RELOAD  (client → server) ─────────────────────────────────────────
        if (rpc_type == SSCRpc.RELOAD)
        {
            if (!GetGame().IsServer())
                return;

            SSC_Reload();
            return;
        }

        // ── TOGGLE_GOD  (client → server) ────────────────────────────────────
        if (rpc_type == SSCRpc.TOGGLE_GOD)
        {
            if (!GetGame().IsServer())
                return;

            SSC_ToggleGod();
            return;
        }

        // ── DENSE_AREA  (client → server) ────────────────────────────────────
        if (rpc_type == SSCRpc.DENSE_AREA)
        {
            if (!GetGame().IsServer())
                return;

            float daCenterX, daCenterZ, daRadius;
            int   daPointCount, daYawCount, daPitchCount, daNoRoof;
            if (!ctx.Read(daCenterX) || !ctx.Read(daCenterZ) || !ctx.Read(daRadius))
            {
                Print("[SSCollector] OnRPC DENSE_AREA: could not read center/radius");
                return;
            }
            if (!ctx.Read(daPointCount) || !ctx.Read(daYawCount) || !ctx.Read(daPitchCount) || !ctx.Read(daNoRoof))
            {
                Print("[SSCollector] OnRPC DENSE_AREA: could not read counts");
                return;
            }
            if (daRadius <= 0 || daRadius > 5000 || daPointCount < 1 || daPointCount > 500)
            {
                Print("[SSCollector] OnRPC DENSE_AREA: bad params");
                return;
            }
            if (daYawCount < 1 || daYawCount > 72 || daPitchCount < 1 || daPitchCount > 8)
            {
                Print("[SSCollector] OnRPC DENSE_AREA: bad params");
                return;
            }
            SSC_DenseArea(daCenterX, daCenterZ, daRadius, daPointCount, daYawCount, daPitchCount, daNoRoof != 0);
            return;
        }

        // ── EXILE  (client → server) ─────────────────────────────────────────
        if (rpc_type == SSCRpc.EXILE)
        {
            if (!GetGame().IsServer())
                return;

            SSC_Exile();
            return;
        }

        // ── SET_INDEX  (client → server) ─────────────────────────────────────
        if (rpc_type == SSCRpc.SET_INDEX)
        {
            if (!GetGame().IsServer())
                return;

            int gotoIndex;
            if (!ctx.Read(gotoIndex))
            {
                Print("[SSCollector] OnRPC SET_INDEX: could not read index");
                return;
            }
            SSC_GoTo(gotoIndex);
            return;
        }
    }
}
