using System;
using System.Buffers.Binary;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

static class Program
{
    private const string BundleMarker = "CVRZIP01";
    private const string ProfileMarker = "CVRCFG01";
    private static readonly Encoding Utf8NoBom = new UTF8Encoding(false);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    static bool ReadTrailer(FileStream stream, ref long end, string marker, out long start, out long size)
    {
        start = size = 0;
        if (end < 16) return false;
        Span<byte> bytes = stackalloc byte[16];
        stream.Position = end - 16;
        stream.ReadExactly(bytes);
        if (Encoding.ASCII.GetString(bytes[..8]) != marker) return false;
        ulong count = BinaryPrimitives.ReadUInt64LittleEndian(bytes[8..]);
        if (count > (ulong)(end - 16)) throw new InvalidDataException("Invalid length in EXE trailer.");
        size = (long)count;
        start = end - 16 - size;
        end = start;
        return true;
    }

    static void CopyRange(FileStream stream, long start, long size, Stream destination)
    {
        stream.Position = start;
        byte[] block = new byte[1024 * 1024];
        while (size > 0)
        {
            int n = stream.Read(block, 0, (int)Math.Min(block.Length, size));
            if (n == 0) throw new EndOfStreamException();
            destination.Write(block, 0, n);
            size -= n;
        }
    }

    static string Fingerprint(FileStream fs, long start, long length)
    {
        using var sha = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        fs.Position = start;
        byte[] buffer = new byte[1024 * 1024];
        while (length > 0)
        {
            int n = fs.Read(buffer, 0, (int)Math.Min(buffer.Length, length));
            if (n == 0) throw new EndOfStreamException();
            sha.AppendData(buffer, 0, n);
            length -= n;
        }
        return Convert.ToHexString(sha.GetHashAndReset())[..16];
    }

    static string ExtractEngine(FileStream stream, long start, long size, string root)
    {
        string key = Fingerprint(stream, start, size);
        string runtime = Path.Combine(root, "runtime-" + key);
        string executable = Path.Combine(runtime, "voice_ui.exe");
        if (File.Exists(executable)) return executable;
        string temporary = runtime + "-partial-" + Guid.NewGuid().ToString("N");
        Directory.CreateDirectory(temporary);
        try
        {
            string archive = Path.Combine(temporary, "runtime.zip");
            using (var dest = File.Create(archive)) CopyRange(stream, start, size, dest);
            ZipFile.ExtractToDirectory(archive, temporary, overwriteFiles: true);
            File.Delete(archive);
            if (!File.Exists(Path.Combine(temporary, "voice_ui.exe")))
                throw new InvalidDataException("Embedded Pocket runtime is incomplete.");
            try { Directory.Move(temporary, runtime); }
            catch (IOException) when (File.Exists(executable))
            {
                Directory.Delete(temporary, true);
            }
            return executable;
        }
        catch
        {
            if (Directory.Exists(temporary)) Directory.Delete(temporary, recursive: true);
            throw;
        }
    }

    static int VerifyOnly()
    {
        try
        {
            using var fs = File.Open(Environment.ProcessPath!, FileMode.Open, FileAccess.Read, FileShare.Read);
            long end = fs.Length;
            if (ReadTrailer(fs, ref end, ProfileMarker, out var profileAt, out var profileLength))
                if (profileLength < 2 || profileLength > 8_000_000) return 41;
            if (!ReadTrailer(fs, ref end, BundleMarker, out long bundleAt, out long bundleLength))
                return 42;
            if (bundleLength < 1_000_000) return 43;
            fs.Position = bundleAt;
            if (fs.ReadByte() != 'P' || fs.ReadByte() != 'K') return 44;
            return 0;
        }
        catch { return 45; }
    }

