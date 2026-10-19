import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _post import post  # noqa: E402

post("/hooks/prompt")
sys.exit(0)
