using System;
using System.Buffers.Binary;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.IO.Pipes;
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
    static bool MatchesHash(string file, string expected)
    {
        try
        {
            using var stream = File.OpenRead(file);
            return Convert.ToHexString(SHA256.HashData(stream)).Equals(expected, StringComparison.Ordinal);
        }
        catch (IOException) { return false; }
    }

    static string FindOrDownloadRuntime(string root)
    {
        foreach (var folder in Directory.EnumerateDirectories(root, "runtime-*"))
        {
            var found = Path.Combine(folder, "voice_ui.exe");
            if (File.Exists(found)) return found;
        }

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
                n.GetString() != "SingleVoice-Warm-v4.exe") continue;
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
        if (!File.Exists(downloaded) || !MatchesHash(downloaded, expectedDigest))
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

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool OpenClipboard(IntPtr owner);
    [DllImport("user32.dll")]
    private static extern bool CloseClipboard();
    [DllImport("user32.dll")]
    private static extern IntPtr GetClipboardData(uint format);
    [DllImport("user32.dll")]
    private static extern bool IsClipboardFormatAvailable(uint format);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GlobalLock(IntPtr handle);
    [DllImport("kernel32.dll")]
    private static extern bool GlobalUnlock(IntPtr handle);

    static string SnapshotClipboard()
    {
        const uint CF_UNICODETEXT = 13;
        for (int attempt = 0; attempt < 20; attempt++)
        {
            if (!OpenClipboard(IntPtr.Zero))
            {
                System.Threading.Thread.Sleep(25);
                continue;
            }
            try
            {
                if (!IsClipboardFormatAvailable(CF_UNICODETEXT)) return "";
                IntPtr handle = GetClipboardData(CF_UNICODETEXT);
                if (handle == IntPtr.Zero) return "";
                IntPtr ptr = GlobalLock(handle);
                if (ptr == IntPtr.Zero) return "";
                try { return Marshal.PtrToStringUni(ptr) ?? ""; }
                finally { GlobalUnlock(handle); }
            }
            finally { CloseClipboard(); }
        }
        return "";
    }

    // A single hidden runtime per Windows user/session. The randomly named pipe
    // and token live in the user's local app-data folder, never on the network.
    static bool SendRequest(string[] state, string command, byte[] profile, string phrase, int timeout)
    {
        if (state.Length < 3 || !state[0].StartsWith("PocketWarm-") || state[1].Length != 64)
            return false;
        using var pipe = new NamedPipeClientStream(".", state[0], PipeDirection.InOut, PipeOptions.Asynchronous);
        try { pipe.Connect(timeout); }
        catch (TimeoutException) { return false; }
        byte[] prefix = Utf8NoBom.GetBytes(state[1] + "\n" + command + "\n");
        byte[] words = Utf8NoBom.GetBytes(phrase);
        if (words.Length > 262144) throw new InvalidDataException("Clipboard message exceeds 256 KB.");
        byte[] request = new byte[prefix.Length + 4 + profile.Length + words.Length];
        prefix.CopyTo(request, 0);
        BinaryPrimitives.WriteInt32LittleEndian(request.AsSpan(prefix.Length, 4), profile.Length);
        profile.CopyTo(request, prefix.Length + 4);
        words.CopyTo(request, prefix.Length + 4 + profile.Length);
        // Do not retry after writing: an acknowledged or ambiguous delivery must
        // never generate duplicate speech.
        using var deadline = new System.Threading.CancellationTokenSource(TimeSpan.FromSeconds(10));
        pipe.WriteAsync(request.AsMemory(), deadline.Token).AsTask().GetAwaiter().GetResult();
        byte[] reply = new byte[1];
        int count = pipe.ReadAsync(reply.AsMemory(), deadline.Token).AsTask().GetAwaiter().GetResult();
        if (count != 1 || (reply[0] != (byte)'A' && reply[0] != (byte)'D'))
            throw new IOException("The warm speech engine did not accept the request.");
        return true;
    }

    static bool WorkerAlive(string[] state)
    {
        if (state.Length < 3 || !int.TryParse(state[2], out int pid) || pid <= 0) return false;
        try
        {
            using var worker = Process.GetProcessById(pid);
            return !worker.HasExited && worker.ProcessName.Equals("voice_ui", StringComparison.OrdinalIgnoreCase);
        }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
    }

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--self-test") return VerifyOnly();
        string command = args.Length == 1 && args[0] == "--stop" ? "stop" :
                         args.Length == 1 && args[0] == "--warmup" ? "warmup" : "speak";
        string phrase = command == "speak" ? SnapshotClipboard() : "";
        if (command == "speak" && string.IsNullOrWhiteSpace(phrase)) return 0;
        var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                "PortableVoiceClone", "WarmRuntime-v4");
        string statePath = Path.Combine(root, "worker-" + Process.GetCurrentProcess().SessionId + ".state");
        using var startupLock = new System.Threading.Mutex(false, @"Local\PortableVoiceClone.WarmSpeech.v4");
        bool ownsLock = false;
        try
        {
            try { ownsLock = startupLock.WaitOne(2000); }
            catch (System.Threading.AbandonedMutexException) { ownsLock = true; }
            if (!ownsLock) return 0;
            byte[] profileBytes = Array.Empty<byte>();
            using var fs = File.Open(Environment.ProcessPath!, FileMode.Open, FileAccess.Read, FileShare.Read);
            long end = fs.Length;
            bool bundled = false;
            long runtimeAt = 0, runtimeLen = 0;
            if (command != "stop")
            {
                if (!ReadTrailer(fs, ref end, ProfileMarker, out long profileAt, out long profileLen))
                    throw new InvalidDataException("Personalize this EXE with your voice on the website first.");
                if (profileLen < 60 || profileLen > 8_000_000)
                    throw new InvalidDataException("Voice profile is invalid or too large.");
                profileBytes = new byte[checked((int)profileLen)];
                fs.Position = profileAt;
                fs.ReadExactly(profileBytes);
                bundled = ReadTrailer(fs, ref end, BundleMarker, out runtimeAt, out runtimeLen);
                if (bundled && runtimeLen < 1_000_000)
                    throw new InvalidDataException("Pocket voice runtime bundle is incomplete.");
            }
            Directory.CreateDirectory(root);
            if (File.Exists(statePath))
            {
                string[] state = File.ReadAllLines(statePath);
                if (SendRequest(state, command, profileBytes, phrase, 250)) return 0;
                if (WorkerAlive(state))
                    throw new IOException("The existing speech engine is not responding. No second engine was started.");
                File.Delete(statePath);
            }
            if (command == "stop") return 0;

            string engine = bundled ? ExtractEngine(fs, runtimeAt, runtimeLen, root) : FindOrDownloadRuntime(root);
            string pipeName = "PocketWarm-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(24));
            string token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            // No clipboard message or voice recording is written to the state file.
            File.WriteAllLines(statePath, new[] {pipeName, token, "0"}, Utf8NoBom);
            Process? process = null;
            try
            {
                process = Process.Start(new ProcessStartInfo
                {
                    FileName = engine,
                    WorkingDirectory = Path.GetDirectoryName(engine),
                    ArgumentList = { "--worker-state", statePath },
                    UseShellExecute = false,
                    CreateNoWindow = true,
                }) ?? throw new InvalidDataException("Cannot start the warm speech engine.");
                string[] state = {pipeName, token, process.Id.ToString()};
                File.WriteAllLines(statePath + ".tmp", state, Utf8NoBom);
                File.Move(statePath + ".tmp", statePath, true);
                var timer = Stopwatch.StartNew();
                while (timer.Elapsed < TimeSpan.FromSeconds(45))
                {
                    if (process.HasExited) throw new IOException("The speech engine exited during startup.");
                    if (SendRequest(state, command, profileBytes, phrase, 250)) return 0;
                }
                throw new IOException("The speech engine did not become ready.");
            }
            catch
            {
                // If a request was sent but acknowledgment failed, do not replay it.
                if (process is null || process.HasExited) File.Delete(statePath);
                throw;
            }
            finally { process?.Dispose(); }
        }
        catch (Exception ex)
        {
            // Failure only; normal launches never open a window or take AAC focus.
            MessageBoxW(IntPtr.Zero, ex.Message, "Pocket Voice - Error", 0x10);
            return 1;
        }
        finally { if (ownsLock) startupLock.ReleaseMutex(); }
    }
}
