class SSCMetaWriter
{
    // Returns unix timestamp in seconds as an int (fits int32 until ~2038).
    // Uses real-world server date from GetDate(), not in-game time.
    static int ComputeUnixSeconds()
    {
        int year, month, day, hour, minute;
        GetGame().GetWorld().GetDate(year, month, day, hour, minute);

        // Leap years from year 1 up to (year-1), minus offset for years before 1970.
        // Offset = floor(1969/4) - floor(1969/100) + floor(1969/400) = 492 - 19 + 4 = 477
        int prevYear = year - 1;
        int leapDays = (prevYear / 4) - (prevYear / 100) + (prevYear / 400) - 477;
        int days = (year - 1970) * 365 + leapDays;

        // Add completed months
        bool isLeap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
        if (month > 1)  days += 31;
        if (month > 2 && isLeap)  days += 29;
        if (month > 2 && !isLeap) days += 28;
        if (month > 3)  days += 31;
        if (month > 4)  days += 30;
        if (month > 5)  days += 31;
        if (month > 6)  days += 30;
        if (month > 7)  days += 31;
        if (month > 8)  days += 31;
        if (month > 9)  days += 30;
        if (month > 10) days += 31;
        if (month > 11) days += 30;

        days += day - 1;

        return days * 86400 + hour * 3600 + minute * 60;
    }

    static void Write(PlayerBase player, vector cameraDir)
    {
        if (!player)
            return;

        SSCConfig cfg = SSCConfigManager.Get();
        string outDir = "$profile:SSCollector/" + cfg.outputDir;

        MakeDirectory("$profile:SSCollector");
        MakeDirectory(outDir);

        int ts = ComputeUnixSeconds();
        string tsStr = "" + ts + "000";
        string filename = outDir + "/ss-meta-" + tsStr + ".json";

        vector pos = player.GetPosition();
        float tod = GetGame().GetDayTime();

        FileHandle fh = OpenFile(filename, FileMode.WRITE);
        if (fh == 0)
        {
            Print("[SSCollector] ERROR: Could not open file for writing: " + filename);
            return;
        }

        FPrintln(fh, "{");
        FPrintln(fh, "    \"timestamp\": " + tsStr + ",");
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
        FPrintln(fh, "    \"timeOfDay\": " + tod);
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
