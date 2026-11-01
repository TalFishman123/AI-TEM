"""Runs INSIDE DigitalMicrograph's Python. Hands the current live View image of the Orius to the UTEM
state logger on request, over a TCP socket. It never acquires: it only copies the image DM is already
showing, so the operator's live view is not interrupted.

How to run it (GMS 3.4 or newer, with Python enabled):
    DM: File > Open this file, choose Python as the script language, Execute. The Output window shows
    "utem view server: listening on 127.0.0.1:47614". The script keeps running (its window shows busy)
    and everything happens from the script thread, the one DM serves reliably; stop it with the script
    window's Stop or by closing DM. DM runs one Python script at a time, so stop it before executing
    it again. A script window keeps the text it loaded: after replacing this file, open it again.
    If the logger runs on another PC than DM, set HOST = "0.0.0.0" here and dm_view.host in the
    logger's config.json to this PC's address.

Protocol (one request per connection): the logger sends one JSON line, the helper answers one JSON
line and, for "grab", the raw image bytes.
    {"cmd": "probe"}  -> {"ok": true, "live": bool, "name": DM image name}
    {"cmd": "grab"}   -> the same plus "tags", "shape", "dtype", "same_as_last", "nbytes", then nbytes
                          of the image (C order, little-endian)
    {"cmd": "status"} -> {"ok": true, "version": ..., "gms": [major, minor, bug], "live_by": "open",
                          "images": [names DM has open], "view": name or null, "live": bool}

Which image is the live view: the first open image whose name contains VIEW_NAME ("Orius"; the window
is "GNY: Orius SC200 3 Image" on the UTEM).

What "live" means: the View window is open. That is all. The helper makes no attempt to tell whether
the View is running or stopped: a data-changed listener on the View image, however little it did,
crashed GMS 3.4.4 within a minute, and sampling the image for change is polling DM while it
acquires. So a frame taken while the View is stopped is the last frame the View showed; the grab reply
(and so the frame's metadata in the log) carries "same_as_last", true when the pixels equal the
previous grab's, so such frames can be told apart afterwards.

DM calls: a probe lists DM's images (one DM-script); a grab lists them and copies the View (one
GetNumArray) and its tags. All from the script thread, on request only; nothing runs between requests.
"""
import builtins
import json
import os
import socket
import threading
import time
import traceback

import numpy as np

try:
    import DigitalMicrograph as DM
except ImportError:  # not inside DM: the tests run this file with a fake DM module
    DM = None

VERSION = "0.4.2"
QUIET_START = os.environ.pop("UTEM_VIEW_SERVER_QUIET_START", "0") == "1"
_quiet_start = QUIET_START
_dm_thread_ident = None
_dm_initialized = False
HOST, PORT = "127.0.0.1", 47614   # HOST = "0.0.0.0" when the logger runs on another PC than DM
VIEW_NAME = "Orius"               # a word from the View window's name ("GNY: Orius SC200 3 Image" on the UTEM)
COPY_S = 0.3                      # a grab reuses the last copy of the View when it is younger than this
BLOCKING = True      # True: the script keeps running (its window shows busy) and every DM call is made
                     # from the script thread, which is the one DM serves reliably (the UTEM's GMS 3.4.4
                     # ignored DM calls from a background thread). Only one Python script runs in DM at
                     # a time, so stop this one (the script window's Stop, or close DM) before executing
                     # it again or another Python script. False: return at once and answer from a
                     # background thread, which then makes the DM calls.

TAG_KEYS = {     # DM tag path -> name in the reply (missing tags are left out)
    "Acquisition:Parameters:High Level:Exposure (s)": "exposure_s",
    "Acquisition:Parameters:High Level:Binning": "binning",
    "Acquisition:Device:Name": "camera",
    "Acquisition:Device:Source": "source",
}

# Executing this script again must not leave the previous run behind: DM keeps one Python interpreter,
# so the previous run is found through a builtins attribute and told to stop.
REGISTRY = "_utem_view_server"
_run = {"stop": False, "socket": None, "version": VERSION}

_state = {"gms": (0, 0, 0), "images": [], "name": None,
          "frame": None, "copied_t": 0.0, "digest": None, "same_as_last": False, "tags": {}}
_lock = threading.Lock()


def _log(msg):
    if _quiet_start or threading.get_ident() != _dm_thread_ident:
        print("utem view server:", msg)
        return
    try:
        DM.Result("utem view server: %s\n" % msg)
    except Exception:  # noqa: BLE001
        print("utem view server:", msg)


_logged = set()


def _log_once(msg):
    if msg not in _logged:
        _logged.add(msg)
        _log(msg)


