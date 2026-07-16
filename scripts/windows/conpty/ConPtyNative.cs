// Minimal Windows Pseudo Console (ConPTY) host, built only on top of the
// documented kernel32 API (no third-party package). This exists solely to
// drive a real interactive console for the #1976 evidence fixture: it lets
// a hosted child process (packaged apm.exe) see an actual console the same
// way a human PowerShell/Windows Terminal session would, so an OpenSSH
// passphrase prompt is issued exactly as it would be for an interactive
// user, instead of failing (or falling back to askpass) because no console
// is attached.
//
// References (public, built-in Win32 API -- no external dependency):
//   https://learn.microsoft.com/windows/console/createpseudoconsole
//   https://learn.microsoft.com/windows/console/creating-a-pseudoconsole-session
//
// This file is intentionally self-contained and narrowly scoped: it knows
// how to (1) create a pseudo console, (2) launch one child process attached
// to it, (3) relay its output into an in-memory buffer on a background
// thread, and (4) write bytes into its input stream (including raw control
// bytes such as Ctrl+C). It does not implement resizing, job objects, or
// any feature the fixture does not need.

using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace ApmConPty
{
    /// <summary>
    /// One chunk of decoded console output, timestamped for evidence capture.
    /// </summary>
    public sealed class ConPtyOutputChunk
    {
        public DateTime TimestampUtc { get; set; }
        public string Text { get; set; }
    }

    /// <summary>
    /// Hosts one child process attached to a freshly created pseudo console.
    /// </summary>
    public sealed class ConPtyProcess : IDisposable
    {
        private const uint EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
        private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
        private const int PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016;

        [StructLayout(LayoutKind.Sequential)]
        private struct COORD
        {
            public short X;
            public short Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct STARTUPINFO
        {
            public int cb;
            public IntPtr lpReserved;
            public IntPtr lpDesktop;
            public IntPtr lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct STARTUPINFOEX
        {
            public STARTUPINFO StartupInfo;
            public IntPtr lpAttributeList;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public int dwProcessId;
            public int dwThreadId;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CreatePipe(
            out IntPtr hReadPipe,
            out IntPtr hWritePipe,
            IntPtr lpPipeAttributes,
            uint nSize);

        [DllImport("kernel32.dll")]
        private static extern int CreatePseudoConsole(
            COORD size,
            IntPtr hInput,
            IntPtr hOutput,
            uint dwFlags,
            out IntPtr phPC);

        [DllImport("kernel32.dll")]
        private static extern void ClosePseudoConsole(IntPtr hPC);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool InitializeProcThreadAttributeList(
            IntPtr lpAttributeList,
            int dwAttributeCount,
            int dwFlags,
            ref IntPtr lpSize);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool UpdateProcThreadAttribute(
            IntPtr lpAttributeList,
            uint dwFlags,
            IntPtr Attribute,
            IntPtr lpValue,
            IntPtr cbSize,
            IntPtr lpPreviousValue,
            IntPtr lpReturnSize);

        [DllImport("kernel32.dll")]
        private static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern bool CreateProcessW(
            string lpApplicationName,
            StringBuilder lpCommandLine,
            IntPtr lpProcessAttributes,
            IntPtr lpThreadAttributes,
            bool bInheritHandles,
            uint dwCreationFlags,
            IntPtr lpEnvironment,
            string lpCurrentDirectory,
            ref STARTUPINFOEX lpStartupInfo,
            out PROCESS_INFORMATION lpProcessInformation);

        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll")]
        private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll")]
        private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll")]
        private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

        private const uint STILL_ACTIVE = 259;
        private const uint WAIT_OBJECT_0 = 0;

        private IntPtr _pseudoConsole = IntPtr.Zero;
        private IntPtr _attributeList = IntPtr.Zero;
        private IntPtr _processHandle = IntPtr.Zero;
        private IntPtr _threadHandle = IntPtr.Zero;
        private FileStream _inputWriter;
        private FileStream _outputReader;
        private Thread _readerThread;
        private readonly ConcurrentQueue<ConPtyOutputChunk> _chunks = new ConcurrentQueue<ConPtyOutputChunk>();
        private readonly ManualResetEventSlim _chunkSignal = new ManualResetEventSlim(false);
        private volatile bool _outputClosed;
        private bool _disposed;

        public int ProcessId { get; private set; }

        /// <summary>
        /// Starts <paramref name="commandLine"/> attached to a brand-new pseudo
        /// console, with an explicit (fully controlled) environment block.
        /// </summary>
        public static ConPtyProcess Start(
            string commandLine,
            string workingDirectory,
            IDictionary environment,
            short columns = 120,
            short rows = 32)
        {
            if (string.IsNullOrEmpty(commandLine))
            {
                throw new ArgumentException("commandLine must not be empty", nameof(commandLine));
            }

            IntPtr inputReadSide = IntPtr.Zero;
            IntPtr inputWriteSide = IntPtr.Zero;
            IntPtr outputReadSide = IntPtr.Zero;
            IntPtr outputWriteSide = IntPtr.Zero;
            IntPtr pseudoConsole = IntPtr.Zero;
            IntPtr attributeList = IntPtr.Zero;
            IntPtr environmentBlock = IntPtr.Zero;

            var session = new ConPtyProcess();
            try
            {
                if (!CreatePipe(out inputReadSide, out inputWriteSide, IntPtr.Zero, 0))
                {
                    throw new InvalidOperationException(
                        "CreatePipe(input) failed: " + Marshal.GetLastWin32Error());
                }
                if (!CreatePipe(out outputReadSide, out outputWriteSide, IntPtr.Zero, 0))
                {
                    throw new InvalidOperationException(
                        "CreatePipe(output) failed: " + Marshal.GetLastWin32Error());
                }

                var size = new COORD { X = columns, Y = rows };
                int hr = CreatePseudoConsole(size, inputReadSide, outputWriteSide, 0, out pseudoConsole);
                if (hr != 0)
                {
                    throw new InvalidOperationException(
                        "CreatePseudoConsole failed, hresult=0x" + hr.ToString("X8", CultureInfo.InvariantCulture));
                }

                // The pseudo console duplicated what it needs; our copies of the
                // "server" ends are no longer needed and must be closed so EOF
                // is detected correctly when the child exits.
                CloseHandle(inputReadSide);
                inputReadSide = IntPtr.Zero;
                CloseHandle(outputWriteSide);
                outputWriteSide = IntPtr.Zero;

                IntPtr requiredSize = IntPtr.Zero;
                InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref requiredSize);
                attributeList = Marshal.AllocHGlobal(requiredSize);
                if (!InitializeProcThreadAttributeList(attributeList, 1, 0, ref requiredSize))
                {
                    throw new InvalidOperationException(
                        "InitializeProcThreadAttributeList failed: " + Marshal.GetLastWin32Error());
                }
                if (!UpdateProcThreadAttribute(
                        attributeList,
                        0,
                        (IntPtr)PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                        pseudoConsole,
                        (IntPtr)IntPtr.Size,
                        IntPtr.Zero,
                        IntPtr.Zero))
                {
                    throw new InvalidOperationException(
                        "UpdateProcThreadAttribute failed: " + Marshal.GetLastWin32Error());
                }

                var startupInfoEx = new STARTUPINFOEX();
                startupInfoEx.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
                startupInfoEx.lpAttributeList = attributeList;

                uint creationFlags = EXTENDED_STARTUPINFO_PRESENT;
                if (environment != null)
                {
                    environmentBlock = BuildEnvironmentBlock(environment);
                    creationFlags |= CREATE_UNICODE_ENVIRONMENT;
                }

                var mutableCommandLine = new StringBuilder(commandLine, commandLine.Length + 32);
                PROCESS_INFORMATION processInfo;
                bool created = CreateProcessW(
                    null,
                    mutableCommandLine,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    false,
                    creationFlags,
                    environmentBlock,
                    workingDirectory,
                    ref startupInfoEx,
                    out processInfo);

                if (!created)
                {
                    throw new InvalidOperationException(
                        "CreateProcessW failed: " + Marshal.GetLastWin32Error()
                        + " command=" + commandLine);
                }

                session._pseudoConsole = pseudoConsole;
                session._attributeList = attributeList;
                session._processHandle = processInfo.hProcess;
                session._threadHandle = processInfo.hThread;
                session.ProcessId = processInfo.dwProcessId;
                session._inputWriter = new FileStream(
                    new Microsoft.Win32.SafeHandles.SafeFileHandle(inputWriteSide, true),
                    FileAccess.Write,
                    4096,
                    false);
                session._outputReader = new FileStream(
                    new Microsoft.Win32.SafeHandles.SafeFileHandle(outputReadSide, true),
                    FileAccess.Read,
                    4096,
                    false);

                // Ownership of these two handles transferred to the FileStreams
                // above; null them out so the failure path below does not
                // double-close them.
                inputWriteSide = IntPtr.Zero;
                outputReadSide = IntPtr.Zero;
                pseudoConsole = IntPtr.Zero;
                attributeList = IntPtr.Zero;

                session._readerThread = new Thread(session.ReadLoop) { IsBackground = true };
                session._readerThread.Start();

                return session;
            }
            catch
            {
                if (inputReadSide != IntPtr.Zero) CloseHandle(inputReadSide);
                if (inputWriteSide != IntPtr.Zero) CloseHandle(inputWriteSide);
                if (outputReadSide != IntPtr.Zero) CloseHandle(outputReadSide);
                if (outputWriteSide != IntPtr.Zero) CloseHandle(outputWriteSide);
                if (pseudoConsole != IntPtr.Zero) ClosePseudoConsole(pseudoConsole);
                if (attributeList != IntPtr.Zero) DeleteProcThreadAttributeList(attributeList);
                throw;
            }
            finally
            {
                if (environmentBlock != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(environmentBlock);
                }
            }
        }

        private static IntPtr BuildEnvironmentBlock(IDictionary environment)
        {
            var keys = environment.Keys.Cast<object>()
                .Select(k => k.ToString())
                .OrderBy(k => k, StringComparer.OrdinalIgnoreCase)
                .ToArray();

            var builder = new StringBuilder();
            foreach (var key in keys)
            {
                var value = environment[key];
                if (value == null)
                {
                    continue;
                }
                builder.Append(key).Append('=').Append(value.ToString()).Append('\0');
            }
            builder.Append('\0');

            var chars = builder.ToString().ToCharArray();
            var bytes = new byte[chars.Length * 2];
            Buffer.BlockCopy(chars, 0, bytes, 0, bytes.Length);
            var block = Marshal.AllocHGlobal(bytes.Length);
            Marshal.Copy(bytes, 0, block, bytes.Length);
            return block;
        }

        private void ReadLoop()
        {
            var buffer = new byte[4096];
            try
            {
                while (true)
                {
                    int read = _outputReader.Read(buffer, 0, buffer.Length);
                    if (read <= 0)
                    {
                        break;
                    }
                    var text = Encoding.UTF8.GetString(buffer, 0, read);
                    _chunks.Enqueue(new ConPtyOutputChunk { TimestampUtc = DateTime.UtcNow, Text = text });
                    _chunkSignal.Set();
                }
            }
            catch (IOException)
            {
                // Expected once the pseudo console tears down the pipe after
                // the hosted process exits.
            }
            catch (ObjectDisposedException)
            {
                // Stream was disposed from Dispose(); nothing left to read.
            }
            finally
            {
                _outputClosed = true;
                _chunkSignal.Set();
            }
        }

        /// <summary>
        /// Drains whatever output has accumulated, waiting up to
        /// <paramref name="timeoutMs"/> for at least one new chunk if the
        /// queue is currently empty and the stream is still open.
        /// </summary>
        public string ReadAvailable(int timeoutMs)
        {
            if (_chunks.IsEmpty && !_outputClosed)
            {
                _chunkSignal.Reset();
                _chunkSignal.Wait(timeoutMs);
            }

            var builder = new StringBuilder();
            while (_chunks.TryDequeue(out var chunk))
            {
                builder.Append(chunk.Text);
            }
            return builder.ToString();
        }

        /// <summary>
        /// Polls output until <paramref name="isMatch"/> returns true for the
        /// accumulated transcript, or the overall timeout elapses. Returns the
        /// full transcript observed (matched or not).
        /// </summary>
        public (bool matched, string transcript) WaitForText(
            Func<string, bool> isMatch,
            int timeoutMs,
            int pollIntervalMs = 200)
        {
            var transcript = new StringBuilder();
            var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                transcript.Append(ReadAvailable(pollIntervalMs));
                if (isMatch(transcript.ToString()))
                {
                    return (true, transcript.ToString());
                }
                if (_outputClosed && _chunks.IsEmpty)
                {
                    break;
                }
            }
            transcript.Append(ReadAvailable(0));
            return (isMatch(transcript.ToString()), transcript.ToString());
        }

        /// <summary>Writes raw text (UTF-8) into the child's console input.</summary>
        public void WriteInput(string text)
        {
            var bytes = Encoding.UTF8.GetBytes(text);
            _inputWriter.Write(bytes, 0, bytes.Length);
            _inputWriter.Flush();
        }

        /// <summary>Writes a single raw byte (e.g. 0x03 for Ctrl+C) into input.</summary>
        public void WriteRawByte(byte value)
        {
            _inputWriter.Write(new[] { value }, 0, 1);
            _inputWriter.Flush();
        }

        public bool WaitForExit(int timeoutMs, out int exitCode)
        {
            uint waited = WaitForSingleObject(_processHandle, (uint)timeoutMs);
            uint code;
            GetExitCodeProcess(_processHandle, out code);
            exitCode = unchecked((int)code);
            return waited == WAIT_OBJECT_0 && code != STILL_ACTIVE;
        }

        public void Kill()
        {
            if (_processHandle != IntPtr.Zero)
            {
                TerminateProcess(_processHandle, 1);
            }
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }
            _disposed = true;

            try { _inputWriter?.Dispose(); } catch (Exception) { /* best effort */ }
            try { _outputReader?.Dispose(); } catch (Exception) { /* best effort */ }
            if (_readerThread != null && _readerThread.IsAlive)
            {
                _readerThread.Join(2000);
            }
            if (_pseudoConsole != IntPtr.Zero)
            {
                ClosePseudoConsole(_pseudoConsole);
                _pseudoConsole = IntPtr.Zero;
            }
            if (_attributeList != IntPtr.Zero)
            {
                DeleteProcThreadAttributeList(_attributeList);
                Marshal.FreeHGlobal(_attributeList);
                _attributeList = IntPtr.Zero;
            }
            if (_threadHandle != IntPtr.Zero)
            {
                CloseHandle(_threadHandle);
                _threadHandle = IntPtr.Zero;
            }
            if (_processHandle != IntPtr.Zero)
            {
                CloseHandle(_processHandle);
                _processHandle = IntPtr.Zero;
            }
        }
    }
}
