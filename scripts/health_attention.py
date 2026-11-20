"""Post or resolve a Cirra health alert through the GLAS-injected event token."""

import sys
from pathlib import Path

from cirra.health_notify import main

MARKER = Path('/srv/lugos/state/cirra/health-alert.pending')


if __name__ == '__main__':
    raise SystemExit(main([*sys.argv[1:], str(MARKER)]))
