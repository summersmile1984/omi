"""Execute the provider as its non-root Linux UID with native file permissions.

The existing Docker product lane invokes this inside its freshly built image.
Docker Desktop shares can map host ownership differently from Linux bind mounts,
so recreate the actual profile's bytes and mode on the container filesystem.
Ubuntu Fork Checks 34337809124 stopped during service startup; reproducing the
0600 generated profile denied the provider's import before its health endpoint.
"""

import os
from pathlib import Path
import pwd
import runpy
import stat


def main():
    original = Path('/proof/profile.json')
    native = Path('/contract/profile.json')
    native.write_bytes(original.read_bytes())
    native.chmod(stat.S_IMODE(original.stat().st_mode))
    private = Path('/contract/private-control')
    private.write_text('synthetic-private-control')
    private.chmod(0o600)

    user = pwd.getpwnam('omi')
    assert user.pw_uid != 0
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    # Import the actual provider, including its startup profile read. An
    # unreadable generated profile fails here even on Docker Desktop.
    provider = runpy.run_path('/contract/providers.py', run_name='profile_access_test')
    assert provider['CONTRACT']['dimension'] == 1024
    try:
        private.read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('provider can read another UID\'s private file')
    print('non-root Linux provider profile access and private-file denial passed')


if __name__ == '__main__':
    main()
