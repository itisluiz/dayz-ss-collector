modded class MissionGameplay
{
    override void OnInit()
    {
        super.OnInit();
        Print("[SSCollector] MissionGameplay initialized on client.");
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

        Print("[SSCollector] /ss-meta sent. cameraDir=" + cameraDir);
    }
}
