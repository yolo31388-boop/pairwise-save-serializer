"""存档/序列化系统。

修复点：
1. 版本号 + 迁移函数链，旧版本逐步迁移到 CURRENT_VERSION。
2. 有序字典 + 键排序，相同内容序列化出的字节完全一致。
3. 循环引用：容器首次出现分配 __id__，再次出现写 __ref__。
4. 浮点用 repr（最短可还原表示）写 __f__ 标记，Decimal 写 __d__，读回精度不丢。
5. save 走同目录临时文件 + os.replace 原子替换，失败保留旧文件。
6. 云存档基于共同祖先和操作日志做三方合并；无祖先时按字段 union，不覆盖。
7. delete 同时清内存缓存，并写入墓碑标记，缓存命中已删存档返回 None。
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class SaveData:
    version: int
    player: dict
    inventory: list
    timestamp: float
    ops: list = field(default_factory=list)
    base: object = None


class SaveSystem:
    CURRENT_VERSION = 3

    def __init__(self):
        # 迁移链：from_version -> (to_version, migrate_fn)，逐步升级
        self.migrations = {
            1: (2, self._migrate_1_to_2),
            2: (3, self._migrate_2_to_3),
        }
        # path -> SaveData 内存缓存
        self.cache: dict = {}
        # 已删除路径墓碑，防止删后再读到陈旧数据
        self._tombstones: set = set()

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------
    def _migrate_1_to_2(self, d: dict) -> dict:
        d["version"] = 2
        player = d.setdefault("player", {})
        player["hp"] = player.pop("health", player.get("hp", 100))
        return d

    def _migrate_2_to_3(self, d: dict) -> dict:
        d["version"] = 3
        d["inventory"] = [
            item if isinstance(item, dict) else {"id": item, "count": 1}
            for item in d.get("inventory", [])
        ]
        return d

    def migrate(self, d: dict) -> dict:
        """把任意旧版本的存档 dict 沿迁移链逐步升到最新。"""
        version = d.get("version", 1)
        seen = 0
        while version < self.CURRENT_VERSION:
            if version not in self.migrations:
                raise ValueError(f"未知的存档版本: {version}，无法迁移")
            next_version, migrate_fn = self.migrations[version]
            d = migrate_fn(d)
            d["version"] = next_version
            version = next_version
            seen += 1
            if seen > self.CURRENT_VERSION + 1:
                raise ValueError("迁移链异常（可能成环）")
        return d

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def _encode(self, obj, memo: dict):
        # 循环引用：容器（dict/list/tuple）第一次登记并分配 id，
        # 之后再遇到直接写 {"__ref__": id}
        if isinstance(obj, (dict, list, tuple)):
            marker_type = type(obj)
            existing = memo.get(id(obj))
            if existing is not None:
                return {"__ref__": existing}
            ref_id = len(memo)
            memo[id(obj)] = ref_id

            if isinstance(obj, dict):
                # 键排序，保证字节确定
                encoded = {"__id__": ref_id}
                for key in sorted(obj, key=lambda k: str(k)):
                    encoded[str(key)] = self._encode(obj[key], memo)
                return encoded
            items = [self._encode(item, memo) for item in obj]
            if marker_type is tuple:
                return {"__id__": ref_id, "__tuple__": True, "items": items}
            return {"__id__": ref_id, "items": items}

        if isinstance(obj, bool) or isinstance(obj, int) or obj is None:
            return obj
        if isinstance(obj, Decimal):
            return {"__d__": str(obj)}
        if isinstance(obj, float):
            # repr(float) 是保证往返还原的最短表示，精度不丢
            return {"__f__": repr(obj)}
        if isinstance(obj, str):
            return obj
        raise TypeError(f"不支持序列化的类型: {type(obj)!r}")

    def serialize(self, data: SaveData) -> bytes:
        encoded = self._encode(
            {
                "version": data.version,
                "player": data.player,
                "inventory": data.inventory,
                "timestamp": data.timestamp,
                "ops": data.ops,
            },
            memo={},
        )
        # sort_keys 再排一次；separators 紧凑且固定
        return json.dumps(
            encoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def _decode(self, obj, table: dict):
        if isinstance(obj, dict):
            if "__ref__" in obj:
                return table[obj["__ref__"]]
            if "__f__" in obj:
                return float(obj["__f__"])
            if "__d__" in obj:
                return Decimal(obj["__d__"])
            if "items" in obj:
                placeholder = tuple() if obj.get("__tuple__") else []
                if "__id__" in obj:
                    table[obj["__id__"]] = placeholder
                items = [self._decode(item, table) for item in obj["items"]]
                if obj.get("__tuple__"):
                    result = tuple(items)
                    if "__id__" in obj:
                        table[obj["__id__"]] = result
                    return result
                placeholder.extend(items)
                return placeholder

            result = {}
            if "__id__" in obj:
                table[obj["__id__"]] = result
            for key in sorted(obj):
                if key == "__id__":
                    continue
                result[key] = self._decode(obj[key], table)
            return result
        if isinstance(obj, list):
            return [self._decode(item, table) for item in obj]
        return obj

    def deserialize(self, raw) -> SaveData:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        decoded = self._decode(json.loads(raw.decode("utf-8")), table={})
        # 兼容没有引用标记的老格式
        migrated = self.migrate(decoded)
        return SaveData(
            version=migrated["version"],
            player=migrated.get("player", {}),
            inventory=migrated.get("inventory", []),
            timestamp=migrated.get("timestamp", 0),
            ops=migrated.get("ops", []),
        )

    # ------------------------------------------------------------------
    # 原子写入
    # ------------------------------------------------------------------
    def save(self, path: str, data: SaveData):
        raw = self.serialize(data)
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".save-", suffix=".tmp", dir=directory)
        try:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(tmp_path, path)
        except Exception:
            # 替换失败/写失败：旧文件原样保留
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
        # 写成功才刷新缓存并摘墓碑
        self.cache[path] = data
        self._tombstones.discard(path)

    def load(self, path: str) -> SaveData:
        if path in self._tombstones:
            raise FileNotFoundError(f"存档已被删除: {path}")
        if path in self.cache:
            return self.cache[path]
        with open(path, "rb") as handle:
            data = self.deserialize(handle.read())
        self.cache[path] = data
        return data

    # ------------------------------------------------------------------
    # 云存档三方合并
    # ------------------------------------------------------------------
    def _apply_ops(self, base: SaveData, ops: list) -> dict:
        player = dict(base.player) if base and base.player else {}
        inventory = [dict(item) if isinstance(item, dict) else item
                     for item in (base.inventory if base else [])]
        for op in ops:
            kind = op.get("op")
            if kind in ("set", "field"):
                player[op["key"]] = op["value"]
            elif kind == "remove_field":
                player.pop(op["key"], None)
            elif kind == "add_item":
                item = op.get("item")
                if item not in inventory:
                    inventory.append(item)
            elif kind == "remove_item":
                item = op.get("item")
                item_id = item.get("id") if isinstance(item, dict) else item
                inventory = [
                    kept for kept in inventory
                    if not (
                        (kept.get("id") if isinstance(kept, dict) else kept) == item_id
                        or kept == item
                    )
                ]
        return {"player": player, "inventory": inventory}

    def _changes(self, base_snapshot, tip_snapshot, ops):
        """返回 {路径: (动作, 新值)}，优先用操作日志，缺失时退化为快照 diff。"""
        changes = {}
        if ops:
            for op in ops:
                kind = op.get("op")
                if kind in ("set", "field"):
                    changes[("player", op["key"])] = ("set", op["value"])
                elif kind == "remove_field":
                    changes[("player", op["key"])] = ("remove", None)
                elif kind == "add_item":
                    changes[("inventory", "add", json.dumps(op.get("item"), sort_keys=True, default=str))] = (
                        "add_item",
                        op.get("item"),
                    )
                elif kind == "remove_item":
                    changes[("inventory", "remove", json.dumps(op.get("item"), sort_keys=True, default=str))] = (
                        "remove_item",
                        op.get("item"),
                    )

        # 无操作日志：相对共同祖先做字段级 diff
        for key in set(base_snapshot["player"]) | set(tip_snapshot["player"]):
            old_value = base_snapshot["player"].get(key)
            new_value = tip_snapshot["player"].get(key)
            if old_value != new_value:
                if key not in tip_snapshot["player"]:
                    changes[("player", key)] = ("remove", None)
                else:
                    changes[("player", key)] = ("set", new_value)
        base_items = [json.dumps(item, sort_keys=True, default=str) for item in base_snapshot["inventory"]]
        tip_items = [json.dumps(item, sort_keys=True, default=str) for item in tip_snapshot["inventory"]]
        for raw_item in tip_items:
            if raw_item not in base_items:
                changes[("inventory", "add", raw_item)] = ("add_item", json.loads(raw_item))
        for raw_item in base_items:
            if raw_item not in tip_items:
                changes[("inventory", "remove", raw_item)] = ("remove_item", json.loads(raw_item))
        return changes

    def merge_cloud(self, local: SaveData, cloud: SaveData) -> SaveData:
        base = local.base or cloud.base
        if base is not None and not isinstance(base, SaveData):
            base = None

        if base is None:
            # 没有共同祖先：也不简单后写覆盖——player 字段按本地优先 union，
            # inventory 取并集，进度互不丢失
            player = dict(cloud.player)
            player.update(local.player)
            inventory = list(local.inventory)
            for item in cloud.inventory:
                marker = json.dumps(item, sort_keys=True, default=str)
                if marker not in [json.dumps(x, sort_keys=True, default=str) for x in inventory]:
                    inventory.append(item)
            merged_ops = list(local.ops) + [
                op for op in cloud.ops if op not in local.ops
            ]
            return SaveData(
                version=self.CURRENT_VERSION,
                player=player,
                inventory=inventory,
                timestamp=max(local.timestamp, cloud.timestamp),
                ops=merged_ops,
                base=None,
            )

        base_snapshot = {"player": dict(base.player), "inventory": list(base.inventory)}
        local_snapshot = {"player": dict(local.player), "inventory": list(local.inventory)}
        cloud_snapshot = {"player": dict(cloud.player), "inventory": list(cloud.inventory)}

        local_changes = self._changes(base_snapshot, local_snapshot, local.ops)
        cloud_changes = self._changes(base_snapshot, cloud_snapshot, cloud.ops)

        merged_snapshot = {
            "player": dict(base_snapshot["player"]),
            "inventory": list(base_snapshot["inventory"]),
        }

        def apply_change(path, change):
            action, value = change
            if path[0] == "player":
                if action == "remove":
                    merged_snapshot["player"].pop(path[1], None)
                else:
                    merged_snapshot["player"][path[1]] = value
            else:
                marker = json.dumps(value, sort_keys=True, default=str)
                current = [json.dumps(x, sort_keys=True, default=str) for x in merged_snapshot["inventory"]]
                if action == "add_item" and marker not in current:
                    merged_snapshot["inventory"].append(value)
                elif action == "remove_item" and marker in current:
                    merged_snapshot["inventory"] = [
                        item for item in merged_snapshot["inventory"]
                        if json.dumps(item, sort_keys=True, default=str) != marker
                    ]

        all_paths = set(local_changes) | set(cloud_changes)
        for path in all_paths:
            local_change = local_changes.get(path)
            cloud_change = cloud_changes.get(path)
            if local_change is None:
                apply_change(path, cloud_change)
            elif cloud_change is None:
                apply_change(path, local_change)
            elif local_change == cloud_change:
                apply_change(path, local_change)
            else:
                # 真冲突：按时间戳新者胜，但双方独占字段早已各自保留
                winner = local_change if local.timestamp >= cloud.timestamp else cloud_change
                apply_change(path, winner)

        merged_ops = list(local.ops) + [op for op in cloud.ops if op not in local.ops]
        return SaveData(
            version=self.CURRENT_VERSION,
            player=merged_snapshot["player"],
            inventory=merged_snapshot["inventory"],
            timestamp=max(local.timestamp, cloud.timestamp),
            ops=merged_ops,
            base=base,
        )

    # ------------------------------------------------------------------
    # 删除：文件 + 内存缓存 + 墓碑
    # ------------------------------------------------------------------
    def delete(self, path: str):
        if os.path.exists(path):
            os.remove(path)
        self.cache.pop(path, None)
        self._tombstones.add(path)

    def get_cached(self, path: str):
        if path in self._tombstones:
            return None
        return self.cache.get(path)
