"""A pretend microscope for testing the logger without PyJEM: the probe's values, a beam switch that
goes on and off, and bursts of knob turns like an operator aligning."""
from __future__ import annotations

import json
import random

from .pyjem_source import Unavailable

PROBE = {   # values from probes/pyjem/pyjem_probe_online_2026-10-13_17-50-21.json
    "HT3.GetHtValue": 200000.0, "GUN3.GetBeamSw": 0, "GUN3.GetFilamentVal": 2.342,
    "GUN3.GetEmissionCurrentValue": 0.0, "GUN3.GetFilamentCurrentValue": 0.0,
    "GUN3.GetAnode2CurrentValue": 0.03, "EOS3.GetFunctionMode": [2, "LOWMAG"],
    "EOS3.GetMagValue": [150, "X", "X150"], "EOS3.GetSpotSize": 0, "Def3.GetBeamBlank": 1,
    "Def3.GetCLA1": [31892, 30880], "Def3.GetCLA2": [36934, 41391], "Def3.GetCLs": [35800, 41392],
    "Def3.GetGunA2": [27724, 38472], "Def3.GetShifBal": [17856, 17664], "Def3.GetTiltBal": [20812, 20776],
    "Def3.GetPLA": [32364, 32864], "Lens3.GetCL3": 48146, "Lens3.GetOLf": 0,
    "Stage3.GetPos": [26903.64, -315.28, -794.74, 19.76, 0.0], "Apt3.GetPosition": [2729, 4074],
}


class MockSource:
    name = "mock"

    def __init__(self, seed=0, p_burst=0.05, p_beam=0.005, p_down=0.0):
        self.rng = random.Random(seed)
        self.state = json.loads(json.dumps(PROBE))
        self.state["GUN3.GetBeamSw"] = 1          # start thermionic, so frames are exercised
        self.p_burst, self.p_beam, self.p_down = p_burst, p_beam, p_down
        self.burst = 0
        self.down = 0

    def read(self):
        r, s = self.rng, self.state
        if self.down or r.random() < self.p_down:
            self.down = self.down - 1 if self.down else 3
            raise Unavailable("mock TEMCenter not answering")
        if r.random() < self.p_beam:
            s["GUN3.GetBeamSw"] ^= 1
        on = s["GUN3.GetBeamSw"] == 1
        s["GUN3.GetEmissionCurrentValue"] = round((95.0 if on else 0.0) + r.gauss(0, 0.5), 2)
        s["GUN3.GetFilamentCurrentValue"] = round((2.3 if on else 0.0) + r.gauss(0, 0.01), 3)
        s["Stage3.GetPos"][0] = round(26903.64 + r.gauss(0, 2), 2)        # jitter under tolerance
        if not self.burst and r.random() < self.p_burst:
            self.burst = r.randint(2, 6)                                 # an operator turns a knob
        if self.burst:
            self.burst -= 1
            key = r.choice(["Def3.GetCLA1", "Def3.GetCLA2", "Def3.GetCLs"])
            s[key] = [v + r.randint(-300, 300) for v in s[key]]
            if r.random() < 0.3:
                s["Lens3.GetCL3"] += r.randint(-200, 200)
        return json.loads(json.dumps(s)), {}

    def close(self):
        pass

