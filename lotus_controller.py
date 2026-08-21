#!/usr/bin/env python3
"""Entry point kept for the old ``python lotus_controller.py …`` invocation.

The implementation now lives in the :mod:`lotus_led` package, split along the
same lines as the Rust version. Everything you could do before still works from
here; ``python -m lotus_led.cli`` and ``led`` are the same program.
"""

import sys

from lotus_led.cli import main

if __name__ == "__main__":
    sys.exit(main())
