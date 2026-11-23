

if _LOG:
    def _argv_hook(event, args):
        if event == "subprocess.Popen":
            with open(_LOG + ".argv", "a", encoding="utf-8") as fh:
                fh.write(repr(args[1])[:300] + "\n")
    sys.addaudithook(_argv_hook)
