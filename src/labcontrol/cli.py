#!/usr/bin/env python3
"""Agent-accessible labscript control. All output is structured JSON."""
import argparse
import json
import sys
import uuid
from labcontrol.client import execute_local
from labcontrol.config import add_connection_arguments, client_from_arguments
from labcontrol.protocol import ControlError


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(p)
    p.add_argument('--request-id')
    p.add_argument('--lease-id', help='Credential returned by lease acquire')
    p.add_argument('--wait', action='store_true', help='Wait for restart initialization to finish')
    p.add_argument('--timeout', type=float, default=120)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('rpc', help=argparse.SUPPRESS)
    status = sub.add_parser('status')
    status.add_argument('app', choices=['all','blacs','runmanager','host'], nargs='?', default='all')
    lease = sub.add_parser('lease', help='Reserve this PC across CLI/MCP commands and app restarts')
    lease.add_argument('action', choices=['status','acquire','renew','release'])
    lease.add_argument('--owner', help='Human-readable agent/task name')
    lease.add_argument('--ttl', type=int, default=300, help='Reservation duration, 30..3600 seconds')
    queue = sub.add_parser('queue')
    queue.add_argument('action', choices=['list','pause','resume','clear','add','remove','move','repeat','abort'])
    queue.add_argument('--path')
    queue.add_argument('--index', type=int)
    queue.add_argument('--destination', type=int)
    queue.add_argument('--revision')
    queue.add_argument('--enabled', choices=['true','false'])
    tab = sub.add_parser('tab')
    tab.add_argument('action', choices=['restart'])
    tab.add_argument('name')
    app = sub.add_parser('app')
    app.add_argument('action', choices=['start','stop','restart'])
    app.add_argument('app', choices=['blacs','runmanager'])
    app.add_argument('--broken-tabs', choices=['refuse','preserve'], default='refuse')
    rm = sub.add_parser('runmanager')
    rm.add_argument('action', choices=['globals','set-globals','select','compile','abort'])
    rm.add_argument('--path')
    rm.add_argument('--values', help='JSON mapping of global names to Python expression strings')
    op = sub.add_parser('operation')
    op.add_argument('app', choices=['blacs','runmanager','host'])
    op.add_argument('id')
    return p


def main():
    args = parser().parse_args()
    if args.command == 'rpc':
        if not args.local:
            raise ControlError('rpc is a local SSH endpoint')
        request = json.load(sys.stdin)
        app = request.pop('app')
        if app not in ('blacs','runmanager','host'):
            raise ControlError('Unknown application')
        print(json.dumps(execute_local(app, request), ensure_ascii=False))
        return
    client = client_from_arguments(args)
    params = {}
    if args.command == 'status':
        apps = ['blacs','runmanager'] if args.app == 'all' else [args.app]
        result = {}
        for app in apps:
            try:
                result[app] = client.call(app, 'status')['result']
            except ControlError as exc:
                result[app] = {'available': False, 'error': str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if any(v.get('available') is False for v in result.values()):
            sys.exit(1)
        return
    if args.command == 'lease':
        app, action = 'host', 'lease.' + args.action
        if args.action != 'status':
            token = args.lease_id
            if args.action == 'acquire':
                token = token or str(uuid.uuid4())
                if not args.owner:
                    raise ControlError('lease acquire requires --owner')
            elif not token:
                raise ControlError('Pass --lease-id before the lease subcommand')
            params = {'lease_id':token, 'owner':args.owner, 'ttl':args.ttl}
    elif args.command == 'queue':
        app, action = 'blacs', 'queue.' + args.action
        params = {key: getattr(args, key) for key in ('path','index','destination','revision') if getattr(args,key) is not None}
        if args.enabled is not None: params['enabled'] = args.enabled == 'true'
    elif args.command == 'tab':
        app, action, params = 'blacs', 'tab.restart', {'name': args.name}
    elif args.command == 'app':
        app, action = args.app, 'app.' + args.action
        params = {'broken_tabs': args.broken_tabs}
    elif args.command == 'operation':
        app, action, params = args.app, 'operation.get', {'request_id': args.id}
    else:
        app = 'runmanager'
        action = {'globals':'globals.get','set-globals':'globals.set','select':'script.select',
                  'compile':'compile','abort':'compile.abort'}[args.action]
        if args.path is not None: params['path'] = args.path
        if args.values is not None: params['values'] = json.loads(args.values)
    response = client.call(app, action, params, args.request_id)
    if args.wait and action in ('app.start','app.restart','tab.restart'):
        result = response['result']
        response['ready_status'] = client.wait_ready(app,
            previous_pid=result.get('pid') if action == 'app.restart' else None,
            tab=args.name if action == 'tab.restart' else None,
            previous_generation=result.get('previous'), timeout=args.timeout)
    print(json.dumps(response, ensure_ascii=False, indent=2))


def entrypoint():
    try:
        main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
