"""Hidden checks. Run with WRASSE_WS=<workspace dir>; each check file runs in its own process."""
import os
import sys

sys.path.insert(0, os.environ["WRASSE_WS"])
