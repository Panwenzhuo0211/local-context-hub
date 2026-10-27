using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Collections.Generic;

// A native executable entry point for CODEX_CLI_PATH. No vendor executable is changed.
class CodexRelay {
    static string Quote(string s) {
        var b = new StringBuilder("\""); int slash = 0;
        foreach (char c in s) {
            if (c == '\\') { slash++; continue; }
            if (c == '"') b.Append('\\', slash * 2 + 1);
            else b.Append('\\', slash);
            b.Append(c); slash = 0;
        }
        b.Append('\\', slash * 2); return b.Append('"').ToString();
    }
    static Thread Copy(Stream source, Stream target, bool close) {
        var t = new Thread(delegate() { try { var buffer=new byte[65536];int count;while((count=source.Read(buffer,0,buffer.Length))>0){target.Write(buffer,0,count);target.Flush();} if(close) target.Close(); } catch(IOException) {} catch(ObjectDisposedException) {} });
        t.IsBackground = true; t.Start(); return t;
    }
    static int Main(string[] args) {
        try {
            string folder = AppDomain.CurrentDomain.BaseDirectory;
            var cfg = new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(Path.Combine(folder,"codex-relay.json")));
            var all = new List<string>();
            all.Add(Path.Combine(folder,"stdio-bridge.cjs"));
            foreach(string k in new[]{"adapter","hub","runtime","home"}) {all.Add("--"+k);all.Add((string)cfg[k]);}
            all.Add("--"); all.Add((string)cfg["backend"]); all.AddRange(args);
            var p = new Process();
            p.StartInfo = new ProcessStartInfo((string)cfg["node"],string.Join(" ",all.ConvertAll(Quote).ToArray()));
            p.StartInfo.UseShellExecute=false;p.StartInfo.CreateNoWindow=true;
            p.StartInfo.RedirectStandardInput=true;p.StartInfo.RedirectStandardOutput=true;p.StartInfo.RedirectStandardError=true;
            p.Start();
            Copy(Console.OpenStandardInput(),p.StandardInput.BaseStream,true);
            var output=Copy(p.StandardOutput.BaseStream,Console.OpenStandardOutput(),false);
            var error=Copy(p.StandardError.BaseStream,Console.OpenStandardError(),false);
            p.WaitForExit();output.Join();error.Join();return p.ExitCode;
        } catch { return 1; }
    }
}
