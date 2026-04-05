class CfgPatches
{
    class SSCollector_Scripts
    {
        units[] = {};
        weapons[] = {};
        requiredVersion = 0.1;
        requiredAddons[] =
        {
            "DZ_Data",
            "DZ_Scripts"
        };
    };
};

class CfgMods
{
    class SSCollector
    {
        dir = "dayz-ss-collector";
        name = "DayZ SS Collector";
        author = "itisluiz";
        type = "mod";
        dependencies[] = { "Game", "World", "Mission" };

        class defs
        {
            class gameScriptModule
            {
                value = "";
                files[] = { "dayz-ss-collector/mod/Scripts/3_Game" };
            };
            class worldScriptModule
            {
                value = "";
                files[] = { "dayz-ss-collector/mod/Scripts/4_World" };
            };
            class missionScriptModule
            {
                value = "";
                files[] = { "dayz-ss-collector/mod/Scripts/5_Mission" };
            };
        };
    };
};