# ---- DM access
LIST_SCRIPT = """number n = CountImageDocuments()
TagGroup p = GetPersistentTagGroup()
p.TagGroupSetTagAsLong("utem_view_server:count", n)
number i
for (i = 0; i < n; i++)
{
    ImageDocument d = GetImageDocument(i)
    string key = "utem_view_server:img" + i
    if (d.ImageDocumentCountImages() > 0)
    {
        image im := d.ImageDocumentGetImage(0)
        p.TagGroupSetTagAsLong(key + ":id", im.ImageGetID())
        p.TagGroupSetTagAsString(key + ":name", im.ImageGetName())
    }
    else
    {
        p.TagGroupSetTagAsLong(key + ":id", -1)
        p.TagGroupSetTagAsString(key + ":name", "")
    }
}
"""


def open_images():
    """(id, name) of every image DM has open, via DM script (the Python module has no list call).
    Raises when the script fails on this GMS."""
    if DM is None:
        return []
    DM.ExecuteScriptString(LIST_SCRIPT)
    tg = DM.GetPersistentTagGroup()
    n = int(float(tg.GetTagAsString("utem_view_server:count")[1] or 0))
    out = []
    for i in range(n):
        img_id = int(float(tg.GetTagAsString("utem_view_server:img%d:id" % i)[1] or -1))
        name = tg.GetTagAsString("utem_view_server:img%d:name" % i)[1]
        if img_id >= 0:
            out.append((img_id, name))
    return out


def front_image():
    """Fallback when the list script does not compile: DM's front image, if it is the View."""
    try:
        img = DM.GetFrontImage()
    except Exception:  # noqa: BLE001 - no image open
        return None
    name = img.GetName()
    if VIEW_NAME.lower() not in name.lower():
        return None
    return img


def find_view():
    """-> the DM image object of the View window, or None when no such window is open. Records the
    names DM has open and the View's name."""
    try:
        images = open_images()
    except Exception as e:  # noqa: BLE001 - the DM-script did not compile on this GMS
        _log_once("listing DM's images failed (%s); using the front image when its name contains %r"
                  % (str(e).strip().replace("\n", " / "), VIEW_NAME))
        img = front_image()
        with _lock:
            _state["name"] = img.GetName() if img is not None else None
        return img
    with _lock:
        _state["images"] = [name for _, name in images]
    for img_id, name in images:
        if VIEW_NAME.lower() in name.lower():
            with _lock:
                _state["name"] = name
            return DM.FindImageByID(img_id)
    with _lock:
        _state["name"] = None
    return None


def gms_version():
    """(major, minor, bug) of the running GMS, (0, 0, 0) when it cannot be asked."""
    if DM is None:
        return (0, 0, 0)
    try:
        DM.ExecuteScriptString(
            'number major, minor, bug; GetApplicationVersion(major, minor, bug); '
            'TagGroup p = GetPersistentTagGroup(); p.TagGroupSetTagAsLong("utem_view_server:gms_major", major); '
            'p.TagGroupSetTagAsLong("utem_view_server:gms_minor", minor); p.TagGroupSetTagAsLong("utem_view_server:gms_bug", bug);')
        tg = DM.GetPersistentTagGroup()
        major, minor, bug = (int(float(tg.GetTagAsString("utem_view_server:gms_" + k)[1])) for k in ("major", "minor", "bug"))
        if minor >= 10:              # GMS 3.4.4 build 3448 reports (3, 44, 3448)
            major, minor, bug = major, minor // 10, minor % 10
        return (major, minor, bug)
    except Exception as e:  # noqa: BLE001
        _log("could not read the GMS version: %s" % e)
        return (0, 0, 0)


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


def keep_copy(img):
    """Copy the View (unless the last copy is younger than COPY_S) and its tags into _state."""
    now = time.time()
    if now - _state["copied_t"] >= COPY_S:
        arr = np.array(img.GetNumArray(), copy=True)
        d = digest(arr)
        with _lock:
            _state["same_as_last"] = d == _state["digest"]
            _state["frame"], _state["copied_t"], _state["digest"] = arr, now, d
    tags = read_tags(img)
    with _lock:
        _state["tags"] = tags


