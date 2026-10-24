from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

from labcontrol.protocol import Dispatcher, make_request

TOKEN = 'reservation-uuid-0001'


def reserve(dispatcher, token=TOKEN, request_id='reserve'):
    return dispatcher.execute(make_request('lease.acquire', {'lease_id':token,'owner':'agent-a','ttl':300}, request_id))


def test_lease_shared_by_both_apps_survives_recreation_and_leaves_reads_open(tmp_path):
    calls = []
    a = Dispatcher('host', lambda *args:{}, tmp_path)
    b = Dispatcher('blacs', lambda action,params:calls.append(action) or {}, tmp_path)
    assert reserve(a)['ok']
    assert b.execute(make_request('status'))['ok']
    assert not b.execute(make_request('queue.resume'))['ok']
    restarted = Dispatcher('runmanager', lambda action,params:calls.append(action) or {}, tmp_path)
    assert not restarted.execute(make_request('compile'))['ok']
    assert restarted.execute(make_request('compile', lease_id=TOKEN))['ok']
    assert calls == ['status','compile']
    status = a.execute(make_request('lease.status'))['result']
    assert status['held'] and 'lease_id' not in status
    assert not a.execute(make_request('lease.release',{'lease_id':'incorrect-uuid-00'}))['ok']
    assert a.execute(make_request('lease.release',{'lease_id':TOKEN}))['ok']
    assert b.execute(make_request('queue.pause'))['ok']


def test_expired_credential_is_rejected_and_receipts_do_not_renew(tmp_path):
    a = Dispatcher('host', lambda *args:{}, tmp_path)
    first = reserve(a)
    assert reserve(a) == first
    with sqlite3.connect(a.gate.database) as db:
        db.execute('UPDATE lease SET expires=?', (time.time()-1,))
    assert not a.execute(make_request('compile', lease_id=TOKEN))['ok']
    assert reserve(a) == first  # An old receipt is not a fresh reservation.
    assert not a.execute(make_request('lease.status'))['result']['held']
    assert reserve(a, 'reservation-uuid-0002', 'reserve-again')['ok']


def test_cross_dispatcher_serialization_and_same_id_at_most_once(tmp_path):
    entered = threading.Event()
    finish = threading.Event()
    calls = []
    def callback(action, params):
        calls.append(action)
        entered.set()
        assert finish.wait(1)
        return {}
    a = Dispatcher('blacs', callback, tmp_path)
    b = Dispatcher('blacs', callback, tmp_path)
    request = make_request('tab.restart', request_id='same')
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(a.execute, request)
        assert entered.wait(1)
        two = pool.submit(b.execute, request)
        finish.set()
        assert one.result()['ok']
        two.result()
    assert calls == ['tab.restart']
    assert b.execute(make_request('operation.get',{'request_id':'same'}))['ok']


def test_competing_acquisitions_have_one_owner(tmp_path):
    clients = [Dispatcher('host', lambda *args:{}, tmp_path) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item:reserve(item[1], 'reservation-uuid-000'+str(item[0]), str(item[0])), enumerate(clients)))
    assert sum(response['ok'] for response in results) == 1


def test_app_start_uses_same_lease_and_durable_receipts(tmp_path, monkeypatch):
    from labcontrol.client import execute_local
    from labcontrol import windows
    monkeypatch.setenv('LABCONTROL_STATE_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(windows, 'start_app', lambda app:calls.append(app) or {'accepted':True})
    host = Dispatcher('host', lambda *args:{}, tmp_path)
    assert reserve(host)['ok']
    assert not execute_local('blacs', make_request('app.start'))['ok']
    assert calls == []
    request = make_request('app.start', request_id='start-once', lease_id=TOKEN)
    first = execute_local('blacs', request)
    assert first['ok']
    assert execute_local('blacs', request) == first
    assert calls == ['blacs']
    assert execute_local('blacs', make_request('operation.get',{'request_id':'start-once'})) == first
