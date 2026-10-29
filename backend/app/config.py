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


def _read_version() -> str:
    """版本号唯一来源：仓库根 VERSION 文件（版本纪律：v0.1.0 → v0.2.0 → v1.0.0）。"""
    try:
        text = (ROOT_DIR / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return "1.0.0"
    return text or "1.0.0"


VERSION = _read_version()

DEMO_MODE = os.environ.get("DEMO_MODE", "1") != "0"
LLM_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
LLM_TIMEOUT = float(os.environ.get("DEEPSEEK_TIMEOUT", "8"))     # 单次请求超时（秒）
LLM_ENABLED = bool(LLM_API_KEY) and os.environ.get("ZEROSTEP_DISABLE_LLM", "") != "1"

# ---------------- LLM 接入加固（v0.3.1，均可在调用时热改 / reload 覆盖） ----------------
LLM_BUDGET = float(os.environ.get("DEEPSEEK_BUDGET", "10"))              # 单轮总预算（秒，含重试）
LLM_MAX_TOKENS = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "800"))        # 输出上限（关思考后够用）
LLM_THINKING = (os.environ.get("DEEPSEEK_THINKING", "disabled") or "disabled").strip().lower()  # disabled 关闭思考
LLM_RETRY = int(os.environ.get("DEEPSEEK_RETRY", "1"))                    # 失败重试次数（1 = 最多两次请求）
LLM_RETRY_BACKOFF_MS = int(os.environ.get("DEEPSEEK_RETRY_BACKOFF_MS", "600"))
LLM_BREAKER_THRESHOLD = int(os.environ.get("DEEPSEEK_BREAKER_THRESHOLD", "3"))  # 连续失败多少次熔断
LLM_BREAKER_COOLDOWN = float(os.environ.get("DEEPSEEK_BREAKER_COOLDOWN", "60"))  # 熔断冷却秒数

# 金额单位：分
YUAN = 100
SINGLE_LIMIT_CENTS = 1000 * YUAN          # 官方红线：大额转账 > 1000 元需强验证
DAILY_LIMIT_CENTS = 1000 * YUAN           # 官方黄线：日累计 <= 1000 元为小额
HARD_SINGLE_LIMIT_CENTS = 50000 * YUAN    # 演示系统单笔硬上限 5 万元，超过直接拒绝
APPROVAL_TTL_SECONDS = 300
UNDO_WINDOW_SECONDS = 60
TOTP_WINDOW = 1
NEW_PAYEE_REQUIRES_HIGH = True

SEED_VERSION = "2026.11.01"
DEFAULT_USER_ID = "u001"

# ---------------- 外部只读服务（MCP 子集，JSON-RPC over HTTP） ----------------
# 全部可关：MCP_ENABLED=0 或服务没起时，主流程自动降级到内置能力，资金安全边界不变。
MCP_ENABLED = os.environ.get("MCP_ENABLED", "1") != "0"
MCP_FX_URL = os.environ.get("MCP_FX_URL", "http://127.0.0.1:8300/rpc")
MCP_RISK_URL = os.environ.get("MCP_RISK_URL", "http://127.0.0.1:8301/rpc")
MCP_TIMEOUT_MS = int(os.environ.get("MCP_TIMEOUT_MS", "10000"))   # 读超时 10s
MCP_CONNECT_TIMEOUT_MS = int(os.environ.get("MCP_CONNECT_TIMEOUT_MS", "2000"))
MCP_TOOLS_CACHE_SECONDS = 60
# 外部汇率不可用时的内置参考价（1 CNY = X 外币），仅用于演示降级，不参与真实报价
MCP_RISK_BLOCK_SCORE = int(os.environ.get("MCP_RISK_BLOCK_SCORE", "60"))   # 外部风控分 >= 该值则拦截（只加严）
MCP_FX_FALLBACK_RATE = {"USD": 0.1403, "HKD": 1.0921, "EUR": 0.1287, "JPY": 20.85}

