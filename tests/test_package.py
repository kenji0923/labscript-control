from importlib.resources import files
import subprocess
import sys


def test_module_cli_works_outside_checkout(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'labcontrol', '--help'], cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    assert '--target' in result.stdout and '--local' in result.stdout


def test_installer_resource_is_packaged():
    script = files('labcontrol').joinpath('register_tasks.ps1').read_text()
    assert '-LogonType Interactive' in script
    assert 'Start-ScheduledTask' not in script
