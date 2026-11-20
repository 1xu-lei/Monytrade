import faulthandler

with open("reports/_startup_trace.txt", "w", buffering=1) as fh:
    faulthandler.dump_traceback_later(8, exit=True, file=fh)
    import app

    fh.write("IMPORT OK\n")
