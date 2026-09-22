import sys
from pathlib import Path

# crawler.py is a standalone script (no package), so make its directory
# importable as `import crawler` regardless of the pytest invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