    static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--self-test")
            return VerifyOnly();
        try
        {
            var source = Environment.ProcessPath ?? throw new InvalidDataException("Executable path missing.");
            using var fs = File.Open(source, FileMode.Open, FileAccess.Read, FileShare.Read);
            long end = fs.Length;
            if (!ReadTrailer(fs, ref end, ProfileMarker, out long profileAt, out long profileLen))
            {
                MessageBoxW(IntPtr.Zero,
                    "This is the unpersonalized EXE template.\n\nCreate a voice with Pocket TTS on the website, listen to the preview, then export your personalized voice application.",
                    "Pocket Voice - Personalize first", 0x40);
                return 2;
            }
            if (profileLen < 60 || profileLen > 8_000_000)
                throw new InvalidDataException("Voice profile is invalid or too large.");
            byte[] profileBytes = new byte[checked((int)profileLen)];
            fs.Position = profileAt;
            fs.ReadExactly(profileBytes);
            if (!ReadTrailer(fs, ref end, BundleMarker, out long runtimeAt, out long runtimeLen))
                throw new InvalidDataException("Pocket voice runtime not found in EXE.");
            if (runtimeLen < 1_000_000)
                throw new InvalidDataException("Pocket voice runtime bundle is incomplete.");

            var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                    "PortableVoiceClone", "SingleRuntime");
            Directory.CreateDirectory(root);
            string engine = ExtractEngine(fs, runtimeAt, runtimeLen, root);
            using var document = JsonDocument.Parse(profileBytes);
            var data = document.RootElement;
            if (!data.TryGetProperty("engine", out var mode) || mode.GetString() != "pocket")
                throw new InvalidDataException("Not a Pocket TTS voice package.");
            if (!data.TryGetProperty("voice_wav_b64", out var audioElement))
                throw new InvalidDataException("No voice was embedded.");
            byte[] wav = Convert.FromBase64String(audioElement.GetString() ?? "");
            if (wav.Length < 44 || wav.Length > 5_000_000 ||
                Encoding.ASCII.GetString(wav, 0, 4) != "RIFF" ||
                Encoding.ASCII.GetString(wav, 8, 4) != "WAVE")
                throw new InvalidDataException("Embedded voice reference is not a valid WAV.");
            string name = data.TryGetProperty("voice_name", out var n) ? (n.GetString() ?? "My Voice") : "My Voice";
            if (name.Length > 72) name = name[..72];
            string lang = data.TryGetProperty("lang", out var l) ? (l.GetString() ?? "english_2026-04") : "english_2026-04";
            if (lang is not ("english_2026-04" or "english" or "german" or "spanish" or "italian" or "portuguese"))
                throw new InvalidDataException("Unsupported voice language.");

            string job = Path.Combine(root, "voice-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(job);
            try
            {
                string voicePath = Path.Combine(job, "reference.wav");
                string configPath = Path.Combine(job, "voice.json");
                File.WriteAllBytes(voicePath, wav);
                File.WriteAllText(configPath, JsonSerializer.Serialize(
                    new VoiceConfig { VoicePath = voicePath, VoiceName = name, Language = lang },
                    VoiceConfigJsonContext.Default.VoiceConfig), Utf8NoBom);
                using Process process = Process.Start(new ProcessStartInfo
                {
                    FileName = engine,
                    WorkingDirectory = Path.GetDirectoryName(engine),
                    ArgumentList = { "--config-file", configPath },
                    UseShellExecute = false,
                    CreateNoWindow = true,
                }) ?? throw new InvalidDataException("Cannot open the voice studio runtime.");
                process.WaitForExit();
                if (process.ExitCode != 0)
                    throw new InvalidDataException("Voice UI returned " + process.ExitCode +
                                                   ". Check %LOCALAPPDATA%\\PortableVoiceClone logs.");
            }
            finally
            {
                try { Directory.Delete(job, recursive: true); } catch (IOException) { }
            }
            return 0;
        }
        catch (Exception ex)
        {
            MessageBoxW(IntPtr.Zero, ex.Message, "Pocket Voice - Error", 0x10);
            return 1;
        }
    }
}

internal sealed class VoiceConfig
{
    [JsonPropertyName("voice_name")] public string VoiceName { get; set; } = "My Voice";
    [JsonPropertyName("lang")] public string Language { get; set; } = "english_2026-04";
    [JsonPropertyName("voice_path")] public string VoicePath { get; set; } = "";
}

[JsonSerializable(typeof(VoiceConfig))]
internal partial class VoiceConfigJsonContext : JsonSerializerContext {}
