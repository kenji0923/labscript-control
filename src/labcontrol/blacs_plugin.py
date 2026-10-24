"""BLACS plugin: commands execute on its Qt main thread, never a worker socket."""
import hashlib
import json
import os
import time
from pathlib import Path

from .protocol import ControlError, ControlServer


def queue_revision(paths):
    return hashlib.sha256(json.dumps(paths).encode()).hexdigest()


def edit_queue(paths, action, params):
    """Validate the complete edit before touching the live Qt model."""
    if params.get('revision') != queue_revision(paths):
        raise ControlError('Queue changed; read queue.list and use its revision')
    paths = list(paths)
    if action == 'queue.clear':
        return []
    index = params.get('index')
    if type(index) is not int or not 0 <= index < len(paths):
        raise ControlError('Invalid queue index')
    if action == 'queue.remove':
        paths.pop(index)
    elif action == 'queue.move':
        destination = params.get('destination')
        if type(destination) is not int or not 0 <= destination < len(paths):
            raise ControlError('Invalid destination index')
        paths.insert(destination, paths.pop(index))
    else:
        raise ControlError('Unknown queue edit')
    return paths


class Controller:
    def __init__(self, app):
        self.app = app
        self.queue = app.queue
        self.generation = {}
        self.reserved = None
        self.closing = False
        from qtutils import inmain_decorator
        original_get = self.queue.get_next_file
        original_status = self.queue.set_status

        @inmain_decorator(True)
        def get_next_file():
            # The stock loop checks pause before hopping into Qt. Recheck here
            # to prevent a queued dequeue racing a pause/restart request.
            if self.queue.manager_paused or self.closing:
                raise IndexError('Queue paused')
            path = original_get()
            self.reserved = path
            return path

        @inmain_decorator(True)
        def set_status(status, shot_filepath=None):
            original_status(status, shot_filepath)
            if status in ('Idle', 'Queue paused'):
                self.reserved = None

        self.queue.get_next_file = get_next_file
        self.queue.set_status = set_status

    def tabs(self):
        result = {}
        for name, tab in self.app.tablist.items():
            workers = {}
            for worker_name, item in getattr(tab, 'workers', {}).items():
                process = item[0]
                # zprocess.Process wraps a subprocess.Popen in .child.
                child = getattr(process, 'child', process)
                workers[worker_name] = getattr(child, 'pid', None)
            error = str(getattr(tab, 'error_message', '') or '')
            state = str(getattr(tab, 'state', ''))
            mode = getattr(tab, 'mode', None)
            result[name] = dict(mode=mode, state=state, error=error,
                                generation=self.generation.get(name, 0), workers=workers,
                                ready=mode == 1 and not error and 'init' not in state.lower()
                                and 'starting' not in state.lower()
                                and state != 'fatal error' and bool(workers)
                                and all(pid is not None for pid in workers.values()))
        return result

    def queue_state(self):
        paths = [self.queue._model.item(i).text() for i in range(self.queue._model.rowCount())]
        return dict(paths=paths, revision=queue_revision(paths),
                    paused=self.queue.manager_paused, repeat=self.queue.manager_repeat,
                    repeat_mode=self.queue.manager_repeat_mode,
                    status=self.queue.get_status(), current_shot=self.reserved)

    def require_idle(self):
        status = self.queue.get_status()
        if self.reserved or status not in ('Idle', 'Queue paused'):
            raise ControlError(f'BLACS is not idle: {status}; pause and wait for the current shot')
        if any(getattr(tab, 'mode', 1) != 1 for tab in self.app.tablist.values()):
            raise ControlError('Some devices are still buffered or transitioning')

    def command(self, action, params):
        if action == 'status':
            tabs = self.tabs()
            return dict(app='blacs', pid=os.getpid(), time=time.time(), closing=self.closing,
                        queue=self.queue_state(), tabs=tabs, ready=bool(tabs) and all(t['ready'] for t in tabs.values()))
        if action == 'queue.list':
            return self.queue_state()
        if self.closing:
            raise ControlError('BLACS is shutting down')
        if action == 'queue.pause':
            self.queue.manager_paused = True
        elif action == 'queue.resume':
            if any(not tab['ready'] for tab in self.tabs().values()):
                raise ControlError('One or more tabs are not ready')
            self.queue.manager_paused = False
        elif action == 'queue.repeat':
            if type(params.get('enabled')) is not bool:
                raise ControlError('enabled must be Boolean')
            self.queue.manager_repeat = params['enabled']
        elif action in ('queue.clear', 'queue.remove', 'queue.move'):
            if not self.queue.manager_paused:
                raise ControlError('Pause the queue before editing it')
            state = self.queue_state()
            paths = edit_queue(state['paths'], action, params)
            from qtutils.qt.QtGui import QStandardItem
            self.queue._toggle_clear()
            for path in paths:
                self.queue._model.appendRow(QStandardItem(path))
        elif action == 'queue.add':
            # Enqueue is separate from resume. It must not start hardware as a
            # side effect of adding a file to an otherwise unpaused queue.
            if not self.queue.manager_paused:
                raise ControlError('Pause the queue before adding shots; resume explicitly')
            path = os.path.normpath(params['path'])
            if Path(path).suffix.lower() != '.h5' or not Path(path).is_file():
                raise ControlError('Expected an existing .h5 shot file on Windows')
            if self.queue.is_in_queue(path) or path == self.reserved:
                raise ControlError('Shot is already queued or running')
            import h5py
            with h5py.File(path, 'r') as f:
                if 'data' in f or 'run time' in f.attrs:
                    raise ControlError('Shot already ran; compile a fresh shot rather than silently rerunning')
            reply = self.queue.process_request(path)
            if 'added successfully' not in reply:
                raise ControlError(reply.strip())
        elif action == 'queue.abort':
            if not self.app.ui.queue_abort_button.isEnabled():
                raise ControlError('There is no abortable shot')
            self.queue.manager_paused = True
            self.app.ui.queue_abort_button.click()
        elif action == 'tab.restart':
            self.require_idle()
            name = params['name']
            if name not in self.app.tablist:
                raise ControlError('Unknown tab: ' + name)
            self.queue.manager_paused = True
            before = self.tabs()[name]
            self.generation[name] = before['generation'] + 1
            self.app.tablist[name].restart()
            return dict(accepted=True, name=name, generation=self.generation[name], previous=before)
        elif action in ('app.restart', 'app.stop'):
            self.require_idle()
            self.queue.manager_paused = True
            broken = self.app.failed_device_settings
            if broken:
                if params.get('broken_tabs', 'refuse') != 'preserve':
                    raise ControlError('Broken tabs exist; use broken_tabs=preserve to retain their saved settings')
                # Preserve failed-device settings without answering a modal dialog.
                original_save = self.app.front_panel_settings.get_save_data
                def save_with_failed():
                    data = original_save()
                    data[0].update(broken)
                    return data
                self.app.front_panel_settings.get_save_data = save_with_failed
                self.app.failed_device_settings = {}
            self.closing = True
            self.app.set_relaunch(action == 'app.restart')
            from qtutils.qt.QtCore import QTimer
            QTimer.singleShot(300, self.app.ui.close)
            return dict(accepted=True, pid=os.getpid(), queue_paused=True)
        else:
            raise ControlError('Unsupported BLACS action: ' + action)
        return self.queue_state()


class Plugin:
    def __init__(self, initial_settings):
        self.server = None

    def get_menu_class(self): return None
    def get_notification_classes(self): return []
    def get_setting_classes(self): return []
    def get_callbacks(self): return {}
    def set_menu_instance(self, menu): pass
    def set_notification_instances(self, notifications): pass
    def get_save_data(self): return {}

    def plugin_setup_complete(self, blacs):
        from qtutils import inmain
        self.controller = Controller(blacs['ui'].blacs)
        self.server = ControlServer('blacs', lambda action, params: inmain(self.controller.command, action, params))

    def close(self):
        if self.server is not None:
            self.server.close()
