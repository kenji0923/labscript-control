"""Small startup hook; retain Runmanager's existing remote API and close path."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .protocol import ControlError, ControlServer


class Controller:
    def __init__(self, app, remote):
        self.app = app
        self.remote = remote
        self.closing = False

    def command_gui(self, action, params):
        app = self.app
        busy = app.ui.pushButton_abort.isEnabled() or not app.compile_queue.empty()
        if action == 'status':
            return dict(app='runmanager', pid=os.getpid(), compiling=busy,
                        closing=self.closing, ready=not busy and not self.closing,
                        script=app.ui.lineEdit_labscript_file.text(),
                        run_shots=app.ui.checkBox_run_shots.isChecked(),
                        n_shots=int(app.n_shots) if app.n_shots is not None else None,
                        configuration=app.last_save_config_file,
                        active_groups=app.get_active_groups(interactive=False))
        if self.closing:
            raise ControlError('Runmanager is shutting down')
        if action in ('app.restart', 'app.stop'):
            if busy:
                raise ControlError('Runmanager is compiling; wait or abort compilation explicitly')
            autoload = app.exp_config.get('runmanager', 'autoload_config_file', fallback=None)
            target = autoload or app.last_save_config_file
            if not target:
                raise ControlError('No configuration save path is configured')
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.copy2(target, str(target) + '.before-agent-restart')
            # Save the current UI state to the autoload file so restart restores
            # the exact group selection and script, even when unsaved in the GUI.
            app.save_configuration(str(target))
            self.closing = True
            if action == 'app.restart':
                argv = [sys.executable] + sys.argv
                payload = base64.b64encode(json.dumps(argv).encode()).decode()
                subprocess.Popen([sys.executable, str(Path(__file__).with_name('restart_after_exit.py')),
                                  str(os.getpid()), payload], close_fds=True)
            from qtutils.qt.QtCore import QTimer
            QTimer.singleShot(300, app.ui.close)
            return dict(accepted=True, pid=os.getpid(), configuration=str(target))
        if action == 'compile':
            if busy:
                raise ControlError('Runmanager is already compiling')
            # Compilation is separate from hardware submission. The stock
            # compiler captures these values on engage; restore them afterwards.
            old_run = app.ui.checkBox_run_shots.isChecked()
            old_view = app.ui.checkBox_view_shots.isChecked()
            app.ui.checkBox_run_shots.setChecked(False)
            app.ui.checkBox_view_shots.setChecked(False)
            try:
                if not app.ui.pushButton_engage.isEnabled():
                    raise ControlError('Engage is disabled; resolve globals or script errors')
                app.on_engage_clicked()
                # The stock handler reports pre-compilation exceptions in the
                # output box instead of raising them to its caller.
                if not app.ui.pushButton_abort.isEnabled():
                    raise ControlError('Compilation was not accepted; see Runmanager output')
            finally:
                app.ui.checkBox_run_shots.setChecked(old_run)
                app.ui.checkBox_view_shots.setChecked(old_view)
            return dict(accepted=True, submitted_to_blacs=False)
        raise ControlError('Unsupported Runmanager action: ' + action)

    def command(self, action, params):
        from qtutils import inmain
        if self.closing and action != 'status':
            raise ControlError('Runmanager is shutting down')
        if action == 'globals.get':
            return self.remote.handle_get_globals(raw=True)
        if action == 'globals.set':
            if self.closing:
                raise ControlError('Runmanager is shutting down')
            self.remote.handle_set_globals(params['values'], raw=True)
            return {'updated': sorted(params['values'])}
        if action == 'script.select':
            path = params['path']
            if not Path(path).is_file() or Path(path).suffix.lower() != '.py':
                raise ControlError('Expected an existing Python shot file')
            self.remote.handle_set_labscript_file(path)
            return {'script': path}
        if action == 'compile.abort':
            self.remote.handle_abort()
            return {'accepted': True}
        if action == 'compile':
            # This waits outside Qt, as in the stock handle_engage implementation.
            self.app.wait_until_preparse_complete()
        return inmain(self.command_gui, action, params)


def attach(app, remote_server):
    controller = Controller(app, remote_server)
    server = ControlServer('runmanager', controller.command)
    app._labcontrol_controller = controller
    app._labcontrol_server = server
    from qtutils.qt.QtWidgets import QApplication
    QApplication.instance().aboutToQuit.connect(server.close)
    return server
