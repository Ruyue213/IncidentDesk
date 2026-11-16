"""A managed child's durable identity survives its launcher's normal exit."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from incident_desk.actions import ActionExecutor, identity
from incident_desk.process_identity import process_birth
from incident_desk.projects import ProjectProfile
from incident_desk.settings import Settings


def test_reparented_service_keeps_verified_persistent_ownership(tmp_path):
    record = tmp_path / "child.json"
    source = Path(__file__).resolve().parents[1] / "src"
    child_code = """import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from incident_desk.actions import identity
from incident_desk.process_identity import process_birth
path=Path(sys.argv[2]); pending=path.with_suffix('.pending')
pending.write_text(json.dumps({'pid':os.getpid(),'original_parent':os.getppid(),
 'birth':process_birth(os.getpid()),'identity':identity(os.getpid())}))
pending.replace(path)
time.sleep(30)
"""
    parent_code = """import subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]],
 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
 start_new_session=True)
deadline=time.monotonic()+15
while not Path(sys.argv[3]).exists():
 if child.poll() is not None: raise RuntimeError('child exited before ready')
 if time.monotonic()>deadline:
  child.terminate(); child.wait(timeout=5); raise RuntimeError('child ready timeout')
 time.sleep(.02)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, child_code, str(source), str(record)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    saved = None
    try:
        output, error = parent.communicate(timeout=25)
        assert parent.returncode == 0, (output, error)
        saved = json.loads(record.read_text())
        assert saved["original_parent"] == parent.pid
        now_parent = subprocess.check_output(
            ["ps", "-p", str(saved["pid"]), "-o", "ppid="], text=True
        ).strip()
        assert int(now_parent) != parent.pid
        assert process_birth(saved["pid"]) == tuple(saved["birth"])
        assert identity(saved["pid"]) == saved["identity"]
        config = Settings(_env_file=None, api_key=None, local_dir=tmp_path / "state")
        executor = ActionExecutor(config, None)
        project = ProjectProfile(id="reparented", name="Reparented", root=tmp_path)
        state = executor.state_path(project)
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps(saved | {"phase": "READY", "operation_id": "synthetic-start"}))
        assert executor.managed(project)["pid"] == saved["pid"]
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)
        if saved is None and record.exists():
            saved = json.loads(record.read_text())
        if (
            saved
            and process_birth(saved["pid"]) == tuple(saved["birth"])
            and identity(saved["pid"]) == saved["identity"]
        ):
            os.kill(saved["pid"], signal.SIGTERM)
