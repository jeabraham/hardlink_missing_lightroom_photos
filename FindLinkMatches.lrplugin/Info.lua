return {
  LrSdkVersion = 10.0,
  LrPluginName = "Find Matches to Missing Photos",
  LrToolkitIdentifier = "com.github.jeabraham.findmissingmatches",
  LrPluginInfoUrl = "https://github.com/jeabraham/hardlink_missing_lightroom_photos",

  LrLibraryMenuItems = {
    {
      title = "Find Matches to Missing Photos To Possibly Link",
      file = "main.lua",
    },
    {
      title = "Write CSV File for Photos",
      file = "write_csv.lua",
    },
    {
      title = "Write CSV File for Missing Photos",
      file = "write_missing_csv.lua",
    },
  },

  VERSION = { major = 1, minor = 0, revision = 0, build = 1 },
}