# ---------------- 通用 MCP 服务注册表（v0.6.0） ----------------
# MCP_SERVERS：JSON 数组，可同时接入任意多个外部只读 MCP 服务（银行开放平台、
# 汇率、风控……）。留空时保持 v0.5.x 行为：仍是 fx / risk 两个内置服务。
# 每项字段与安全约定见 docs/mcp.md §8；密钥一律用 env:VAR / ${VAR} 间接引用，
# 不把明文写进 JSON（JSON 可能出现在 CI 日志、演示命令里）。
MCP_SERVERS = (os.environ.get("MCP_SERVERS", "") or "").strip()
MCP_MAX_SERVERS = int(os.environ.get("MCP_MAX_SERVERS", "8"))            # 最多接入服务数（防误配置打爆）
MCP_ALLOW_WRITE_TOOLS = os.environ.get("MCP_ALLOW_WRITE_TOOLS", "0") == "1"  # 全局写工具开关，默认关闭
MCP_OAUTH_REFRESH_SKEW = int(os.environ.get("MCP_OAUTH_REFRESH_SKEW", "60"))  # OAuth2 token 提前刷新秒数

# ---------------- IM 渠道适配层（webhook，两步确认） ----------------
CHANNEL_ENABLED = os.environ.get("CHANNEL_ENABLED", "1") != "0"
CHANNEL_PORT = int(os.environ.get("CHANNEL_PORT", "8400"))
CHANNEL_TOKEN = os.environ.get("CHANNEL_TOKEN", "zerostep-demo-token").strip() or "zerostep-demo-token"
CHANNEL_MAX_BODY = 32 * 1024
CHANNEL_IDEMPOTENT_SECONDS = 10
CHANNEL_MESSAGE_LOG = BASE_DIR / "channels" / "messages.jsonl"
CHANNEL_SESSIONS_FILE = BASE_DIR / "channels" / "sessions.json"

# ---------------- 插件注册表（仿 DSH 能力开关，v0.3.0） ----------------
# 插件启停状态的持久化文件；文件不存在 = 全部启用。测试可设 PLUGINS_FILE 指向临时文件。
PLUGINS_FILE = Path(os.environ.get("PLUGINS_FILE", str(DATA_DIR / "plugins.json")))

# ---------------- 企业微信回调适配器（v0.3.2） ----------------
# 官方回调协议：GET 验签后解密 echostr 并回显明文；POST 收加密 XML，被动加密回包。
# 未配置 Token / EncodingAESKey / CorpID 时回调接口返回 503，不影响主流程。
WECOM_ENABLED = os.environ.get("WECOM_ENABLED", "1") != "0"
WECOM_PORT = int(os.environ.get("WECOM_PORT", "8401"))
WECOM_TOKEN = os.environ.get("WECOM_TOKEN", "").strip()          # 回调验签 Token
WECOM_AES_KEY = os.environ.get("WECOM_AES_KEY", "").strip()      # 43 位 EncodingAESKey
WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "").strip()      # 企业 CorpID
WECOM_TIMEOUT = float(os.environ.get("WECOM_TIMEOUT", "5"))      # 转发主服务硬超时（秒）
WECOM_DEDUPE_SECONDS = int(os.environ.get("WECOM_DEDUPE_SECONDS", "300"))  # MsgId 去重窗口（秒）

# ---------------- 多因子强验证与会话安全锁定（v0.4.1） ----------------
# 短信/人脸因子为演示级模拟实现（内存态，不接真实网关）；TTL 与次数阈值秒级可测。
SMS_TTL_SECONDS = int(os.environ.get("SMS_TTL", "120"))            # 短信验证码有效期（秒）
FACE_TTL_SECONDS = int(os.environ.get("FACE_TTL", "60"))           # 人脸确认令牌有效期（秒）
SMS_MAX_ATTEMPTS = int(os.environ.get("SMS_MAX_ATTEMPTS", "5"))    # 同一短信验证码最多失败次数
SECURITY_LOCK_THRESHOLD = int(os.environ.get("SECURITY_LOCK_THRESHOLD", "3"))              # 窗口内安全事件达到该次数即锁定会话
SECURITY_LOCK_WINDOW_SECONDS = int(os.environ.get("SECURITY_LOCK_WINDOW_SECONDS", "600"))  # 安全事件统计窗口（秒）
SECURITY_LOCK_SECONDS = int(os.environ.get("SECURITY_LOCK_SECONDS", "300"))                # 会话锁定时长（秒）
