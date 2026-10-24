import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
from labcontrol.protocol import Dispatcher,ControlError,make_request,ControlServer,provision,token_value
from labcontrol.blacs_plugin import edit_queue,queue_revision


def test_durable_deduplication_and_payload_conflict(tmp_path):
    calls=[]
    callback=lambda a,p: calls.append(p) or {'accepted':True}
    dispatcher=Dispatcher('test',callback,tmp_path)
    req=make_request('queue.add',{'path':'a.h5'},'stable')
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies=list(pool.map(dispatcher.execute,[req]*8))
    assert len(calls)==1 and all(r==replies[0] for r in replies)
    # A newly started process must remember the receipt.
    assert Dispatcher('test',callback,tmp_path).execute(req)==replies[0]
    assert len(calls)==1
    with pytest.raises(ControlError,match='different parameters'):
        dispatcher.execute(make_request('queue.add',{'path':'b.h5'},'stable'))


def test_unknown_outcome_not_retried(tmp_path):
    def crash(a,p): raise KeyboardInterrupt()
    dispatcher=Dispatcher('test',crash,tmp_path)
    req=make_request('app.restart',request_id='once')
    with pytest.raises(KeyboardInterrupt): dispatcher.execute(req)
    restarted=Dispatcher('test',lambda *a: pytest.fail('must not repeat'),tmp_path)
    assert restarted.execute(req)['state']=='outcome_unknown'


def test_stale_queue_revision_and_invalid_move_are_nonmutating():
    paths=['a','b','c']
    with pytest.raises(ControlError,match='Queue changed'):
        edit_queue(paths,'queue.clear',{'revision':'stale'})
    with pytest.raises(ControlError,match='destination'):
        edit_queue(paths,'queue.move',{'revision':queue_revision(paths),'index':0,'destination':3})
    assert paths==['a','b','c']
    assert edit_queue(paths,'queue.move',{'revision':queue_revision(paths),'index':0,'destination':2})==['b','c','a']


def test_http_authentication(tmp_path,monkeypatch):
    monkeypatch.setenv('LABCONTROL_STATE_DIR',str(tmp_path))
    provision()
    calls=[]
    server=ControlServer('blacs',lambda *a:calls.append(a) or {'pid':123},port=0)
    url=f'http://127.0.0.1:{server.server_port}/v1/command'
    body=json.dumps(make_request('status')).encode()
    try:
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(url,data=body),timeout=2)
        assert exc.value.code==403 and not calls
        with urlopen(Request(url,data=body,headers={'Authorization':'Bearer '+token_value()}),timeout=2) as response:
            assert json.load(response)['result']['pid']==123
        assert len(calls)==1
    finally: server.close()
