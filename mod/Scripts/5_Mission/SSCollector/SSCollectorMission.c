modded class MissionServer
{
    override void OnInit()
    {
        super.OnInit();
        SSCConfigManager.Init();
        Print("[SSCollector] Server mission initialized.");
    }
};
