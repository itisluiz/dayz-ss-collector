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
        if (msg == "/ss-meta")
        {
            SendCaptureMeta();
            return;
        }

        // /ss-add [N]  — N is optional, defaults to 8
        if (msg == "/ss-add" || msg.IndexOf("/ss-add ") == 0)
        {
            int addYawCount = 8;
            if (msg.Length() > 8)
            {
                string numStr = msg.Substring(8, msg.Length() - 8);
                numStr.TrimInPlace();
                int parsed = numStr.ToInt();
                if (parsed > 0)
                    addYawCount = parsed;
            }
            SendAddLocation(addYawCount);
            return;
        }

        // /ss-generate <step> [N]  — step is required, N defaults to 8
        if (msg.IndexOf("/ss-generate ") == 0)
        {
            string args = msg.Substring(13, msg.Length() - 13);
            args.TrimInPlace();

            int spaceIdx = args.IndexOf(" ");
            int step, genYawCount = 8;

            if (spaceIdx < 0)
            {
                step = args.ToInt();
            }
            else
            {
                string stepStr = args.Substring(0, spaceIdx);
                string yawStr  = args.Substring(spaceIdx + 1, args.Length() - spaceIdx - 1);
                yawStr.TrimInPlace();
                step = stepStr.ToInt();
                int parsedYaw = yawStr.ToInt();
                if (parsedYaw > 0)
                    genYawCount = parsedYaw;
            }

            if (step > 0)
                SendGenerateGrid(step, genYawCount);
            else
                Print("[SSCollector] /ss-generate: missing or invalid step. Usage: /ss-generate <step> [yaw_count]");

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

        if (msg == "/ss-freecam")
        {
            FreeDebugCamera cam = FreeDebugCamera.GetInstance();
            if (cam)
                cam.SetActive(!cam.IsActive());
            return;
        }

        // /ss-goto <N>  — jump directly to location index N
        if (msg.IndexOf("/ss-goto ") == 0)
        {
            string idxStr = msg.Substring(9, msg.Length() - 9);
            idxStr.TrimInPlace();
            int gotoIdx = idxStr.ToInt();
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

    protected void SendGenerateGrid(int step, int yawCount)
    {
        PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
        if (!player)
            return;

        ScriptRPC rpc = new ScriptRPC();
        rpc.Write(step);
        rpc.Write(yawCount);
        rpc.Send(player, SSCRpc.GENERATE_GRID, true, null);

        Print("[SSCollector] Generate grid sent. step=" + step + " yawCount=" + yawCount);
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
