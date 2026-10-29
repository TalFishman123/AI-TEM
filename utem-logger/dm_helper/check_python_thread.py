"""CHECK 3: can a background Python thread inside DM call DM functions? The helper relies on it.
Run in DM with Python as the language. It starts a thread that asks DM for the front image's name
every second, 5 times, and prints the result. Then open or close an image and run it again.
Expected: five lines like "thread sees: Orius SC200 View". An error or a frozen DM means the helper
must run with BLOCKING = True (set at the top of utem_view_server.py).
Copy the Output window text into the project."""
import threading
import time

import DigitalMicrograph as DM


def work():
    for _ in range(5):
        try:
            img = DM.GetFrontImage()
            DM.Result("thread sees: %s %s\n" % (img.GetName(), img.GetNumArray().shape))
        except Exception as e:  # noqa: BLE001
            DM.Result("thread error: %r\n" % (e,))
        time.sleep(1)
    DM.Result("thread done\n")


threading.Thread(target=work, daemon=True).start()
DM.Result("script returned; the thread keeps going\n")
