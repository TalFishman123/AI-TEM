// DM-script launcher for the UTEM View helper (utem_view_server.py, DM's Python).
//
// DM installs DM-script files only (File > Install Script is greyed out for a Python window, and
// Install Script File... compiles the file as DM-script), so this small script is what gets installed;
// it starts the Python helper. Install it with File > Install Script File... as
//   - a Menu command: click the menu item once after each DM start, or
//   - a Library script: it then runs when DM starts. "threaded" must be 1 for that, or DM's startup
//     would wait for the helper, which never ends.
// Copy utem_view_server.py into DM's plugin folder first (C:\Program Files\Gatan\Plugins on the UTEM;
// the "starting ..." line in the Output window shows the path it looks at), or edit "dir".
//
// Before installing, test it: File > Open this file (DM-script), Execute. The Output window should show
// the helper's listening message (quiet starts skip the automatic self-test); then, with the View window open,
// `python -m utem_logger --dm-status --config config.json` on the logger PC should print live: true.
// If --dm-status times out or says live: false with the View open, DM is not serving the helper's DM
// calls from the thread: set threaded = 0 and install as a Menu command only (never as a Library).
// To remove an installed copy: end DM, delete the package file DM wrote (the plugin folder, or
// C:\ProgramData\Gatan\Plugins, named after the package name given when installing).

number threaded = 1;                                   // 1: start the helper in a DM-script thread and return at once
number startDelayS = 90;                               // threaded only: wait this long before starting the helper, so a
                                                       // Library start does not run anything while DM is still starting
                                                       // (set 0 for a menu command)
string dir = GetApplicationDirectory("plugin", 0);   // folder holding utem_view_server.py
string path = PathConcatenate(dir, "utem_view_server.py");

void launch(string pyPath) {
    // exec rather than runpy: DM's embedded Python has an empty sys.argv, which runpy.run_path trips on
    // QUIET_START: the helper makes no DM call until the logger's first request (a DM script run during DM's
    // startup fails and shows in DM's startup error dialog)
    string code = "import os; os.environ['UTEM_VIEW_SERVER_QUIET_START'] = '1'; p = r'" + pyPath + "'; exec(compile(open(p).read(), p, 'exec'), {'__name__': '__main__', '__file__': p})";
    try {
        ExecutePythonScriptString(code);
    } catch {
        Result("utem view server: launching Python failed (ExecutePythonScriptString raised); run the .py from a Python script window instead\n");
        break;
    }
    Result("utem view server: the helper ended\n");
}

class UtemViewServerThread : Thread {
    string pyPath;                                     // DM-script: a member may not reuse a global's name
    number delayS;
    void SetPath(object self, string p, number d) { pyPath = p; delayS = d; }
    void RunThread(object self) {
        if (delayS > 0) Sleep(delayS);
        launch(pyPath);
    }
}

if (!DoesFileExist(path)) {
    Result("utem view server: " + path + " not found; copy utem_view_server.py there (or edit dir in this script)\n");
} else if (threaded) {
    object t = alloc(UtemViewServerThread);
    t.SetPath(path, startDelayS);
    t.StartThread();
    if (startDelayS > 0) Result("utem view server: starting " + path + " in a DM thread after " + startDelayS + " s\n");
    else Result("utem view server: starting " + path + " in a DM thread\n");
} else {
    Result("utem view server: starting " + path + " in this script thread\n");
    launch(path);
}
