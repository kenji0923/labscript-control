import pytest
from labcontrol.install_windows import BEGIN, END, render_runmanager_hook, enable_plugin


def test_hook_is_idempotent_and_replaces_old_checkout_path():
    source = "if __name__ == '__main__':\n    remote_server = RemoteServer()\n    qapplication.exec_()\n"
    updated = render_runmanager_hook(source)
    assert updated.count(BEGIN) == 1
    assert render_runmanager_hook(updated) == updated
    old = source.replace('    qapplication.exec_()',
        f"    {BEGIN}\n    import sys\n    sys.path.insert(0, 'C:/old-checkout/script')\n    {END}\n    qapplication.exec_()")
    result = render_runmanager_hook(old)
    assert 'old-checkout' not in result
    assert result == updated


@pytest.mark.parametrize('source', ['pass\n', BEGIN, END, END + '\n' + BEGIN])
def test_unknown_startup_or_incomplete_hook_is_rejected(source):
    with pytest.raises(RuntimeError):
        render_runmanager_hook(source)


def test_enable_plugin_changes_only_plugin_section_and_is_idempotent():
    source = '[other]\nagent_control = False\n\n[BLACS/plugins]\nagent_control = False\nother = True\n[next]\nx = 1\n'
    result = enable_plugin(source)
    assert result.startswith('[other]\nagent_control = False')
    assert '[BLACS/plugins]\nagent_control = True\nother = True' in result
    assert enable_plugin(result) == result
    assert 'agent_control = True' in enable_plugin('[BLACS/plugins]')
