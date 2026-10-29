// DM-script wrapper that starts the Python helper (utem_view_server.py, same folder) when DM starts.
// Install once: File > Install Script... > this file > "Library". Library scripts run at every DM start.
// To confirm on the PC: whether this GMS version lets DM-script launch a Python script
// (ExecutePythonScriptString exists in GMS 3.4+ on some installs). If the Output window says
// "cannot launch Python from DM-script", install utem_view_server.py itself as a menu command instead
// (File > Install Script... > Python file > "Menu command") and click it once after DM opens.
string dir = GetApplicationDirectory("plugin", 0);   // edit if the .py lives elsewhere
string path = PathConcatenate(dir, "utem_view_server.py");
if (!DoesFileExist(path)) {
    Result("utem view server: " + path + " not found\n");
    exit(0);
}
number f = OpenFileForReading(path);
string code = "";
string line;
while (ReadFileLine(f, line)) code += line;
CloseFile(f);
try {
    ExecutePythonScriptString(code);
} catch {
    Result("utem view server: cannot launch Python from DM-script in this GMS; install utem_view_server.py as a menu command instead\n");
    break;
}
