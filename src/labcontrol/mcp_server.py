"""Official MCP SDK stdio server over the same client used by labctl."""
import argparse
from typing import Literal
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .client import Client
from .config import add_connection_arguments, client_from_arguments


def build_server(client):
    target_name = getattr(getattr(client, 'target', None), 'name', 'local')
    server = FastMCP('labscript-' + target_name, instructions=(
        f'Control BLACS and Runmanager on target {target_name}, via local API or SSH. '
        'Check the actual hostname in responses. Adding a shot requires a paused queue. '
        'Queue resume may fire hardware. Restart leaves BLACS paused. '
        'Use explicit request_id values for mutations and query receipts after an uncertain response. '
        'Acquire a PC-wide lease for multi-step changes, pass lease_id with mutations, renew before expiry, '
        'and release when finished. Reads remain available to other agents. GUI/native APIs do not honor leases.'))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    mutate = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def labscript_status(app: Literal['blacs', 'runmanager', 'host']) -> dict:
        """Read application PID, readiness, and queue/tabs or compilation state."""
        return client.call(app, 'status')

    @server.tool(annotations=read)
    def blacs_queue_list() -> dict:
        """Read pending shot paths and the revision required for destructive queue edits."""
        return client.call('blacs', 'queue.list')

    @server.tool(annotations=mutate)
    def blacs_queue_control(action: Literal['pause','resume','abort'], request_id: str, lease_id: str | None = None) -> dict:
        """Pause future shots, resume hardware execution, or abort the current shot and pause."""
        return client.call('blacs', 'queue.' + action, request_id=request_id, lease_id=lease_id)

    @server.tool(annotations=mutate)
    def blacs_queue_add(path: str, request_id: str, lease_id: str | None = None) -> dict:
        """Add a fresh compiled Windows .h5 path to the paused queue; does not resume it."""
        return client.call('blacs', 'queue.add', {'path':path}, request_id, lease_id=lease_id)

    @server.tool(annotations=mutate)
    def blacs_queue_edit(action: Literal['clear','remove','move'], revision: str,
                         request_id: str, index: int | None = None,
                         destination: int | None = None, lease_id: str | None = None) -> dict:
        """Edit pending shots while paused; revision must match the last queue listing."""
        return client.call('blacs', 'queue.' + action,
                           {'revision':revision,'index':index,'destination':destination}, request_id, lease_id=lease_id)

    @server.tool(annotations=mutate)
    def blacs_queue_repeat(enabled: bool, request_id: str, lease_id: str | None = None) -> dict:
        """Enable or disable queue repetition; enabling can cause repeated hardware shots."""
        return client.call('blacs','queue.repeat',{'enabled':enabled},request_id,lease_id=lease_id)

    @server.tool(annotations=mutate)
    def blacs_restart_tab(name: str, request_id: str, wait: bool = True, lease_id: str | None = None) -> dict:
        """Restart one idle device tab, leave queue paused, optionally verify new workers are ready."""
        response = client.call('blacs','tab.restart',{'name':name},request_id,lease_id=lease_id)
        if wait:
            response['ready_status'] = client.wait_ready('blacs',tab=name,
                previous_generation=response['result']['previous'])
        return response

    @server.tool(annotations=mutate)
    def labscript_application(app: Literal['blacs','runmanager'],
                              action: Literal['start','restart','stop'], request_id: str,
                              preserve_broken_tabs: bool = False, wait: bool = True,
                              lease_id: str | None = None) -> dict:
        """Start in the logged-on Windows session, or gracefully stop/restart an idle app."""
        response = client.call(app,'app.'+action,
            {'broken_tabs':'preserve' if preserve_broken_tabs else 'refuse'},request_id,lease_id=lease_id)
        if wait and action in ('start','restart'):
            response['ready_status'] = client.wait_ready(app,
                previous_pid=response['result'].get('pid') if action=='restart' else None)
        return response

    @server.tool(annotations=read)
    def labscript_operation(app: Literal['blacs','runmanager','host'], request_id: str) -> dict:
        """Read a durable mutation receipt; do not resend an uncertain action with a new ID."""
        return client.call(app,'operation.get',{'request_id':request_id})

    @server.tool(annotations=read)
    def runmanager_globals() -> dict:
        """Read active globals as expression strings."""
        return client.call('runmanager','globals.get')

    @server.tool(annotations=mutate)
    def runmanager_set_globals(values: dict[str,str], request_id: str, lease_id: str | None = None) -> dict:
        """Update existing active globals with expression strings using Runmanager's native API."""
        return client.call('runmanager','globals.set',{'values':values},request_id,lease_id=lease_id)

    @server.tool(annotations=mutate)
    def runmanager_select_script(path: str, request_id: str, lease_id: str | None = None) -> dict:
        """Select an existing Windows Python shot file without compiling or executing it."""
        return client.call('runmanager','script.select',{'path':path},request_id,lease_id=lease_id)

    @server.tool(annotations=mutate)
    def runmanager_compile(request_id: str, lease_id: str | None = None) -> dict:
        """Compile current settings without submitting shots to BLACS or Runviewer."""
        return client.call('runmanager','compile',request_id=request_id,lease_id=lease_id)

    @server.tool(annotations=mutate)
    def runmanager_abort_compile(request_id: str, lease_id: str | None = None) -> dict:
        """Abort compilation; this does not abort an already-running BLACS shot."""
        return client.call('runmanager','compile.abort',request_id=request_id,lease_id=lease_id)

    @server.tool(annotations=read)
    def labscript_lease_status() -> dict:
        """Read this PC's reservation owner and expiry, including while apps are stopped."""
        return client.call('host', 'lease.status')

    @server.tool(annotations=mutate)
    def labscript_lease(action: Literal['acquire','renew','release'], lease_id: str,
                        request_id: str, owner: str | None = None, ttl: int = 300) -> dict:
        """Reserve both apps on this PC. Choose a UUID lease_id; TTL is 30..3600 seconds.

        Use a fresh request_id for each renewal; retrying an old request returns
        its receipt without extending the lease. Reads never require a lease.
        """
        return client.call('host', 'lease.' + action,
                           {'lease_id':lease_id, 'owner':owner, 'ttl':ttl}, request_id)

    return server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    args=parser.parse_args()
    build_server(client_from_arguments(args)).run(transport='stdio')


if __name__ == '__main__': main()
