"""Start GUI applications through preinstalled interactive scheduled tasks."""
import json
import os
import subprocess
from .protocol import ControlError, local_call, make_request

TASKS = {'blacs': 'LabscriptAgent-BLACS', 'runmanager': 'LabscriptAgent-Runmanager'}


def start_app(app):
    if os.name != 'nt':
        raise ControlError('Application start requires Windows')
    try:
        result = local_call(app, make_request('status'), timeout=2)
    except Exception:
        result = None
    if result and result.get('ok'):
        if result['result'].get('closing'):
            raise ControlError('Application is shutting down; wait for it to exit before starting it')
        return dict(already_running=True, pid=result['result']['pid'])
    # A failed API is not evidence that the GUI is stopped. Refuse duplicates.
    query = "$p=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '" + app + r"[\\/]__main__\.py|\-m " + app + r"( |$)' }; @($p | Select-Object ProcessId,SessionId) | ConvertTo-Json -Compress"
    proc = subprocess.run(['powershell', '-NoProfile', '-Command', query], capture_output=True, text=True, check=True)
    if proc.stdout.strip():
        parsed = json.loads(proc.stdout)
        if parsed:
            raise ControlError('Application process exists but control API is unavailable; do not launch a duplicate: ' + proc.stdout.strip())
    subprocess.run(['schtasks', '/Run', '/TN', TASKS[app]], check=True, capture_output=True, text=True)
    return {'accepted': True, 'task': TASKS[app], 'requires_logged_on_user': True}
