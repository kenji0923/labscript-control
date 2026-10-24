"""Loopback-only JSON transport with durable mutation receipts."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, build_opener, ProxyHandler
from .identity import machine_info, same_host
from .lease import LeaseGate

PORTS = {'blacs': 47140, 'runmanager': 47141}
READ_ACTIONS = {'status', 'queue.list', 'operation.get', 'globals.get', 'lease.status'}
MAX_BODY = 1024 * 1024


class ControlError(RuntimeError):
    pass


def state_dir():
    base = os.environ.get('LABCONTROL_STATE_DIR')
    if base:
        return Path(base)
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'LabscriptAgentControl'


def provision():
    directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = directory / 'token'
    try:
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_hex(32))
    return directory


def token_value():
    return (state_dir() / 'token').read_text().strip()


def make_request(action, params=None, request_id=None, lease_id=None, expected_hostname=None):
    return {'action': action, 'params': params or {},
            'request_id': request_id or str(uuid.uuid4()),
            'lease_id': lease_id, 'expected_hostname': expected_hostname}


def check_identity(request):
    expected = request.get('expected_hostname')
    actual = machine_info()['hostname']
    if expected is not None and not same_host(expected, actual):
        raise ControlError(f'Wrong machine: expected {expected}, reached {actual}; request refused')


class Dispatcher:
    def __init__(self, app_name, callback, directory=None):
        self.callback = callback
        self.lock = threading.Lock()
        directory = directory or state_dir()
        self.gate = LeaseGate(directory)
        self.database = directory / (app_name + '-operations.sqlite3')
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, digest TEXT, response TEXT, created REAL)')

    def execute(self, request):
        check_identity(request)
        response = self._execute(request)
        return {**response, 'server': machine_info()}

    def _execute(self, request):
        action = request['action']
        params = request.get('params', {})
        if not isinstance(action, str) or not isinstance(params, dict):
            raise ControlError('Invalid action or parameters')
        if action == 'operation.get':
            with sqlite3.connect(self.database) as db:
                row = db.execute('SELECT response FROM operations WHERE id=?', (params['request_id'],)).fetchone()
            return json.loads(row[0]) if row else {'ok': False, 'error': 'Unknown operation'}
        if action in READ_ACTIONS:
            result = self.gate.status() if action == 'lease.status' else self.callback(action, params)
            if action == 'status':
                result = {**result, 'hostname': machine_info()['hostname'], 'lease': self.gate.status()}
            return {'ok': True, 'result': result}
        request_id = request.get('request_id')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ControlError('Mutations require a request_id of 1..128 characters')
        # Include the lease credential in the request fingerprint: a receipt
        # must not turn a request made with another credential into a retry.
        digest = hashlib.sha256(json.dumps([action, params, request.get('lease_id')], sort_keys=True).encode()).hexdigest()
        with self.lock:
            with self.gate.transaction() as lease_db:
                with sqlite3.connect(self.database) as db:
                    row = db.execute('SELECT digest, response FROM operations WHERE id=?', (request_id,)).fetchone()
                    if row:
                        legacy = hashlib.sha256(json.dumps([action, params], sort_keys=True).encode()).hexdigest()
                        if row[0] != digest and not (request.get('lease_id') is None and row[0] == legacy):
                            raise ControlError('request_id already used for different parameters')
                        return json.loads(row[1])
                    response = {'ok': False, 'request_id': request_id, 'state': 'outcome_unknown',
                                'error': 'Operation started; do not retry with a new request_id'}
                    db.execute('INSERT INTO operations VALUES (?,?,?,?)',
                               (request_id, digest, json.dumps(response), time.time()))
                try:
                    if action.startswith('lease.'):
                        result = self.gate.control(lease_db, action, params)
                    else:
                        self.gate.check(lease_db, request.get('lease_id'))
                        result = self.callback(action, params)
                    response = {'ok': True, 'request_id': request_id, 'result': result}
                except Exception as exc:
                    response = {'ok': False, 'request_id': request_id, 'error': str(exc),
                                'error_type': type(exc).__name__}
            # Commit a lease change before claiming success in its receipt.
            # A crash between the two commits leaves an honest unknown outcome.
            with sqlite3.connect(self.database) as db:
                db.execute('UPDATE operations SET response=? WHERE id=?', (json.dumps(response), request_id))
            return response


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app_name, callback, port=None):
        self.secret = token_value()
        self.dispatcher = Dispatcher(app_name, callback)
        super().__init__(('127.0.0.1', PORTS[app_name] if port is None else port), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True, name=app_name + '-control')
        self.thread.start()

    def close(self):
        # Do not join handlers waiting on the Qt main thread during shutdown.
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.connection.settimeout(10)
        if self.path != '/v1/command':
            self.send_error(404)
            return
        auth = self.headers.get('Authorization', '')
        if not hmac.compare_digest(auth, 'Bearer ' + self.server.secret):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                raise ControlError('Invalid request length')
            request = json.loads(self.rfile.read(length))
            response = self.server.dispatcher.execute(request)
        except Exception as exc:
            response = {'ok': False, 'error': str(exc), 'error_type': type(exc).__name__,
                        'server': machine_info()}
        payload = json.dumps(response, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Receipt remains queryable; never execute again on transport failure.


def local_call(app, request, timeout=15):
    request = Request(f'http://127.0.0.1:{PORTS[app]}/v1/command',
                      data=json.dumps(request).encode(), method='POST',
                      headers={'Authorization': 'Bearer ' + token_value(), 'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)
