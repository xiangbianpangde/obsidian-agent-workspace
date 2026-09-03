import sys
from pathlib import Path

# 确保 backend/ 目录在 sys.path 中，支持根目录下 unittest discover 直接发现
backend_dir = Path(__file__).resolve().parents[1]
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
