"""One-time normal GUI close for apps started before the control API was installed.

Run in the logged-on Windows session with --app and the explicitly inspected PID.
Requires pywinauto in LabscriptAgentControl/bootstrap-deps. Normal operation uses
the API and does not require this helper or desktop automation.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import time
import traceback


def close_existing(app, pid):
    from pywinauto import Desktop
    desktop = Desktop(backend='uia')
    windows = [w for w in desktop.windows(visible_only=False) if w.process_id() == pid]
    expected = 'BLACS' if app == 'blacs' else 'MainWindow'
    windows = [w for w in windows if w.element_info.automation_id == expected]
    if len(windows) != 1:
        raise RuntimeError('Expected exactly one application window for the specified PID')
    window = windows[0]

    def control(suffix):
        found = [c for c in window.descendants() if c.element_info.automation_id.endswith('.' + suffix)]
        if len(found) != 1:
            raise RuntimeError('Missing or ambiguous control: ' + suffix)
        return found[0]

    abort = control('queue_abort_button' if app == 'blacs' else 'pushButton_abort')
    if abort.is_enabled():
        raise RuntimeError('Application is busy; no close request sent')
    if app == 'blacs':
        pause = control('queue_pause_button')
        if pause.get_toggle_state() == 0:
            pause.toggle()
        if pause.get_toggle_state() != 1:
            raise RuntimeError('Could not pause BLACS')
        time.sleep(1)
        if abort.is_enabled():
            raise RuntimeError('A shot is still in progress; queue left paused')

    # WM_CLOSE follows the application's ordinary save/worker shutdown path.
    ctypes.windll.user32.PostMessageW(window.handle, 0x0010, 0, 0)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        remaining = [w for w in desktop.windows(visible_only=False) if w.process_id() == pid]
        if not remaining:
            return {'closed_window': True, 'pid': pid, 'app': app}
        dialogs = remaining + [d for w in remaining for d in w.descendants(control_type='Window')]
        for dialog in dialogs:
            title = dialog.window_text()
            allowed = ('Save broken tab data?' if app == 'blacs' else 'Quit runmanager')
            if title != allowed:
                continue
            # Preserve existing settings; never accept unrelated dialogs.
            buttons = [c for c in dialog.descendants(control_type='Button')
                       if c.window_text().replace('&', '') == 'Yes']
            if len(buttons) != 1:
                raise RuntimeError('Cannot identify the save confirmation')
            buttons[0].invoke()
        time.sleep(0.5)
    raise RuntimeError('Normal close timed out; no process was killed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, choices=['blacs', 'runmanager'])
    parser.add_argument('--pid', required=True, type=int)
    args = parser.parse_args()
    directory = Path(os.environ['LOCALAPPDATA']) / 'LabscriptAgentControl'
    sys.path.insert(0, str(directory / 'bootstrap-deps'))
    try:
        result = close_existing(args.app, args.pid)
    except Exception:
        result = {'error': traceback.format_exc()}
    (directory / ('bootstrap-' + args.app + '.json')).write_text(json.dumps(result, indent=2))
