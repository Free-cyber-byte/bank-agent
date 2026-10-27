"""ZeroStep 全局配置（零依赖，仅标准库）。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # bank-agent/backend
ROOT_DIR = BASE_DIR.parent                                  # bank-agent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bank.db"
FRONTEND_DIR = ROOT_DIR / "frontend"
TASKS_DIR = ROOT_DIR / "tasks"


def _load_dotenv(path):
    """读取项目根目录的 .env（每行 KEY=VALUE，# 开头为注释）。

    只做最小实现：不覆盖已存在的环境变量，支持 UTF-8 BOM 与简单引号。
    找不到文件或读失败时静默跳过，保证零依赖、零配置也能跑。
    """
    if not path.is_file():
        return
    try:
        raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT_DIR / ".env")

APP_NAME = "ZeroStep 零步银行"
VERSION = "1.0.0"

DEMO_MODE = os.environ.get("DEMO_MODE", "1") != "0"
LLM_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
LLM_TIMEOUT = float(os.environ.get("DEEPSEEK_TIMEOUT", "12"))
LLM_ENABLED = bool(LLM_API_KEY) and os.environ.get("ZEROSTEP_DISABLE_LLM", "") != "1"

# 金额单位：分
YUAN = 100
SINGLE_LIMIT_CENTS = 1000 * YUAN          # 官方红线：大额转账 > 1000 元需强验证
DAILY_LIMIT_CENTS = 1000 * YUAN           # 官方黄线：日累计 <= 1000 元为小额
HARD_SINGLE_LIMIT_CENTS = 50000 * YUAN    # 演示系统单笔硬上限 5 万元，超过直接拒绝
APPROVAL_TTL_SECONDS = 300
UNDO_WINDOW_SECONDS = 60
TOTP_WINDOW = 1
NEW_PAYEE_REQUIRES_HIGH = True

SEED_VERSION = "2026.10.26"
DEFAULT_USER_ID = "u001"
