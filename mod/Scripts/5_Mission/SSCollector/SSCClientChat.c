modded class MissionGameplay
{
    protected UAInput m_SSCCaptureInput;

    override void OnInit()
    {
        super.OnInit();
        m_SSCCaptureInput = GetUApi().GetInputByName("UASSCCaptureMeta");
        if (m_SSCCaptureInput)
            Print("[SSCollector] UASSCCaptureMeta input registered OK.");
        else
            Print("[SSCollector] UASSCCaptureMeta input NOT FOUND - inputs.xml not loaded!");
        Print("[SSCollector] MissionGameplay initialized on client.");
    }

    override void OnUpdate(float timeslice)
    {
        super.OnUpdate(timeslice);

        if (m_SSCCaptureInput && m_SSCCaptureInput.LocalPress())
            SendCaptureMeta();
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
                SendCaptureMeta();
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
}
