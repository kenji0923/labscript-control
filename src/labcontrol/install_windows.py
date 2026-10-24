"""Install application bridges and interactive startup tasks on Windows."""
import argparse
import configparser
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .identity import machine_info
from .protocol import provision

BEGIN = '# BEGIN labscript agent control hook'
END = '# END labscript agent control hook'


def render_runmanager_hook(source):
    hook = (f'{BEGIN}\n'
            '    from labcontrol.runmanager_hook import attach as _attach_agent_control\n'
            '    _agent_control = _attach_agent_control(app, remote_server)\n'
            f'    {END}')
    if BEGIN in source or END in source:
        if source.count(BEGIN) != 1 or source.count(END) != 1:
            raise RuntimeError('Ambiguous or incomplete Runmanager control hook')
        start, end = source.index(BEGIN), source.index(END)
        if end < start:
            raise RuntimeError('Malformed Runmanager control hook')
        source = source[:start] + hook + source[end + len(END):]
    else:
        anchor = '    remote_server = RemoteServer()'
        if source.count(anchor) != 1:
            raise RuntimeError('Unsupported Runmanager startup layout; inspect before patching')
        source = source.replace(anchor, anchor + '\n    ' + hook)
    compile(source, 'runmanager/__main__.py', 'exec')
    return source


def enable_plugin(source):
    config = configparser.ConfigParser()
    config.read_string(source)
    section = 'BLACS/plugins'
    if not config.has_section(section):
        return source + '\n[BLACS/plugins]\nagent_control = True\n'
    # Edit only this section, preserving unrelated options and formatting.
    lines = source.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == '[BLACS/plugins]')
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
    matches = [i for i in range(start + 1, end) if re.match(r'^\s*agent_control\s*=', lines[i], re.I)]
    if matches:
        lines[matches[0]] = 'agent_control = True\n'
    else:
        if not lines[start].endswith('\n'):
            lines[start] += '\n'
        lines.insert(start + 1, 'agent_control = True\n')
    return ''.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labscript-root', required=True, type=Path,
                        help='Existing labscript profile/repository containing labconfig')
    parser.add_argument('--labconfig', type=Path, help='Override the machine-specific labconfig INI')
    parser.add_argument('--mcp-python', type=Path,
                        help='Optional agent Python environment with labscript-control[mcp] installed')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('Run this installer with the Windows labscript Python environment')
    root = args.labscript_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    ini = args.labconfig or root / 'labconfig' / (os.environ['COMPUTERNAME'] + '.ini')
    packages = {name: importlib.util.find_spec(name) for name in ('blacs', 'runmanager')}
    if any(spec is None or spec.origin is None for spec in packages.values()):
        raise RuntimeError('BLACS and Runmanager must be installed in this Python environment')
    plugin = Path(packages['blacs'].origin).parent / 'plugins/agent_control/__init__.py'
    runmanager = Path(packages['runmanager'].origin).parent / '__main__.py'
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise FileNotFoundError(pythonw)
    mcp_python = (args.mcp_python or Path(sys.executable)).resolve()
    if not mcp_python.is_file():
        raise FileNotFoundError(mcp_python)

    # Validate every source edit before changing files or registering tasks.
    changes = [
        (plugin, b'from labcontrol.blacs_plugin import Plugin\n', 'plugin.py'),
        (runmanager, render_runmanager_hook(runmanager.read_text(encoding='utf-8')).encode(), 'runmanager_main.py'),
        (ini, enable_plugin(ini.read_text(encoding='utf-8')).encode(), 'labconfig.ini'),
    ]
    stage = provision()
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
    backup = stage / 'backups' / stamp
    backup.mkdir(parents=True)
    account = subprocess.check_output(['whoami'], text=True).strip()
    subprocess.run(['icacls', str(stage), '/inheritance:r', '/grant:r',
                    account + ':(OI)(CI)F', 'SYSTEM:(OI)(CI)F'], check=True, capture_output=True)
    manifest = []
    for path, data, label in changes:
        previous = path.read_bytes() if path.exists() else None
        if previous == data:
            continue
        if previous is not None:
            (backup / label).write_bytes(previous)
        manifest.append({'path': str(path), 'backup': label if previous is not None else None,
                         'before': hashlib.sha256(previous).hexdigest() if previous is not None else None,
                         'after': hashlib.sha256(data).hexdigest()})
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.agent-stage')
        temporary.write_bytes(data)
        temporary.replace(path)
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(Path(__file__).with_name('register_tasks.ps1')),
                    '-Python', str(pythonw), '-Root', str(root)], check=True)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))

    hostname = machine_info()['hostname']
    name = 'labscript-' + hostname.lower()
    arguments = ['-m', 'labcontrol.mcp_server', '--local']
    command = str(mcp_python)
    (stage / 'mcp-client.json').write_text(json.dumps(
        {'mcpServers': {name: {'command': command, 'args': arguments}}}, indent=2))
    (stage / 'mcp-codex.toml').write_text(
        f'[mcp_servers.{json.dumps(name)}]\ncommand = {json.dumps(command)}\n'
        f'args = {json.dumps(arguments)}\ntool_timeout_sec = 180\n')
    print(json.dumps({'installed': True, 'hostname': hostname, 'backup': str(backup),
                      'restart_required': ['blacs', 'runmanager'], 'state_dir': str(stage),
                      'mcp_config_examples': ['mcp-client.json', 'mcp-codex.toml']}, indent=2))


if __name__ == '__main__':
    main()
