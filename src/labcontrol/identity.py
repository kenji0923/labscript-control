"""Identity supplied by the actual execution host, not by a client-side alias."""
import getpass
import platform
import socket

PROTOCOL_VERSION = 2


def machine_info():
    return {'hostname': socket.gethostname(), 'user': getpass.getuser(),
            'platform': platform.system(), 'protocol_version': PROTOCOL_VERSION}


def same_host(left, right):
    return isinstance(left, str) and isinstance(right, str) and left.casefold() == right.casefold()
