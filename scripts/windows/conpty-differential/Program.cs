using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using static ApmConPtyDifferential.NativeMethods;

namespace ApmConPtyDifferential
{
    /// <summary>
    /// Minimal, standalone ConPTY differential for issue #1976. Spawns
    /// "cmd.exe /c echo &lt;marker&gt; ..." attached to a freshly created
    /// pseudo console and reports whether the marker text appears in the
    /// captured transcript. Deliberately excludes every workaround added
    /// to ../conpty/ConPtyNative.cs this session (DSR responder, resize
    /// kicks, process-tree diagnostics) -- this is the plainest possible
    /// ConPTY consumer, matching Microsoft's own "Creating a
    /// Pseudoconsole session" sample structure, run as a real standalone
    /// .exe (not via PowerShell Add-Type in-process).
    ///
    /// Usage: MiniTerm.exe &lt;marker&gt;
    /// Exit code 0 = marker observed in transcript (harness works here).
    /// Exit code 1 = marker NOT observed (same symptom as the fixture).
    /// Exit code 2 = setup/native-call failure before the child even ran.
    /// </summary>
    internal static class Program
    {
        private static int Main(string[] args)
        {
            string marker = args.Length > 0 ? args[0] : "MINITERM_MARKER_DEFAULT";
            string commandLine = "C:\\Windows\\System32\\cmd.exe /c \"echo " + marker
                + " & ping -n 3 127.0.0.1 >nul & exit /b 7\"";

            IntPtr inputReadSide = IntPtr.Zero, inputWriteSide = IntPtr.Zero;
            IntPtr outputReadSide = IntPtr.Zero, outputWriteSide = IntPtr.Zero;
            IntPtr pseudoConsole = IntPtr.Zero;
            IntPtr attributeList = IntPtr.Zero;
            IntPtr processHandle = IntPtr.Zero, threadHandle = IntPtr.Zero;

            try
            {
                if (!CreatePipe(out inputReadSide, out inputWriteSide, IntPtr.Zero, 0))
                {
                    Console.Error.WriteLine("CreatePipe(input) failed: " + Marshal.GetLastWin32Error());
                    return 2;
                }
                if (!CreatePipe(out outputReadSide, out outputWriteSide, IntPtr.Zero, 0))
                {
                    Console.Error.WriteLine("CreatePipe(output) failed: " + Marshal.GetLastWin32Error());
                    return 2;
                }

                var size = new COORD { X = 120, Y = 32 };
                int hr = CreatePseudoConsole(size, inputReadSide, outputWriteSide, 0, out pseudoConsole);
                if (hr != 0)
                {
                    Console.Error.WriteLine("CreatePseudoConsole failed, hresult=0x" + hr.ToString("X8"));
                    return 2;
                }

                IntPtr requiredSize = IntPtr.Zero;
                InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref requiredSize);
                attributeList = Marshal.AllocHGlobal(requiredSize);
                if (!InitializeProcThreadAttributeList(attributeList, 1, 0, ref requiredSize))
                {
                    Console.Error.WriteLine("InitializeProcThreadAttributeList failed: " + Marshal.GetLastWin32Error());
                    return 2;
                }
                if (!UpdateProcThreadAttribute(
                        attributeList, 0, (IntPtr)PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                        pseudoConsole, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero))
                {
                    Console.Error.WriteLine("UpdateProcThreadAttribute failed: " + Marshal.GetLastWin32Error());
                    return 2;
                }

                var startupInfoEx = new STARTUPINFOEX();
                startupInfoEx.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
                startupInfoEx.lpAttributeList = attributeList;

                var mutableCommandLine = new StringBuilder(commandLine, commandLine.Length + 32);
                PROCESS_INFORMATION processInfo;
                bool created = CreateProcessW(
                    null, mutableCommandLine, IntPtr.Zero, IntPtr.Zero, false,
                    EXTENDED_STARTUPINFO_PRESENT, IntPtr.Zero, null,
                    ref startupInfoEx, out processInfo);

                if (!created)
                {
                    Console.Error.WriteLine("CreateProcessW failed: " + Marshal.GetLastWin32Error());
                    return 2;
                }

                processHandle = processInfo.hProcess;
                threadHandle = processInfo.hThread;
                Console.WriteLine("[i] MiniTerm: pseudo console 0x" + pseudoConsole.ToString("X")
                    + ", child pid " + processInfo.dwProcessId);

                // Documented lifecycle: free our copies of the "server" ends
                // only after CreateProcess has succeeded.
                CloseHandle(inputReadSide);
                inputReadSide = IntPtr.Zero;
                CloseHandle(outputWriteSide);
                outputWriteSide = IntPtr.Zero;

                byte[] captured = ReadAvailableOutput(outputReadSide, TimeSpan.FromSeconds(8));
                string transcript = RenderTranscript(captured);
                Console.WriteLine("[i] MiniTerm transcript (" + captured.Length + " bytes): " + transcript);

                bool markerObserved = Encoding.ASCII.GetString(captured).Contains(marker);
                Console.WriteLine(markerObserved
                    ? "[+] MiniTerm PASS: marker observed in transcript"
                    : "[x] MiniTerm FAIL: marker NOT observed in transcript (same symptom as fixture)");
                return markerObserved ? 0 : 1;
            }
            finally
            {
                if (inputReadSide != IntPtr.Zero) CloseHandle(inputReadSide);
                if (inputWriteSide != IntPtr.Zero) CloseHandle(inputWriteSide);
                if (outputReadSide != IntPtr.Zero) CloseHandle(outputReadSide);
                if (outputWriteSide != IntPtr.Zero) CloseHandle(outputWriteSide);
                if (pseudoConsole != IntPtr.Zero) ClosePseudoConsole(pseudoConsole);
                if (attributeList != IntPtr.Zero) DeleteProcThreadAttributeList(attributeList);
                if (threadHandle != IntPtr.Zero) CloseHandle(threadHandle);
                if (processHandle != IntPtr.Zero) CloseHandle(processHandle);
            }
        }

        private static byte[] ReadAvailableOutput(IntPtr outputReadSide, TimeSpan timeout)
        {
            using (var handle = new Microsoft.Win32.SafeHandles.SafeFileHandle(outputReadSide, false))
            using (var stream = new FileStream(handle, FileAccess.Read, 4096, false))
            {
                var buffer = new byte[4096];
                var collected = new MemoryStream();
                var deadline = DateTime.UtcNow + timeout;

                var readTask = stream.ReadAsync(buffer, 0, buffer.Length);
                while (DateTime.UtcNow < deadline)
                {
                    if (readTask.Wait(200))
                    {
                        int read = readTask.Result;
                        if (read <= 0)
                        {
                            break;
                        }
                        collected.Write(buffer, 0, read);
                        readTask = stream.ReadAsync(buffer, 0, buffer.Length);
                    }
                }
                return collected.ToArray();
            }
        }

        private static string RenderTranscript(byte[] bytes)
        {
            var sb = new StringBuilder();
            foreach (byte b in bytes)
            {
                if (b == 0x1B)
                {
                    sb.Append("^[");
                }
                else if (b >= 0x20 && b < 0x7F)
                {
                    sb.Append((char)b);
                }
                else if (b == (byte)'\r' || b == (byte)'\n')
                {
                    sb.Append((char)b);
                }
                else
                {
                    sb.Append("\\x").Append(b.ToString("X2"));
                }
            }
            return sb.ToString();
        }
    }
}