# ---- the server
def answer(cmd):
    """-> (reply dict, frame or None). Makes the DM calls (listing, and for a grab the copy)."""
    global _quiet_start, _dm_initialized, _dm_thread_ident
    _dm_thread_ident = threading.get_ident()
    _quiet_start = False
    if not _dm_initialized:
        _state["gms"] = gms_version()
        _dm_initialized = True
    img = find_view()
    with _lock:
        name = _state["name"]
    h = {"ok": True, "live": img is not None, "live_by": "open", "name": name, "t": time.time(), "version": VERSION}
    if img is None:
        h["note"] = "no image named *%s* is open" % VIEW_NAME
        return h, None
    if cmd != "grab":
        return h, None
    keep_copy(img)
    with _lock:
        arr, h["tags"], h["same_as_last"] = _state["frame"], dict(_state["tags"]), _state["same_as_last"]
        h["copy_age_s"] = round(time.time() - _state["copied_t"], 2)
    h["shape"], h["dtype"] = list(arr.shape), str(arr.dtype)
    return h, arr


def handle(conn):
    conn.settimeout(5.0)
    f = conn.makefile("rb")
    try:
        req = json.loads(f.readline().decode("utf-8") or "{}")
        cmd = req.get("cmd")
        if cmd == "status":
            h, _arr = answer("probe")
            with _lock:
                images = list(_state["images"])
            reply = {"ok": True, "version": VERSION, "gms": list(_state["gms"]), "live_by": "open",
                     "view_name": VIEW_NAME, "images": images, "view": h["name"], "live": h["live"]}
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        elif cmd in ("probe", "grab"):
            h, arr = answer(cmd)
            if arr is not None:
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


def self_test():
    """From a short-lived thread: connect to our own port and ask for status, so the Output window
    says whether the port really reaches this run (on Windows a bind can silently succeed on a port a
    crashed DM still holds; connections then go to the dead process)."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as c:
            c.sendall(b'{"cmd": "status"}\n')
            line = c.makefile("rb").readline().decode("utf-8", "replace").strip()
        h = json.loads(line or "{}")
        if h.get("version") == VERSION:
            _log("self-test ok: the port answers from this run")
        else:
            _log("self-test: the port answered, but from another run (version %s): a previous DM "
                 "process still holds port %d. Close it (Task Manager: DigitalMicrograph) and run again." % (h.get("version"), PORT))
    except Exception as e:  # noqa: BLE001
        _log("self-test failed: %s. Another process may hold port %d: in a console, "
             "netstat -ano | findstr %d" % (e, PORT, PORT))


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):       # Windows: fail if any process holds the port, instead of sharing it
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as e:
        _log("cannot bind port %d: %s. Another process holds it (a DM that crashed but is still running?): "
             "in a console, netstat -ano | findstr %d shows its PID; end it in Task Manager and run again." % (PORT, e, PORT))
        raise
    srv.listen(2)
    srv.settimeout(1.0)
    _run["socket"] = srv
    _log("listening on %s:%d" % (HOST, PORT))
    if not QUIET_START:
        threading.Timer(0.5, self_test).start()
    try:
        while not _run["stop"]:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:              # closed by a newer run
                break
            handle(conn)
    finally:
        _run["stop"] = True
        srv.close()


def stop_previous_run():
    global _run
    prev = getattr(builtins, REGISTRY, None)
    if prev is not None and not prev.get("stop"):
        prev["stop"] = True
        try:
            if prev.get("socket") is not None:
                prev["socket"].close()
        except Exception:  # noqa: BLE001
            pass
        _log("stopped the previous run (version %s)" % prev.get("version"))
        time.sleep(1.5)              # its threads notice the flag within an accept timeout
    _run = {"stop": False, "socket": None, "version": VERSION}
    setattr(builtins, REGISTRY, _run)


def main():
    global _dm_thread_ident, _dm_initialized, _quiet_start
    _dm_thread_ident = threading.get_ident()
    _dm_initialized = False
    _quiet_start = QUIET_START
    _log("version %s starting" % VERSION)
    stop_previous_run()
    # In quiet mode even version queries, image listing and DM.Result are deferred.
    # Do not run self_test either: its status request would immediately make DM calls.
    try:                             # say what is there now, so the setup can be checked in the Output window
        if QUIET_START:
            _log("quiet start: DM access deferred until the first external request")
        elif answer("probe")[0]["live"]:
            _log("the View window is open: %r" % _state["name"])
        else:
            _log("no image named *%s* is open (DM has: %s)" % (VIEW_NAME, ", ".join(_state["images"]) or "nothing"))
    except Exception as e:  # noqa: BLE001
        _log("looking for the View: %s" % e)
    if BLOCKING:
        _log("running in this script thread; stop the script (or close DM) to end it")
        try:
            serve()
        finally:
            _log("stopped")
    else:
        threading.Thread(target=serve, name="utem-view-server", daemon=True).start()
        _log("running in the background; close DM to stop it")


if not os.environ.get("UTEM_VIEW_SERVER_NO_AUTOSTART"):   # set by the tests, which start it themselves
    main()
