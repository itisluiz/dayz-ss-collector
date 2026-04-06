modded class MissionServer
{
    override void OnInit()
    {
        super.OnInit();
        SSCConfigManager.Init();
        SSCNavigator.Init();
        Print("[SSCollector] Server mission initialized.");
    }
};
