"""MoneyTrail 全局配置：阈值、常量、路径。

约定（对应项目介绍书 6.3 / 6.5）：
- 金额一律整数「分」，本文件不出现任何浮点金额；
- 阈值集中在此处，不硬编码进业务逻辑；
- 模型凭据只从环境变量读取，不写入源码、日志、git 历史。
"""

from pathlib import Path

APP_NAME = "MoneyTrail"
APP_SUBTITLE = "资金链路核查台"
VERSION = "1.0"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = BASE_DIR / "moneytrail.db"
WEB_DIR = BASE_DIR / "web"
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

HOST = "127.0.0.1"
PORT = 8000

# ---------------------------------------------------------------- 检测规则

LABELS = ("NOT_SETTLED", "DUP_REFUND", "AMOUNT_MISMATCH", "FEE_ANOMALY")

RULES_ENABLED = {label: True for label in LABELS}

RULE_NAME = {
    "NOT_SETTLED": "规则一 · 已支付未到账",
    "DUP_REFUND": "规则二 · 重复退款",
    "AMOUNT_MISMATCH": "规则三 · 到账金额不符",
    "FEE_ANOMALY": "规则四 · 手续费异常",
}

LABEL_TEXT = {
    "NOT_SETTLED": "已支付未到账",
    "DUP_REFUND": "重复退款",
    "AMOUNT_MISMATCH": "到账金额不符",
    "FEE_ANOMALY": "手续费异常",
}

LABEL_SHORT = {
    "NOT_SETTLED": "NS",
    "DUP_REFUND": "DR",
    "AMOUNT_MISMATCH": "AM",
    "FEE_ANOMALY": "FA",
}

SEVERITY = {
    "NOT_SETTLED": "high",
    "DUP_REFUND": "high",
    "AMOUNT_MISMATCH": "medium",
    "FEE_ANOMALY": "low",
}

SEVERITY_TEXT = {"high": "高", "medium": "中", "low": "低"}

# 费率容差 10%：只用于「费率比值」比较，不参与任何金额加减。
FEE_TOLERANCE_RATIO = 0.10

# 重复退款判定：同额 + 时间窗（分钟）
DUP_REFUND_WINDOW_MINUTES = 10

# ---------------------------------------------------------------- 渠道

CHANNELS = ("wechat", "alipay", "meituan")

CHANNEL_TEXT = {
    "wechat": "微信支付",
    "alipay": "支付宝",
    "meituan": "美团外卖",
}

# 约定费率，单位「基点」（1 bp = 0.01%）。整数运算，避免浮点误差。
DEFAULT_RATE_BP = {"wechat": 60, "alipay": 55, "meituan": 200}

# ---------------------------------------------------------------- 工单状态

STATUSES = ("pending", "in_progress", "resolved", "ignored")

STATUS_TEXT = {
    "pending": "待处理",
    "in_progress": "处理中",
    "resolved": "已解决",
    "ignored": "已忽略",
}

# 「未结」= 计入待核差异
OPEN_STATUSES = ("pending", "in_progress")

# ---------------------------------------------------------------- AI 助手

BAIZE_API_KEY_ENV = "BAIZE_API_KEY"
BAIZE_BASE_URL_ENV = "BAIZE_BASE_URL"
BAIZE_MODEL_ENV = "BAIZE_MODEL"
BAIZE_NO_AUTH_ENV = "BAIZE_NO_AUTH"

# 默认接入本机模型端点（无需凭据）；换云端端点时设 BAIZE_BASE_URL 且 BAIZE_NO_AUTH=0
DEFAULT_BAIZE_BASE_URL = "http://127.0.0.1:8766/v1"
DEFAULT_BAIZE_MODEL = "Baize-Omni-Sense"
DEFAULT_BAIZE_NO_AUTH = "1"

AI_TIMEOUT_SECONDS = 25
AI_KILL_SWITCH_KEY = "ai_kill_switch"
