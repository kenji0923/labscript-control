import base64
import json
from types import SimpleNamespace as NS

import pytest
from labcontrol import config, client
from labcontrol.identity import machine_info
from labcontrol.protocol import ControlError, Dispatcher, make_request


@pytest.fixture
def profiles(tmp_path):
    path = tmp_path / 'targets.json'
    path.write_text(json.dumps({'version':1, 'default_target':'micro0', 'targets':{
        name: {'hostname':'TestHost' + name.title(), 'ssh_host':name,
               'python':"C:/DAQ user's env/$data;literal/python.exe"}
        for name in ('micro0','micro1')}}))
    return path


def test_native_windows_defaults_local_and_named_self_uses_local(monkeypatch, profiles):
    monkeypatch.setattr(config, 'os', NS(name='nt', environ={}))
    monkeypatch.setattr(config, 'machine_info', lambda:{'hostname':'TESTHOSTMICRO1'})
    assert config.resolve_target(config_path=profiles).transport == 'local'
    assert config.resolve_target('micro1', profiles).transport == 'local'
    assert config.resolve_target('micro0', profiles).transport == 'ssh'
    with pytest.raises(ControlError, match='does not match'):
        config.resolve_target('micro0', profiles, 'local')


def test_other_pc_profiles_environment_override_and_legacy_host(monkeypatch, profiles):
    monkeypatch.setattr(config, 'os', NS(name='posix', environ={'LABCONTROL_CONFIG':str(profiles)}))
    monkeypatch.setattr(config, 'machine_info', lambda:{'hostname':'LinuxControl'})
    assert config.resolve_target().name == 'micro0'
    assert client.Client('micro1').target.ssh_host == 'micro1'
    with pytest.raises(ControlError, match='configuration'):
        config.resolve_target('unknown')


def test_ssh_paths_are_literal_and_json_uses_stdin(monkeypatch, profiles):
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return NS(returncode=0, stderr='', stdout=json.dumps({'ok':True,'result':{},
                  'server':{'hostname':'TestHostMicro1','protocol_version':2}}))
    monkeypatch.setattr(client.subprocess, 'run', run)
    c = client.Client('micro1', config_path=profiles, transport='ssh', ssh_config='ssh-test')
    response = c.call('blacs', 'queue.add', {'path':"Z:/a'; evil.h5"}, 'req-1', lease_id='test-lease-000001')
    argv, kwargs = seen[0]
    assert argv[-2] == 'micro1'
    script = base64.b64decode(argv[-1].split()[-1]).decode('utf-16le')
    assert "'C:/DAQ user''s env/$data;literal/python.exe' -m labcontrol --local rpc" in script
    assert "evil.h5" not in script
    request = json.loads(kwargs['input'])
    assert request['expected_hostname'] == 'TestHostMicro1'
    assert request['lease_id'] == 'test-lease-000001'
    assert response['transport'] == 'ssh'
    assert response['target'] == 'micro1'


def test_local_transport_never_spawns_ssh(monkeypatch):
    monkeypatch.setattr(client, 'execute_local', lambda app,req:{'ok':True,'result':{},'server':machine_info()})
    monkeypatch.setattr(client.subprocess, 'run', lambda *a,**kw:pytest.fail('must not use SSH'))
    assert client.Client('local').call('host','status')['transport'] == 'local'


def test_wrong_machine_is_refused_before_callback(tmp_path):
    dispatcher = Dispatcher('blacs', lambda *args:pytest.fail('must not mutate wrong host'), tmp_path)
    with pytest.raises(ControlError, match='Wrong machine'):
        dispatcher.execute(make_request('queue.resume', expected_hostname='wrong-machine'))


def test_stale_v1_bridge_cannot_bypass_leases(monkeypatch):
    seen = []
    monkeypatch.setattr(client, 'local_call', lambda app,request:seen.append(request['action']) or {'ok':True,'result':{}})
    result = client.execute_local('blacs', make_request('queue.resume'))
    assert not result['ok'] and 'protocol v2' in result['error']
    assert seen == ['status']
