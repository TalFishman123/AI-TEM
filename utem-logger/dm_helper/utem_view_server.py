"""Runs INSIDE DigitalMicrograph's Python. Hands the current live View image of the Orius to the UTEM
state logger on request, over a local TCP socket. It never acquires: it only copies the image DM is
already showing, so the operator's live view is not interrupted.

How to run it (GMS 3.4 or newer, with Python enabled):
    DM: File > Open this file, choose Python as the script language, Execute. The Output window shows
    "utem view server: listening on 127.0.0.1:47614" and the script returns at once; the server keeps
    running inside DM as a background thread until DM is closed.
    To start it with DM: File > Install Script... on this file as a menu command, then run it once after
    each DM start (or as a startup script if your GMS offers it for Python scripts).

Protocol (one request per connection): the logger sends one JSON line, the helper answers one JSON
line and, for "grab", the raw image bytes.
    {"cmd": "probe"}  -> {"ok": true, "live": bool, "age_s": s since the view image last changed,
                          "name": DM image name, "shape": [h, w], "dtype": "float32", "tags": {...}}
    {"cmd": "grab"}   -> the same header plus "nbytes", then nbytes of the image (C order, little-endian)
    {"cmd": "status"} -> {"ok": true, "version": ..., "images": [names DM has open]}

Which image is the live view: the first open image whose name contains VIEW_NAME (default "View";
set it to your camera's view-window name if needed). A background thread samples that image twice a
second (one pixel in 64) to know when it last changed, so "live" is "changed in the last LIVE_S s".
"""
import json
import os
import socket
import struct
import threading
import time
import traceback

import numpy as np

try:
    import DigitalMicrograph as DM
except ImportError:  # not inside DM: the tests run this file with a fake DM module
    DM = None

VERSION = "0.1.0"
HOST, PORT = "127.0.0.1", 47614
VIEW_NAME = "View"
LIVE_S = 2.0
SAMPLE_S = 0.5
BLOCKING = False     # True: keep the script running in the foreground instead of a thread (if your
                     # GMS refuses DM calls from a background thread, set this and leave the script open)

TAG_KEYS = {     # DM tag path -> name in the reply (missing tags are left out)
    "Acquisition:Parameters:High Level:Exposure (s)": "exposure_s",
    "Acquisition:Parameters:High Level:Binning": "binning",
    "Acquisition:Device:Name": "camera",
    "Acquisition:Device:Source": "source",
}

_state = {"last_change": 0.0, "digest": None, "name": None}
_lock = threading.Lock()


def _log(msg):
    try:
        DM.Result("utem view server: %s\n" % msg)
    except Exception:  # noqa: BLE001
        print("utem view server:", msg)


# ---- DM access
def open_images():
    """Names of every image DM has open, via DM script (the Python module has no list call)."""
    if DM is None:
        return []
    DM.ExecuteScriptString(
        'number n = CountImageDocuments(); string s = ""; for (number i = 0; i < n; i++) '
        '{ ImageDocument d = GetImageDocument(i); if (d.ImageDocumentCountImages() > 0) '
        '{ image im := d.ImageDocumentGetImage(0); s += im.ImageGetID() + "\\t" + im.ImageGetName() + "\\n"; } } '
        'GetPersistentTagGroup().TagGroupSetTagAsString("utem_view_server:images", s);')
    s = DM.GetPersistentTagGroup().GetTagAsString("utem_view_server:images")[1]
    out = []
    for line in s.splitlines():
        if "\t" in line:
            i, name = line.split("\t", 1)
            out.append((int(i), name))
    return out


def find_view():
    """-> DM image object of the live view, or None."""
    for img_id, name in open_images():
        if VIEW_NAME.lower() in name.lower():
            return DM.FindImageByID(img_id)
    return None


def read_tags(img):
    tags = {}
    tg = img.GetTagGroup()
    for path, key in TAG_KEYS.items():
        try:
            ok, v = tg.GetTagAsFloat(path)
            if not ok:
                ok, v = tg.GetTagAsString(path)
            if ok:
                tags[key] = v
        except Exception:  # noqa: BLE001
            pass
    return tags


def digest(arr):
    sub = arr[::8, ::8]
    return (float(sub.sum()), float(sub[::4, ::4].std()))


def sample_loop():
    """Keeps _state["last_change"] fresh: when the view image's content changes, the view is live."""
    while True:
        try:
            img = find_view()
            if img is not None:
                arr = img.GetNumArray()
                d = digest(arr)
                with _lock:
                    if d != _state["digest"]:
                        _state["digest"], _state["last_change"] = d, time.time()
                    _state["name"] = img.GetName()
            else:
                with _lock:
                    _state["name"] = None
        except Exception as e:  # noqa: BLE001
            _log("sample: %s" % e)
        time.sleep(SAMPLE_S)


def header(img):
    arr = img.GetNumArray()
    with _lock:
        age = time.time() - _state["last_change"]
    return arr, {"ok": True, "live": age <= LIVE_S, "age_s": round(age, 2), "name": img.GetName(),
                 "shape": list(arr.shape), "dtype": str(arr.dtype), "tags": read_tags(img),
                 "t": time.time(), "version": VERSION}


# ---- the server
def handle(conn):
    conn.settimeout(5.0)
    f = conn.makefile("rb")
    try:
        req = json.loads(f.readline().decode("utf-8") or "{}")
        cmd = req.get("cmd")
        if cmd == "status":
            reply = {"ok": True, "version": VERSION, "images": [n for _, n in open_images()]}
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        elif cmd in ("probe", "grab"):
            img = find_view()
            if img is None:          # not an error: nobody is viewing
                conn.sendall((json.dumps({"ok": True, "live": False, "name": None,
                                          "note": "no image named *%s* is open" % VIEW_NAME}) + "\n").encode("utf-8"))
                return
            arr, h = header(img)
            if cmd == "grab":
                data = np.ascontiguousarray(arr).astype(arr.dtype.newbyteorder("<")).tobytes()
                h["nbytes"] = len(data)
                conn.sendall((json.dumps(h) + "\n").encode("utf-8"))
                conn.sendall(data)
            else:
                conn.sendall((json.dumps(h) + "\n").encode("utf-8"))
        else:
            conn.sendall((json.dumps({"ok": False, "error": "unknown cmd %r" % cmd}) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001
        _log(traceback.format_exc())
        try:
            conn.sendall((json.dumps({"ok": False, "error": traceback.format_exc().splitlines()[-1]}) + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(2)
    _log("listening on %s:%d" % (HOST, PORT))
    while True:
        conn, _ = srv.accept()
        handle(conn)


def main():
    threading.Thread(target=sample_loop, name="utem-view-sample", daemon=True).start()
    if BLOCKING:
        serve()
    else:
        threading.Thread(target=serve, name="utem-view-server", daemon=True).start()
        _log("running in the background; close DM to stop it")


if not os.environ.get("UTEM_VIEW_SERVER_NO_AUTOSTART"):   # set by the tests, which start it themselves
    main()
