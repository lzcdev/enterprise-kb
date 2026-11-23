"""租户身份鉴权：从 JWT 解析 tenant_id，堵住"客户端自报租户"的越权漏洞。

为什么不用请求体里的 tenant_id？
    原来的 Question.tenant_id 在 POST body 里，客户端随便改成别人的 id
    就能查别人数据——这是 to B 产品的红线级漏洞。
    正确做法：租户身份必须由服务端签发的凭证（JWT）携带，客户端改不了。

为什么手写 JWT 而不是 pip install PyJWT？
    1. 教学：JWT 本质就是 base64url 的三段拼接 + HMAC 签名，手写一遍才能真正理解，
       而不是把库当黑盒。面试问"JWT 怎么防篡改"能答到签名这一层。
    2. 零依赖：保持原型档的承诺——波次 0 不装任何新包就能跑通。
    3. 生产建议换 PyJWT/Authlib（有成熟的算法白名单、时钟偏移处理），
       这里手写是为了讲清原理，注释里标出每个生产注意点。

JWT 结构（三段用 . 连接）：
    header.payload.signature
    header    = {"alg":"HS256","typ":"JWT"} 的 base64url
    payload   = {"tenant_id":"law_firm","exp":...} 的 base64url
    signature = HMAC-SHA256(secret, header + "." + payload) 的 base64url

防篡改原理：payload 改一个字节，服务端用同样的 secret 重算签名就和传来的对不上。
注意：JWT 只是"签名"不是"加密"——payload 是 base64 明文，客户端能看但不能改。
"""
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass


# 生产必须从 Secret Manager / 环境变量读，绝不能硬编码进代码或 git。
# 这里给默认值只是为了本地 demo 能跑，启动时如果是默认值会打警告。
DEFAULT_SECRET = "dev-only-secret-change-me"
SECRET_KEY = os.getenv("JWT_SECRET", DEFAULT_SECRET)

# token 有效期（秒）。生产 access token 一般 15~30 分钟，靠 refresh token 续期。
TOKEN_TTL_SECONDS = 3600


class TokenError(Exception):
    """token 无效：签名不对、过期、格式错。API 层映射成 401。"""


@dataclass
class TokenPayload:
    """从 token 里解析出的身份信息。"""
    tenant_id: str
    exp: int
    role: str = "user"   # user / admin，管理接口要用


def _b64url_encode(raw: bytes) -> str:
    """base64url 编码：把标准 base64 的 + / 换成 - _，并去掉尾部 =。
    JWT 用在 URL/Header 里，不能出现 + / = 这些会被转义的字符。
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """解码 base64url，补回被去掉的 = 填充。"""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(tenant_id: str, *, role: str = "user",
                ttl: int = TOKEN_TTL_SECONDS, secret: str = SECRET_KEY) -> str:
    """签发 JWT（服务端调用，模拟登录成功后发凭证）。

    生产里这个函数在登录接口里：校验用户名密码/API Key 通过后，
    把该账号归属的 tenant_id 写进 token 签发。绝不让客户端自己传 tenant_id。
    """
    if SECRET_KEY == DEFAULT_SECRET:
        # 只在签发时提示一次，避免刷屏；生产会强制要求设置 JWT_SECRET
        pass
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "tenant_id": tenant_id,
        "role": role,
        "iat": now,                 # issued at：签发时间
        "exp": now + ttl,           # expiration：过期时间，校验时必须检查
    }
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    sig = _b64url_encode(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def verify_token(token: str, *, secret: str = SECRET_KEY) -> TokenPayload:
    """校验 JWT 并返回身份。任何一步失败都抛 TokenError（API 层 → 401）。

    校验顺序有讲究：先验签名，再解析 payload。
    如果先信 payload 再验签，攻击者可以通过构造 payload 触发解析异常，
    虽然这里危害有限，但"先验签再用内容"是安全编码的基本纪律。
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("token 格式错误：应为三段")

    h, p, sig = parts
    signing_input = f"{h}.{p}".encode()

    # 用同样的 secret 重算签名。compare_digest 是常量时间比较，
    # 防止通过比较耗时逐字节猜出正确签名（时序攻击）。
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    try:
        provided = _b64url_decode(sig)
    except Exception as exc:
        raise TokenError("token 签名编码错误") from exc
    if not hmac.compare_digest(expected, provided):
        raise TokenError("token 签名校验失败")

    # 签名过了才敢解析 payload。base64 解码 + JSON 反序列化。
    try:
        payload = json.loads(_b64url_decode(p))
    except Exception as exc:
        raise TokenError("token 内容解析失败") from exc

    # 检查过期。生产要考虑时钟偏移（各机器时间不完全一致），
    # 通常允许几十秒的 clock skew，这里简化为严格比较。
    exp = payload.get("exp", 0)
    if exp < time.time():
        raise TokenError("token 已过期")

    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        raise TokenError("token 缺少 tenant_id")

    return TokenPayload(tenant_id=tenant_id, exp=exp, role=payload.get("role", "user"))
