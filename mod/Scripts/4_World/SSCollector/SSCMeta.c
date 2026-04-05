class SSCMetaWriter
{
    static int s_Counter = 0;

    static void Write(PlayerBase player, vector cameraDir)
    {
        if (!player)
            return;

        SSCConfig cfg = SSCConfigManager.Get();
        string outDir = "$profile:SSCollector/" + cfg.outputDir;

        MakeDirectory("$profile:SSCollector");
        MakeDirectory(outDir);

        s_Counter++;
        string filename = outDir + "/ss-meta-" + s_Counter + ".json";

        vector pos = player.GetPosition();
        float tod = GetGame().GetDayTime();

        string worldName;
        GetGame().GetWorldName(worldName);

        Weather w = GetGame().GetWeather();
        float overcast  = w.GetOvercast().GetActual();
        float rain      = w.GetRain().GetActual();
        float fog       = w.GetFog().GetActual();
        float snowfall  = w.GetSnowfall().GetActual();
        float windSpeed = w.GetWindSpeed();

        FileHandle fh = OpenFile(filename, FileMode.WRITE);
        if (fh == 0)
        {
            Print("[SSCollector] ERROR: Could not open file for writing: " + filename);
            return;
        }

        FPrintln(fh, "{");
        FPrintln(fh, "    \"map\": \"" + worldName + "\",");
        FPrintln(fh, "    \"position\": {");
        FPrintln(fh, "        \"x\": " + pos[0] + ",");
        FPrintln(fh, "        \"y\": " + pos[1] + ",");
        FPrintln(fh, "        \"z\": " + pos[2]);
        FPrintln(fh, "    },");
        FPrintln(fh, "    \"cameraDirection\": {");
        FPrintln(fh, "        \"x\": " + cameraDir[0] + ",");
        FPrintln(fh, "        \"y\": " + cameraDir[1] + ",");
        FPrintln(fh, "        \"z\": " + cameraDir[2]);
        FPrintln(fh, "    },");
        FPrintln(fh, "    \"timeOfDay\": " + tod + ",");
        FPrintln(fh, "    \"weather\": {");
        FPrintln(fh, "        \"overcast\": " + overcast + ",");
        FPrintln(fh, "        \"rain\": " + rain + ",");
        FPrintln(fh, "        \"fog\": " + fog + ",");
        FPrintln(fh, "        \"snowfall\": " + snowfall + ",");
        FPrintln(fh, "        \"windSpeed\": " + windSpeed);
        FPrintln(fh, "    }");
        FPrintln(fh, "}");

        CloseFile(fh);
        Print("[SSCollector] Meta saved: " + filename);
    }
}

modded class PlayerBase
{
    override void OnRPC(PlayerIdentity sender, int rpc_type, ParamsReadContext ctx)
    {
        super.OnRPC(sender, rpc_type, ctx);

        if (!GetGame().IsServer())
            return;

        if (rpc_type == SSCRpc.CAPTURE_META)
        {
            vector cameraDir;
            if (!ctx.Read(cameraDir))
            {
                Print("[SSCollector] OnRPC CAPTURE_META: failed to read cameraDir");
                return;
            }
            SSCMetaWriter.Write(PlayerBase.Cast(this), cameraDir);
        }
    }
}
