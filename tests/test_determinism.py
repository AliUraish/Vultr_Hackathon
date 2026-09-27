"""The make-or-break property: recorded runs replay hash-for-hash."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from replay_core import capsule
from replay_core.engine import step
from replay_core.hashing import clone
from replay_core.state import state_hash
from tests.driver import Driver

TICKS = 1200


def _fleet(seed: int = 3) -> Driver:
    d = Driver(seed=seed, job_seed=3)
    d.run(350)
    d.sim.request_chaos("rockfall")
    d.run(250)
    d.sim.request_chaos("grade_mixup")
    d.sim.request_chaos("road_damage")
    d.run(TICKS - 600)
    return d


@pytest.fixture(scope="module")
def fleet() -> Driver:
    return _fleet()


def test_same_seed_and_inputs_give_the_same_trajectory(fleet):
    again = _fleet()
    assert [r["hash"] for r in again.records] == [r["hash"] for r in fleet.records]


def test_seed_matters(fleet):
    other = _fleet(seed=4)
    assert [r["hash"] for r in other.records] != [r["hash"] for r in fleet.records]


def test_any_snapshot_restores_and_replays_hash_for_hash(fleet):
    by_tick = {r["tick"]: r for r in fleet.records}
    for snap_tick in (0, 350, 700):
        state = clone(fleet.snapshots[snap_tick])
        for t in range(snap_tick, TICKS):
            step(state, clone(by_tick[t + 1]["inputs"]))
            assert state_hash(state) == by_tick[t + 1]["hash"], f"diverged at {t + 1} from snapshot {snap_tick}"


def test_capsule_replays_identically_in_fresh_processes(fleet, tmp_path):
    failure = next(f for f in fleet.failures if f["type"] == "collision")
    cap = fleet.cut(failure)
    path = tmp_path / "capsule.json"
    path.write_text(json.dumps(cap))
    here = capsule.run(cap, keep_frames=False)
    assert here["matches_live"] and here["outcome"] == "reproduced"

    code = ("import json, sys; from replay_core import capsule; "
            "r = capsule.run(json.load(open(sys.argv[1])), keep_frames=False); "
            "print(r['trajectory_hash'], r['matches_live'], r['outcome'])")
    root = Path(__file__).resolve().parents[1]
    seen = set()
    for hash_seed in ("1", "2"):  # different dict/set hashing per process
        out = subprocess.run([sys.executable, "-c", code, str(path)], cwd=root, check=True,
                             env={**os.environ, "PYTHONHASHSEED": hash_seed},
                             capture_output=True, text=True).stdout.split()
        seen.add(tuple(out))
    assert seen == {(here["trajectory_hash"], "True", "reproduced")}


def test_tampered_capsule_is_rejected(fleet):
    failure = next(f for f in fleet.failures if f["type"] == "collision")
    cap = clone(fleet.cut(failure))
    cap["inputs"] = cap["inputs"][1:]
    with pytest.raises(capsule.CapsuleError):
        capsule.run(cap)
