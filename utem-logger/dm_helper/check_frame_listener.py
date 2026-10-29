"""CHECK 1: does a data-changed listener fire on every new frame of the live View?

Run in DM with Python as the language, a View running and the View window in front. It attaches a
listener to that image for 10 s and prints how many data-changed events it saw. At the View's frame
rate (say 10 fps) expect about 100; 0 means this GMS does not raise the event for live frames.
Copy the Output window text into the project.

The listener API below is the one Gatan uses in its own published examples ("Live Simple Copy.py",
"Live Radial Profile.py" on gatan.com): a DM.Py_ScriptObject subclass with HandleDataChangedEvent,
registered with ImageHandleDataChangedEvent. Gatan's examples warn that GMS 3.5.0 and 3.5.1 crash on
listeners, and that unregistering differs before and after 3.5.2; both are handled here.
"""
import time

import DigitalMicrograph as DM


def dm_version():
    DM.ExecuteScriptString(
        'number minor, major, bug\nGetApplicationVersion(major, minor, bug)\n'
        'GetPersistentTagGroup().TagGroupSetTagAsLong("Python_Temp:DM_Version_Major", major)\n'
        'GetPersistentTagGroup().TagGroupSetTagAsLong("Python_Temp:DM_Version_Minor", minor)\n'
        'GetPersistentTagGroup().TagGroupSetTagAsLong("Python_Temp:DM_Version_bugVersion", bug)')
    tg = DM.GetPersistentTagGroup()
    return tuple(int(tg.GetTagAsString("Python_Temp:DM_Version_" + k)[1])
                 for k in ("Major", "Minor", "bugVersion"))


class FrameListener(DM.Py_ScriptObject):
    def __init__(self):
        DM.Py_ScriptObject.__init__(self)
        self.n = 0
        self.last = 0.0

    def HandleDataChangedEvent(self, flags, image):
        self.n += 1
        self.last = time.time()

    def __del__(self):
        DM.Py_ScriptObject.__del__(self)


ver = dm_version()
DM.Result("GMS %d.%d.%d\n" % ver)
if ver[:2] == (3, 5) and ver[2] in (0, 1):
    DM.Result("GMS 3.5.0 / 3.5.1 crash on listeners (Gatan's own warning): not running this check\n")
else:
    img = DM.GetFrontImage()
    DM.Result("listening on %s for 10 s ...\n" % img.GetName())
    listener = FrameListener()
    lid = listener.ImageHandleDataChangedEvent(img, "pythonplugin")
    t0 = time.time()
    time.sleep(10)
    n = listener.n
    if ver >= (3, 5, 2):
        listener.UnregisterAllListeners()
    else:
        del listener
    DM.DoEvents()
    DM.Result("data-changed events in 10 s: %d\n" % n)
    # If this prints a sensible count, the helper can record the time of the last event instead of
    # sampling pixels: tell the thread and it gets added.
