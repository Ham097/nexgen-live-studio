using System;
using System.IO;
using System.Text;

internal static class FindStudio
{
    private const string Origin = "chrome-extension://hheadkjenhbpkhnabjfnkffbpanjbmgj/";

    public static void Main(string[] args)
    {
        if (args.Length == 0 || args[0] != Origin)
            return;

        string file = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "chrome-session.json");
        using (var input = new BinaryReader(Console.OpenStandardInput()))
        using (var output = new BinaryWriter(Console.OpenStandardOutput()))
        {
            while (true)
            {
                try
                {
                    uint size = input.ReadUInt32();
                    if (size == 0 || size > 256 || input.ReadBytes((int)size).Length != size)
                        return;

                    byte[] message = Encoding.UTF8.GetBytes("{}");
                    try
                    {
                        using (var pairing = new FileStream(file, FileMode.Open, FileAccess.Read,
                                                           FileShare.ReadWrite | FileShare.Delete))
                        {
                            if (pairing.Length <= 256)
                            {
                                using (var text = new StreamReader(pairing))
                                    message = Encoding.UTF8.GetBytes(text.ReadToEnd());
                            }
                        }
                    }
                    catch (IOException) { }
                    output.Write((uint)message.Length);
                    output.Write(message);
                    output.Flush();
                }
                catch (IOException) { return; }
            }
        }
    }
}
