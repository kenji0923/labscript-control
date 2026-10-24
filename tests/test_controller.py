import sys
import types
from types import SimpleNamespace as NS
import pytest
from labcontrol.blacs_plugin import Controller,queue_revision
from labcontrol.protocol import ControlError


class Model:
    def __init__(self): self.paths=['one.h5','two.h5']
    def rowCount(self): return len(self.paths)
    def item(self,i): return NS(text=lambda:self.paths[i])


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setitem(sys.modules,'qtutils',NS(inmain_decorator=lambda *a:lambda f:f))
    model=Model()
    queue=NS(_model=model,manager_paused=False,manager_repeat=False,manager_repeat_mode=0,
             get_status=lambda:'Idle',set_status=lambda *a:None,get_next_file=lambda:'one.h5')
    tab=NS(mode=1,state='idle',error_message='',workers={'worker':(NS(pid=1),None,None)},restart=lambda:None)
    app=NS(queue=queue,tablist={'emccd':tab},failed_device_settings={})
    return Controller(app)


def test_dequeue_rechecks_pause(controller):
    controller.command('queue.pause',{})
    with pytest.raises(IndexError): controller.queue.get_next_file()
    assert controller.reserved is None


def test_restart_refuses_running_shot(controller):
    controller.queue.get_next_file()
    with pytest.raises(ControlError,match='not idle'):
        controller.command('tab.restart',{'name':'emccd'})
    with pytest.raises(ControlError,match='not idle'):
        controller.command('app.restart',{})


def test_edit_requires_pause_and_matching_revision(controller):
    with pytest.raises(ControlError,match='Pause'):
        controller.command('queue.clear',{'revision':queue_revision(['one.h5','two.h5'])})
    controller.command('queue.pause',{})
    with pytest.raises(ControlError,match='Queue changed'):
        controller.command('queue.clear',{'revision':'stale'})
    assert controller.queue._model.paths==['one.h5','two.h5']


def test_tab_restart_does_not_resume_queue(controller):
    response=controller.command('tab.restart',{'name':'emccd'})
    assert response['accepted'] and controller.queue.manager_paused
    assert response['generation']==1


def test_errors_block_queue_resume(controller):
    controller.app.tablist['emccd'].error_message='backend unavailable'
    with pytest.raises(ControlError,match='not ready'):
        controller.command('queue.resume',{})


def test_zprocess_worker_pid_and_startup_readiness(controller):
    tab = controller.app.tablist['emccd']
    tab.workers = {'worker': (NS(child=NS(pid=123)), None, None)}
    assert controller.tabs()['emccd']['workers'] == {'worker': 123}
    assert controller.tabs()['emccd']['ready']
    tab.state = 'Starting worker process...'
    assert not controller.tabs()['emccd']['ready']
