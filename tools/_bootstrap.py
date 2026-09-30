"""让 tools/ 下的脚本能被直接运行：把项目根目录加进 sys.path。

直接跑 `python tools/scan_clean.py` 时，`sys.path[0]` 是 `tools/` 而不是项目根，
所以 `from lyric_tag_translator import ...` 会失败。在其它脚本的 import 区最前面加一行：

    import _bootstrap  # noqa: F401

就能正常导入了。（这一行必须排在 `from lyric_tag_translator...` 之前。）
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
