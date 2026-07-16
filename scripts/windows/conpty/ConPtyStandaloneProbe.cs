using System;
using System.Collections;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using ApmConPty;

internal static class ConPtyStandaloneProbe
{
    private const int DiagnosticLineLimit = 32;
    private const int DiagnosticTextLimit = 512;

    private static readonly string[] RemovedEnvironmentVariables =
    {
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "DISPLAY",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "CI",
        "GITHUB_ACTIONS",
        "TRAVIS",
        "JENKINS_URL",
        "BUILDKITE",
        "APM_NON_INTERACTIVE",
        "APM_E2E_TESTS",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_APM_PAT",
        "ADO_APM_PAT",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GIT_HTTP_EXTRAHEADER",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    };

    public static int Main()
    {
        ConPtyProcess session = null;
        try
        {
            string marker = "CONPTY_SELFTEST_" + Guid.NewGuid().ToString("N").Substring(0, 12);
            string windowsDirectory = Environment.GetEnvironmentVariable("WINDIR");
            if (string.IsNullOrEmpty(windowsDirectory))
            {
                throw new InvalidOperationException("WINDIR is not set");
            }

            string commandInterpreter = Path.Combine(windowsDirectory, "System32", "cmd.exe");
            string commandLine = "\"" + commandInterpreter + "\" /c \"echo " + marker
                + " & ping -n 3 127.0.0.1 >nul & exit /b 7\"";

            session = ConPtyProcess.Start(
                commandLine,
                Environment.CurrentDirectory,
                BuildHumanLikeEnvironment());

            Thread.Sleep(700);
            session.Resize(100, 24);
            Thread.Sleep(200);
            session.Resize(120, 32);

            var wait = session.WaitForText(
                text => text.IndexOf(marker, StringComparison.Ordinal) >= 0,
                10000,
                200);
            bool exited = session.WaitForExit(10000, out int exitCode);
            string transcript = wait.Item2 + session.ReadAvailable(500);
            bool matched = transcript.IndexOf(marker, StringComparison.Ordinal) >= 0;

            Console.WriteLine("[i] Standalone ConPTY marker: " + marker);
            Console.WriteLine(
                "[i] Standalone ConPTY handle: " + session.PseudoConsoleHandleHex
                + ", child pid: " + session.ProcessId.ToString(CultureInfo.InvariantCulture));
            Console.WriteLine(
                "[i] Standalone ConPTY result: matched=" + matched
                + ", exited=" + exited
                + ", exitCode=" + exitCode.ToString(CultureInfo.InvariantCulture));
            Console.WriteLine(
                "[i] Standalone ConPTY transcript (bounded): "
                + ToBoundedAscii(transcript, DiagnosticTextLimit));

            string[] diagnostics = session.GetDiagnostics();
            int diagnosticCount = Math.Min(diagnostics.Length, DiagnosticLineLimit);
            for (int index = 0; index < diagnosticCount; index++)
            {
                Console.WriteLine(
                    "[i] Standalone ConPTY trace: "
                    + ToBoundedAscii(diagnostics[index], DiagnosticTextLimit));
            }
            if (diagnostics.Length > diagnosticCount)
            {
                Console.WriteLine(
                    "[i] Standalone ConPTY trace: "
                    + (diagnostics.Length - diagnosticCount).ToString(CultureInfo.InvariantCulture)
                    + " additional line(s) omitted");
            }

            if (!matched)
            {
                Console.WriteLine("[x] Standalone ConPTY marker capture failed");
                return 1;
            }
            if (!exited)
            {
                Console.WriteLine("[x] Standalone ConPTY child did not exit before the timeout");
                return 1;
            }
            if (exitCode != 7)
            {
                Console.WriteLine("[x] Standalone ConPTY child exit code was not 7");
                return 1;
            }

            Console.WriteLine("[+] Standalone ConPTY marker capture and exit-code assertions passed");
            return 0;
        }
        catch (Exception exception)
        {
            Console.WriteLine(
                "[x] Standalone ConPTY probe failed: "
                + ToBoundedAscii(
                    exception.GetType().Name + ": " + exception.Message,
                    DiagnosticTextLimit));
            return 1;
        }
        finally
        {
            if (session != null)
            {
                session.Dispose();
            }
        }
    }

    private static IDictionary BuildHumanLikeEnvironment()
    {
        var environment = new Hashtable(StringComparer.OrdinalIgnoreCase);
        foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            environment[(string)entry.Key] = (string)entry.Value;
        }

        foreach (string name in RemovedEnvironmentVariables)
        {
            environment.Remove(name);
        }

        var keys = new string[environment.Keys.Count];
        environment.Keys.CopyTo(keys, 0);
        foreach (string name in keys)
        {
            if (name.StartsWith("GITHUB_APM_PAT_", StringComparison.OrdinalIgnoreCase)
                || name.StartsWith("GIT_CONFIG_KEY_", StringComparison.OrdinalIgnoreCase)
                || name.StartsWith("GIT_CONFIG_VALUE_", StringComparison.OrdinalIgnoreCase))
            {
                environment.Remove(name);
            }
        }

        return environment;
    }

    private static string ToBoundedAscii(string value, int limit)
    {
        if (string.IsNullOrEmpty(value))
        {
            return "(empty)";
        }

        var result = new StringBuilder(Math.Min(value.Length, limit));
        foreach (char character in value)
        {
            string encoded;
            if (character >= ' ' && character <= '~')
            {
                encoded = character.ToString();
            }
            else if (character == '\r')
            {
                encoded = "\\r";
            }
            else if (character == '\n')
            {
                encoded = "\\n";
            }
            else if (character == '\t')
            {
                encoded = "\\t";
            }
            else if (character <= byte.MaxValue)
            {
                encoded = "\\x" + ((int)character).ToString("X2", CultureInfo.InvariantCulture);
            }
            else
            {
                encoded = "\\u" + ((int)character).ToString("X4", CultureInfo.InvariantCulture);
            }

            if (result.Length + encoded.Length > limit)
            {
                result.Append("...");
                break;
            }
            result.Append(encoded);
        }

        return result.ToString();
    }
}
