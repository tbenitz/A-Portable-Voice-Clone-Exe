using System;
using System.Buffers.Binary;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
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


    // For compact browser-built EXEs, the launcher contains only its voice.
    // The large frozen Pocket runtime is downloaded on Windows once and cached.
    // The GitHub API supplies a SHA-256 digest; the binary MUST match it.
    static string FindOrDownloadRuntime(string root)
    {
        foreach (var folder in Directory.EnumerateDirectories(root, "runtime-*"))
        {
            var found = Path.Combine(folder, "voice_ui.exe");
            if (File.Exists(found)) return found;
        }

        MessageBoxW(IntPtr.Zero,
            "First launch: Pocket Voice will download its Windows speech runtime (about 205 MB) from GitHub Releases. " +
            "A separate Python install is NOT required.\n\nAn internet connection is needed this first time. " +
            "The runtime is cached for future offline use.",
            "Pocket Voice - First Launch", 0x40);
        const string api = "https://api.github.com/repos/tbenitz/A-Portable-Voice-Clone-Exe/releases/latest";
        using var http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = true });
        http.Timeout = TimeSpan.FromMinutes(20);
        http.DefaultRequestHeaders.UserAgent.ParseAdd("PocketVoiceStudio/1.0");
        using var releaseResponse = http.GetAsync(api).GetAwaiter().GetResult();
        releaseResponse.EnsureSuccessStatusCode();
        string json = releaseResponse.Content.ReadAsStringAsync().GetAwaiter().GetResult();
        using var document = JsonDocument.Parse(json);
        string? assetUrl = null;
        string? assetDigest = null;
        long assetSize = 0;
        foreach (var asset in document.RootElement.GetProperty("assets").EnumerateArray())
        {
            if (!asset.TryGetProperty("name", out var n) ||
                n.GetString() != "SingleVoice-Base.exe") continue;
            assetUrl = asset.GetProperty("browser_download_url").GetString();
            assetDigest = asset.GetProperty("digest").GetString();
            assetSize = asset.GetProperty("size").GetInt64();
            break;
        }
        if (assetUrl is null ||
            !assetUrl.StartsWith("https://github.com/tbenitz/A-Portable-Voice-Clone-Exe/releases/download/", StringComparison.Ordinal) ||
            assetDigest is null || !assetDigest.StartsWith("sha256:", StringComparison.Ordinal) ||
            assetDigest.Length != 71 || assetSize < 1_000_000 || assetSize > 500_000_000)
            throw new InvalidDataException("GitHub release metadata for Pocket TTS is invalid.");
        string expectedDigest = assetDigest["sha256:".Length..].ToUpperInvariant();
        foreach (char ch in expectedDigest) if (!Uri.IsHexDigit(ch))
            throw new InvalidDataException("Engine digest contains non-hex characters.");

        string downloaded = Path.Combine(root, "engine-" + expectedDigest[..16] + ".exe");
        if (!File.Exists(downloaded) ||
            !Convert.ToHexString(SHA256.HashData(File.OpenRead(downloaded))).Equals(expectedDigest, StringComparison.Ordinal))
        {
            // Incomplete downloads never replace a validated cached file.
            string temporary = downloaded + ".part-" + Guid.NewGuid().ToString("N");
            try
            {
                using var binaryResponse = http.GetAsync(assetUrl, HttpCompletionOption.ResponseHeadersRead).GetAwaiter().GetResult();
                binaryResponse.EnsureSuccessStatusCode();
                using var incoming = binaryResponse.Content.ReadAsStreamAsync().GetAwaiter().GetResult();
                using (var target = File.Create(temporary))
                {
                    byte[] block = new byte[1024 * 1024];
                    long total = 0;
                    while (true)
                    {
                        int n = incoming.Read(block, 0, block.Length);
                        if (n == 0) break;
                        total += n;
                        if (total > 500_000_000) throw new InvalidDataException("Runtime exceeds download safety limit.");
                        target.Write(block, 0, n);
                    }
                }
                if (new FileInfo(temporary).Length != assetSize)
                    throw new InvalidDataException("Runtime download was truncated.");
                using (var check = File.OpenRead(temporary))
                    if (!Convert.ToHexString(SHA256.HashData(check)).Equals(expectedDigest, StringComparison.Ordinal))
                        throw new InvalidDataException("Runtime SHA-256 verification failed.");
                File.Move(temporary, downloaded, overwrite: true);
            }
            finally { try { File.Delete(temporary); } catch { } }
        }

        using var stream = File.Open(downloaded, FileMode.Open, FileAccess.Read, FileShare.Read);
        long end = stream.Length;
        if (!ReadTrailer(stream, ref end, BundleMarker, out var start, out var length) ||
            length < 1_000_000)
            throw new InvalidDataException("Downloaded runtime does not contain a valid Pocket bundle.");
        stream.Position = start;
        if (stream.ReadByte() != 'P' || stream.ReadByte() != 'K')
            throw new InvalidDataException("Downloaded runtime ZIP is invalid.");
        return ExtractEngine(stream, start, length, root);
    }

    static int VerifyOnly()
    {
        try
        {
            using var fs = File.Open(Environment.ProcessPath!, FileMode.Open, FileAccess.Read, FileShare.Read);
            long end = fs.Length;
            if (ReadTrailer(fs, ref end, ProfileMarker, out var profileAt, out var profileLength))
                if (profileLength < 2 || profileLength > 8_000_000) return 41;
            // Both the large bundled EXE and the tiny browser starter are valid.
            if (!ReadTrailer(fs, ref end, BundleMarker, out long bundleAt, out long bundleLength))
                return 0;
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
            bool bundled = ReadTrailer(fs, ref end, BundleMarker, out long runtimeAt, out long runtimeLen);
            if (bundled && runtimeLen < 1_000_000)
                throw new InvalidDataException("Pocket voice runtime bundle is incomplete.");

            var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                    "PortableVoiceClone", "SingleRuntime");
            Directory.CreateDirectory(root);
            string engine = bundled ? ExtractEngine(fs, runtimeAt, runtimeLen, root)
                                    : FindOrDownloadRuntime(root);
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
