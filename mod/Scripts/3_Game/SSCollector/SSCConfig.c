class SSCConfig
{
    // Subdirectory under $profile:SSCollector/ where meta files are written.
    // Example: "output" -> Documents\DayZ Other Profiles\Server\SSCollector\output\
    string outputDir = "output";

    // Playable area bounds used by /ss-generate.
    // Defaults cover Chernarus (~15360×15360m). Override in config.json for other maps.
    float mapMinX = 0.0;
    float mapMaxX = 15360.0;
    float mapMinZ = 0.0;
    float mapMaxZ = 15360.0;
}

class SSCConfigManager
{
    protected static const string CONFIG_PATH = "$profile:SSCollector/config.json";
    protected static ref SSCConfig m_Config;

    static void Init()
    {
        MakeDirectory("$profile:SSCollector");

        m_Config = new SSCConfig();

        if (!FileExist(CONFIG_PATH))
        {
            Save();
            Print("[SSCollector] Config created: " + CONFIG_PATH);
            return;
        }

        string error;
        if (!JsonFileLoader<SSCConfig>.LoadFile(CONFIG_PATH, m_Config, error))
        {
            Print("[SSCollector] Config load error: " + error + " - using defaults");
            m_Config = new SSCConfig();
        }

        Print("[SSCollector] Config loaded. outputDir=" + m_Config.outputDir);
    }

    static void Save()
    {
        string error;
        if (!JsonFileLoader<SSCConfig>.SaveFile(CONFIG_PATH, m_Config, error))
            Print("[SSCollector] Config save error: " + error);
    }

    static SSCConfig Get()
    {
        if (!m_Config)
            m_Config = new SSCConfig();
        return m_Config;
    }
}
