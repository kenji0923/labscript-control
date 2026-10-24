"""Shared CLI/MCP client. SSH carries JSON on stdin, never shell-interpolated input."""
import json
from pathlib import Path
import subprocess
import time
import base64

from .protocol import ControlError, Dispatcher, check_identity, local_call, make_request, READ_ACTIONS, provision
from .identity import machine_info, same_host, PROTOCOL_VERSION
from .config import resolve_target


def ssh_command(target):
    # OpenSSH on Windows may use cmd.exe or PowerShell as its default shell.
    # Use a fixed command and UTF-16 encoded PowerShell source; configured paths
    # are literal strings, never interpolated into the remote shell's syntax.
    literal = lambda value: "'" + value.replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; & " + literal(target.python) +
              ' -m labcontrol --local rpc; exit $LASTEXITCODE')
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return 'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded


class Client:
    def __init__(self, host=None, ssh_config=None, timeout=30, *, config_path=None,
                 transport='auto', lease_id=None):
        self.target = resolve_target(host, config_path, transport)
        self.host = self.target.name
        default_config = Path.home() / '.ssh/config'
        self.ssh_config = ssh_config or (str(default_config) if default_config.is_file() else None)
        self.timeout = timeout
        self.lease_id = lease_id

    def call(self, app, action, params=None, request_id=None, lease_id=None):
        if app not in ('blacs', 'runmanager', 'host'):
            raise ControlError('Unknown application')
        request = make_request(action, params, request_id, lease_id or self.lease_id, self.target.hostname)
        try:
            if self.target.transport == 'local':
                response = execute_local(app, request)
            else:
                argv = ['ssh']
                if self.ssh_config:
                    argv += ['-F', self.ssh_config]
                result = subprocess.run(argv + ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                               self.target.ssh_host, ssh_command(self.target)],
                                        input=json.dumps({'app': app, **request}),
                                        text=True, capture_output=True, timeout=self.timeout)
                if result.returncode:
                    raise ControlError(result.stderr.strip() or result.stdout.strip())
                response = json.loads(result.stdout)
            server = response.get('server', {})
            if not same_host(server.get('hostname'), self.target.hostname):
                raise ControlError('Response identity does not match the selected target; upgrade the remote bridge if absent')
        except (OSError, ValueError, subprocess.TimeoutExpired, ControlError) as exc:
            raise ControlError(f'{exc}; request_id={request["request_id"]}. '
                               'For a mutation, check its receipt before retrying.') from exc
        if not response.get('ok'):
            raise ControlError(json.dumps(response, ensure_ascii=False))
        response['target'] = self.target.name
        response['transport'] = self.target.transport
        return response

    def wait_ready(self, app, previous_pid=None, tab=None, previous_generation=None, timeout=120):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = self.call(app, 'status')['result']
                if tab:
                    value = last['tabs'].get(tab, {})
                    # Restarts return before worker initialization. Require a
                    # new generation AND a new set of worker process IDs.
                    previous = previous_generation or {}
                    if (value.get('ready') and value.get('generation', 0) > previous.get('generation', -1)
                            and value.get('workers') != previous.get('workers')):
                        return last
                elif (previous_pid is None or last.get('pid') != previous_pid) and last.get('ready'):
                    return last
            except ControlError as exc:
                last = str(exc)
            time.sleep(1)
        raise ControlError(f'Readiness timeout; last status: {json.dumps(last, ensure_ascii=False)}')


def execute_local(app, request):
    try:
        check_identity(request)
        if app not in ('blacs', 'runmanager', 'host'):
            raise ControlError('Unknown application')
        action = request['action']
        if app == 'host' or action in ('app.start', 'operation.get'):
            from .windows import start_app
            def host_command(action, params):
                if action == 'app.start' and app != 'host':
                    return start_app(app)
                if action == 'status' and app == 'host':
                    return machine_info()
                raise ControlError('Unsupported host action')
            return Dispatcher(app, host_command, provision()).execute(request)
        if action not in READ_ACTIONS:
            # A pre-v2 GUI ignores leases. Refuse writes until it is restarted
            # with the new plugin; never give a false concurrency guarantee.
            status = local_call(app, make_request('status'))
            if status.get('server', {}).get('protocol_version', 0) < PROTOCOL_VERSION:
                raise ControlError('Application bridge must be restarted to load protocol v2 before mutations')
        return local_call(app, request)
    except Exception as exc:
        return {'ok': False, 'request_id': request.get('request_id'),
                'error': str(exc), 'error_type': type(exc).__name__, 'server': machine_info()}
