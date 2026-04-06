modded class MissionGameplay
{
    protected UAInput m_SSCCaptureInput;
    protected UAInput m_SSCPrevInput;
    protected UAInput m_SSCNextInput;

    protected bool  m_SSCOrientActive = false;
    protected float m_SSCOrientYaw    = 0;
    protected float m_SSCOrientPitch  = 0;

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
            m_SSCOrientYaw    = SSCCameraCommand.yaw;
            m_SSCOrientPitch  = SSCCameraCommand.pitch;
            m_SSCOrientActive = true;
        }

        if (m_SSCOrientActive)
        {
            PlayerBase player = PlayerBase.Cast(GetGame().GetPlayer());
            if (player)
                player.SetOrientation(Vector(m_SSCOrientYaw, m_SSCOrientPitch, 0));
        }
    }

    override void OnEvent(EventType eventTypeId, Param params)
    {
        super.OnEvent(eventTypeId, params);

        if (eventTypeId == ChatMessageEventTypeID)
        {
            Param4<int, string, string, string> chatParams;
            if (!Class.CastTo(chatParams, params))
                return;

            string msg = chatParams.param3;
            msg.ToLower();

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
}
