modded class MissionGameplay
{
    protected UAInput m_SSCCaptureInput;
    protected UAInput m_SSCPrevInput;
    protected UAInput m_SSCNextInput;

    override void OnInit()
    {
        super.OnInit();

        m_SSCCaptureInput = GetUApi().GetInputByName("UASSCCaptureMeta");
        m_SSCPrevInput    = GetUApi().GetInputByName("UASSCPrevLocation");
        m_SSCNextInput    = GetUApi().GetInputByName("UASSCNextLocation");

        if (m_SSCCaptureInput)
            Print("[SSCollector] UASSCCaptureMeta input registered OK.");
        else
            Print("[SSCollector] UASSCCaptureMeta input NOT FOUND - inputs.xml not loaded!");

        if (m_SSCPrevInput)
            Print("[SSCollector] UASSCPrevLocation input registered OK.");
        else
            Print("[SSCollector] UASSCPrevLocation input NOT FOUND.");

        if (m_SSCNextInput)
            Print("[SSCollector] UASSCNextLocation input registered OK.");
        else
            Print("[SSCollector] UASSCNextLocation input NOT FOUND.");

        Print("[SSCollector] MissionGameplay initialized on client.");
    }

    override void OnUpdate(float timeslice)
    {
        super.OnUpdate(timeslice);

        if (m_SSCCaptureInput && m_SSCCaptureInput.LocalPress())
            SendCaptureMeta();

        if (m_SSCPrevInput && m_SSCPrevInput.LocalPress())
            SendNavigate(-1);

        if (m_SSCNextInput && m_SSCNextInput.LocalPress())
            SendNavigate(1);

        if (SSCCameraCommand.pending)
        {
            SSCCameraCommand.pending = false;
            FreeDebugCamera cam = FreeDebugCamera.GetInstance();
            if (cam)
            {
                cam.SetPosition(Vector(SSCCameraCommand.x, SSCCameraCommand.y, SSCCameraCommand.z));
                cam.SetOrientation(Vector(SSCCameraCommand.yaw, SSCCameraCommand.pitch, 0));
                cam.SetActive(true);
            }
        }
    }

    override void OnEvent(EventType eventTypeId, Param params)
    {
        // Intercept SSC commands BEFORE calling super so they never reach the HUD
        // chat widget. super.OnEvent() is what renders the message on screen.
        if (eventTypeId == ChatMessageEventTypeID)
        {
            Param4<int, string, string, string> chatParams;
            if (Class.CastTo(chatParams, params))
            {
                string msg = chatParams.param3;
                msg.ToLower();

                if (msg.IndexOf("/ss-") == 0)
                {
                    HandleSSCCommand(msg);
                    return;  // swallow — do NOT call super, stays off HUD
                }
            }
        }

        super.OnEvent(eventTypeId, params);
    }

    // Dispatch an already-lowercased /ss-* command string.
    protected void HandleSSCCommand(string msg)
    {
        // Hoisted locals — Enforce Script requires all declarations at function scope.
        string args, cleaned, numStr, stepStr, yawStr, idxStr, remaining;
        int flagIdx, spaceIdx, parsed, parsedYaw, parsedPitch;
        int step, genYawCount, addYawCount, gotoIdx, spIdx;
        int denseCount, denseYaw, densePitch;
        float denseRadius, centerX, centerZ, camX, camY, camZ;
        bool noRoof;
        array<string> tokens;
        vector camPos, playerPos;
        FreeDebugCamera cam;
        PlayerBase localPlayer;

        if (msg == "/ss-meta")
        {
            SendCaptureMeta();
            return;
        }

        // /ss-add [N]  — N is optional, defaults to 8
        if (msg == "/ss-add" || msg.IndexOf("/ss-add ") == 0)
        {
            addYawCount = 8;
            if (msg.Length() > 8)
            {
                numStr = msg.Substring(8, msg.Length() - 8);
                numStr.TrimInPlace();
                parsed = numStr.ToInt();
                if (parsed > 0)
                    addYawCount = parsed;
            }
            SendAddLocation(addYawCount);
            return;
        }

        // /ss-generate <step> [N] [--no-roof]  — step is required, N defaults to 8
        if (msg.IndexOf("/ss-generate ") == 0)
        {
            args = msg.Substring(13, msg.Length() - 13);
            args.TrimInPlace();

            noRoof = args.IndexOf("--no-roof") >= 0;
            if (noRoof)
            {
                flagIdx = args.IndexOf("--no-roof");
                cleaned = args.Substring(0, flagIdx);
                cleaned.TrimInPlace();
                args = cleaned;
            }

            spaceIdx = args.IndexOf(" ");
            step = 0;
            genYawCount = 8;

            if (spaceIdx < 0)
            {
                step = args.ToInt();
            }
            else
            {
                stepStr = args.Substring(0, spaceIdx);
                yawStr  = args.Substring(spaceIdx + 1, args.Length() - spaceIdx - 1);
                yawStr.TrimInPlace();
                step = stepStr.ToInt();
                parsedYaw = yawStr.ToInt();
                if (parsedYaw > 0)
                    genYawCount = parsedYaw;
            }

            if (step > 0)
                SendGenerateGrid(step, genYawCount, noRoof);
            else
                Print("[SSCollector] /ss-generate: missing or invalid step. Usage: /ss-generate <step> [yaw_count] [--no-roof]");

            return;
        }

        if (msg == "/ss-clear")
        {
            SendClearLocations();
            return;
        }

        if (msg == "/ss-reload")
        {
            SendReload();
            return;
        }

        if (msg == "/ss-god")
        {
            SendToggleGod();
            return;
        }

        if (msg == "/ss-exile")
        {
            SendExile();
            return;
        }

        // /ss-dense <radius> <count> [yaw_count] [pitch_count] [--no-roof]
        // Center: freecam position if active, else player position.
        // Pitches default to 3 (spread -15° to +10°); --no-roof is on by default.
        if (msg.IndexOf("/ss-dense ") == 0)
        {
            args = msg.Substring(10, msg.Length() - 10);
            args.TrimInPlace();

            noRoof = true;
            if (args.IndexOf("--no-roof") >= 0)
            {
                flagIdx = args.IndexOf("--no-roof");
                cleaned = args.Substring(0, flagIdx);
                cleaned.TrimInPlace();
                args = cleaned;
            }

            tokens = new array<string>();
            remaining = args;
            while (remaining.Length() > 0)
            {
                spIdx = remaining.IndexOf(" ");
                if (spIdx < 0)
                {
                    tokens.Insert(remaining);
                    break;
                }
                tokens.Insert(remaining.Substring(0, spIdx));
                remaining = remaining.Substring(spIdx + 1, remaining.Length() - spIdx - 1);
                remaining.TrimInPlace();
            }

            if (tokens.Count() < 2)
            {
                Print("[SSCollector] /ss-dense: Usage: /ss-dense <radius> <count> [yaw_count] [pitch_count] [--no-roof]");
                return;
            }

            denseRadius = tokens[0].ToFloat();
            denseCount  = tokens[1].ToInt();
            denseYaw    = 8;
            densePitch  = 3;

            if (tokens.Count() >= 3)
            {
                parsedYaw = tokens[2].ToInt();
                if (parsedYaw > 0)
                    denseYaw = parsedYaw;
            }
            if (tokens.Count() >= 4)
            {
                parsedPitch = tokens[3].ToInt();
                if (parsedPitch > 0)
                    densePitch = parsedPitch;
            }

            if (denseRadius <= 0 || denseCount <= 0)
            {
                Print("[SSCollector] /ss-dense: invalid radius or count.");
                return;
            }

            cam = FreeDebugCamera.GetInstance();
            if (cam && cam.IsActive())
            {
                camPos  = cam.GetPosition();
                centerX = camPos[0];
                centerZ = camPos[2];
            }
            else
            {
                localPlayer = PlayerBase.Cast(GetGame().GetPlayer());
                if (!localPlayer)
                    return;
                playerPos = localPlayer.GetPosition();
                centerX   = playerPos[0];
                centerZ   = playerPos[2];
            }

            SendDenseArea(centerX, centerZ, denseRadius, denseCount, denseYaw, densePitch, noRoof);
            return;
        }

        if (msg == "/ss-freecam")
        {
            cam = FreeDebugCamera.GetInstance();
            if (cam)
                cam.SetActive(!cam.IsActive());
            return;
        }

        // /ss-cam <x> <z>        — move freecam to x, z at SurfaceRoadY + 5 m
        // /ss-cam <x> <y> <z>    — move freecam to exact x, y, z
        if (msg.IndexOf("/ss-cam ") == 0)
        {
            args = msg.Substring(8, msg.Length() - 8);
            args.TrimInPlace();

            tokens = new array<string>();
            remaining = args;
            while (remaining.Length() > 0)
            {
                spIdx = remaining.IndexOf(" ");
                if (spIdx < 0)
                {
                    tokens.Insert(remaining);
                    break;
                }
                tokens.Insert(remaining.Substring(0, spIdx));
                remaining = remaining.Substring(spIdx + 1, remaining.Length() - spIdx - 1);
                remaining.TrimInPlace();
            }

            if (tokens.Count() == 2)
            {
                camX = tokens[0].ToFloat();
                camZ = tokens[1].ToFloat();
                camY = GetGame().SurfaceRoadY(camX, camZ) + 5.0;
            }
            else if (tokens.Count() >= 3)
            {
                camX = tokens[0].ToFloat();
                camY = tokens[1].ToFloat();
                camZ = tokens[2].ToFloat();
            }
            else
            {
                Print("[SSCollector] /ss-cam: Usage: /ss-cam <x> <z>  or  /ss-cam <x> <y> <z>");
                return;
            }

            cam = FreeDebugCamera.GetInstance();
            if (!cam)
            {
                Print("[SSCollector] /ss-cam: FreeDebugCamera not available.");
                return;
            }
            cam.SetPosition(Vector(camX, camY, camZ));
            cam.SetActive(true);
            Print(string.Format("[SSCollector] /ss-cam: moved to (%1, %2, %3)", camX, camY, camZ));
            return;
        }

        // /ss-goto <N>  — jump directly to location index N
        if (msg.IndexOf("/ss-goto ") == 0)
        {
            idxStr = msg.Substring(9, msg.Length() - 9);
            idxStr.TrimInPlace();
            gotoIdx = idxStr.ToInt();
            if (gotoIdx >= 0)
                SendSetIndex(gotoIdx);
            else
                Print("[SSCollector] /ss-goto: invalid index. Usage: /ss-goto <N>");
            return;
        }
    }

    protected void SendCaptureMeta()
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        vector cameraDir = GetGame().GetCurrentCameraDirection();

        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(cameraDir);
        rpc.Send(player, SSCRpc.CAPTURE_META, true, null);

        Print("[SSCollector] Capture meta sent. cameraDir=" + cameraDir);
    }

    // delta: +1 = next location, -1 = previous location.
    protected void SendNavigate(int delta)
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(delta);
        rpc.Send(player, SSCRpc.NAVIGATE, true, null);

        Print("[SSCollector] Navigate sent. delta=" + delta);
    }

    protected void SendAddLocation(int yawCount)
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(yawCount);
        rpc.Send(player, SSCRpc.ADD_LOCATION, true, null);

        Print("[SSCollector] Add location sent. yawCount=" + yawCount);
    }

    protected void SendClearLocations()
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Send(player, SSCRpc.CLEAR_LOCATIONS, true, null);

        Print("[SSCollector] Clear locations sent.");
    }

    protected void SendGenerateGrid(int step, int yawCount, bool noRoof = false)
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        int noRoofInt = 0;
        if (noRoof)
            noRoofInt = 1;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(step);
        rpc.Write(yawCount);
        rpc.Write(noRoofInt);
        rpc.Send(player, SSCRpc.GENERATE_GRID, true, null);

        Print("[SSCollector] Generate grid sent. step=" + step + " yawCount=" + yawCount + " noRoof=" + noRoof);
    }

    protected void SendReload()
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Send(player, SSCRpc.RELOAD, true, null);

        Print("[SSCollector] Reload sent.");
    }

    protected void SendToggleGod()
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Send(player, SSCRpc.TOGGLE_GOD, true, null);

        Print("[SSCollector] Toggle god sent.");
    }

    protected void SendExile()
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Send(player, SSCRpc.EXILE, true, null);

        Print("[SSCollector] Exile sent.");
    }

    protected void SendDenseArea(float centerX, float centerZ, float radius, int pointCount, int yawCount, int pitchCount, bool noRoof)
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        int noRoofInt = 0;
        if (noRoof)
            noRoofInt = 1;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(centerX);
        rpc.Write(centerZ);
        rpc.Write(radius);
        rpc.Write(pointCount);
        rpc.Write(yawCount);
        rpc.Write(pitchCount);
        rpc.Write(noRoofInt);
        rpc.Send(player, SSCRpc.DENSE_AREA, true, null);

        Print(string.Format("[SSCollector] Dense area sent. center=(%1, %2) r=%3 count=%4 yaw=%5 pitch=%6 noRoof=%7",
            centerX, centerZ, radius, pointCount, yawCount, pitchCount, noRoof));
    }

    protected void SendSetIndex(int index)
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(index);
        rpc.Send(player, SSCRpc.SET_INDEX, true, null);

        Print("[SSCollector] Set index sent. index=" + index);
    }
}
