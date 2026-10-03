"""去标识化身份与确定性分桶。

- 用户原始 id 一律 HMAC-SHA256 派生伪 ID，事件日志中不出现明文用户标识；
- 分桶由 (实验, 伪ID, 盐) 确定性哈希得到，暂停/恢复后同一用户必然落回原桶，
  从机制上杜绝恢复后的跨组污染；换盐即作废旧分配（用于全新实验轮次）。
"""

from __future__ import annotations

import hashlib
import hmac

DEFAULT_PEPPER = "long-lab-dev-pepper"  # 生产由密钥管理注入


def pseudonymize(raw_id: str, pepper: str = DEFAULT_PEPPER) -> str:
    if not raw_id:
        raise ValueError("raw_id 不能为空")
    digest = hmac.new(pepper.encode(), raw_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"u_{digest}"


def deterministic_bucket(user_pseudo: str, experiment_id: str, salt: str, buckets: int) -> int:
    assert buckets >= 2
    msg = f"{experiment_id}|{salt}|{user_pseudo}".encode()
    slot = int(hmac.new(salt.encode(), msg, hashlib.sha256).hexdigest(), 16)
    return slot % buckets


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
