"""Exercise the actual MCP initialize/list/call protocol over stdio."""
import asyncio
import json
import os
from pathlib import Path
import sys
import textwrap
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_mcp(tmp_path):
    fixture=tmp_path/'server.py'
    fixture.write_text(textwrap.dedent('''
        from labcontrol.mcp_server import build_server
        class FakeClient:
            def call(self, app, action, params=None, request_id=None, lease_id=None):
                return {'ok':True,'result':{'app':app,'action':action,'params':params,'id':request_id,'lease_id':lease_id}}
        build_server(FakeClient()).run(transport='stdio')
    '''))
    async def check():
        params=StdioServerParameters(command=sys.executable,args=[str(fixture)],env=dict(os.environ))
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed=await session.list_tools()
                tools={t.name:t for t in listed.tools}
                assert len(tools)==16
                assert tools['labscript_status'].annotations.readOnlyHint
                assert not tools['blacs_queue_control'].annotations.readOnlyHint
                status=await session.call_tool('labscript_status',{'app':'blacs'})
                assert not status.isError
                assert json.loads(status.content[0].text)['result']['action']=='status'
                pause=await session.call_tool('blacs_queue_control',{'action':'pause','request_id':'test-pause','lease_id':'reservation-uuid-0001'})
                assert json.loads(pause.content[0].text)['result']['id']=='test-pause'
                assert json.loads(pause.content[0].text)['result']['lease_id']=='reservation-uuid-0001'
                lease=await session.call_tool('labscript_lease',{'action':'acquire','request_id':'reserve',
                    'lease_id':'reservation-uuid-0001','owner':'agent-a'})
                assert json.loads(lease.content[0].text)['result']['app']=='host'
                invalid=await session.call_tool('blacs_queue_control',{'action':'arbitrary-exec','request_id':'x'})
                assert invalid.isError
    asyncio.run(check())
