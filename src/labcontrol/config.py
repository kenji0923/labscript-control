"""Shared CLI/MCP target profiles; no machine paths embedded in the client."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from .identity import machine_info, same_host
from .protocol import ControlError


@dataclass(frozen=True)
class Target:
    name: str
    transport: str
    hostname: str
    ssh_host: str = ''
    python: str = ''


def default_config_path():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'LabscriptAgentControl/targets.json'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'labscript-control/targets.json'


def resolve_target(name=None, config_path=None, transport='auto'):
    if transport not in ('auto', 'local', 'ssh'):
        raise ControlError('Transport must be auto, local, or ssh')
    # A native Windows agent defaults to its own machine, regardless of the
    # Linux workstation's configured default. Explicit targets remain explicit.
    if name is None and os.name == 'nt':
        name = 'local'
    if name == 'local':
        if transport == 'ssh':
            raise ControlError('Choose a named target for SSH')
        return Target('local', 'local', machine_info()['hostname'])
    path = Path(config_path or os.environ.get('LABCONTROL_CONFIG') or default_config_path())
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
        if config['version'] != 1:
            raise ControlError('Unsupported target configuration version')
        name = name or config['default_target']
        profile = config['targets'][name]
        hostname = profile['hostname']
        if not isinstance(hostname, str) or not hostname:
            raise ControlError('Target hostname is required')
        chosen = transport
        if chosen == 'auto':
            chosen = 'local' if same_host(hostname, machine_info()['hostname']) else 'ssh'
        if chosen == 'local':
            if not same_host(hostname, machine_info()['hostname']):
                raise ControlError('Local machine does not match target ' + name)
            return Target(name, chosen, hostname)
        host = profile.get('ssh_host', name)
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+', host) or host.startswith('-'):
            raise ControlError('Invalid SSH host alias')
        if any(not isinstance(profile.get(key), str) or not profile[key] for key in ('python',)):
            raise ControlError('SSH targets require a nonempty Python path')
        return Target(name, chosen, hostname, host, profile['python'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ControlError(f'Invalid target configuration {path}: {exc}') from exc


def add_connection_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--target', '--host', dest='target', help='Target profile name (--host is an alias)')
    group.add_argument('--local', action='store_true', help='Operate on this machine without SSH')
    parser.add_argument('--config', help='Target JSON file (or LABCONTROL_CONFIG)')
    parser.add_argument('--transport', choices=['auto', 'local', 'ssh'], default='auto')
    parser.add_argument('--ssh-config', help='OpenSSH config; otherwise use ~/.ssh/config if present')


def client_from_arguments(args):
    from .client import Client
    return Client('local' if args.local else args.target, args.ssh_config,
                  config_path=args.config, transport=args.transport,
                  lease_id=getattr(args, 'lease_id', None))
