import sys

for _s in (sys.stdout, sys.stderr):   # émojis des journaux sur les consoles non UTF-8
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from mangarr.cli import main

sys.exit(main())
