"""存档序列化系统"""
import copy
import json
import os
import tempfile
from dataclasses import dataclass


@dataclass
class SaveData:
    version: int
    player: dict
    inventory: list
    timestamp: float


class _Encoder:
    """把对象图编码为 JSON 可序列化结构。

    - 浮点数序列化为十进制字符串（repr 保证往返精度不丢失）
    - 字典/列表按遍历顺序分配引用 ID，重复出现的对象写 {"$ref": id}
    """

    def __init__(self):
        self._seen = {}

    def encode(self, obj):
        if isinstance(obj, float):
            return {"__float__": repr(obj)}
        if isinstance(obj, dict):
            oid = id(obj)
            if oid in self._seen:
                return {"__ref__": self._seen[oid]}
            self._seen[oid] = len(self._seen)
            return {k: self.encode(obj[k]) for k in sorted(obj)}
        if isinstance(obj, (list, tuple)):
            oid = id(obj)
            if oid in self._seen:
                return {"__ref__": self._seen[oid]}
            self._seen[oid] = len(self._seen)
            return [self.encode(item) for item in obj]
        return obj


class _Decoder:
    """_Encoder 的逆过程，按相同的遍历顺序注册容器以解析引用。"""

    def __init__(self):
        self._objects = []

    def decode(self, node):
        if isinstance(node, dict):
            if set(node) == {"__ref__"}:
                return self._objects[node["__ref__"]]
            if set(node) == {"__float__"}:
                return float(node["__float__"])
            result = {}
            self._objects.append(result)
            for key, value in node.items():
                result[key] = self.decode(value)
            return result
        if isinstance(node, list):
            result = []
            self._objects.append(result)
            for value in node:
                result.append(self.decode(value))
            return result
        return node


class SaveSystem:
    CURRENT_VERSION = 3

    def __init__(self):
        self.migrations = {1: self._migrate_1_to_2, 2: self._migrate_2_to_3}
        self.cache: dict = {}
        self.deleted: set = set()

    def serialize(self, data: SaveData) -> bytes:
        d = {
            "version": data.version,
            "player": data.player,
            "inventory": data.inventory,
            "timestamp": data.timestamp,
        }
        encoded = _Encoder().encode(d)
        # 键排序 + 固定分隔符，保证相同内容字节一致
        return json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def deserialize(self, raw: bytes) -> SaveData:
        d = _Decoder().decode(json.loads(raw.decode("utf-8")))
        d = self._migrate_to_latest(d)
        return SaveData(**d)

    def _migrate_to_latest(self, d: dict) -> dict:
        d = copy.deepcopy(d)
        version = d.get("version", 1)
        if version > self.CURRENT_VERSION:
            raise ValueError(f"存档版本 {version} 高于当前支持的版本 {self.CURRENT_VERSION}")
        while version < self.CURRENT_VERSION:
            migrate = self.migrations.get(version)
            if migrate is None:
                raise ValueError(f"缺少版本 {version} -> {version + 1} 的迁移函数")
            d = migrate(d)
            version += 1
            d["version"] = version
        return d

    def _migrate_1_to_2(self, d):
        d["player"]["hp"] = d["player"].get("health", 100)
        return d

    def _migrate_2_to_3(self, d):
        d["inventory"] = [
            i if isinstance(i, dict) else {"id": i, "count": 1}
            for i in d.get("inventory", [])
        ]
        return d

    def save(self, path: str, data: SaveData):
        # 临时文件 + 原子替换：写失败时旧文件保持完好
        raw = self.serialize(data)
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".save_tmp_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        self.deleted.discard(path)

    def load(self, path: str) -> SaveData:
        with open(path, "rb") as f:
            return self.deserialize(f.read())

    def merge_cloud(self, local: SaveData, cloud: SaveData, base: SaveData = None) -> SaveData:
        # 三方合并：字段级合并玩家数据，背包取并集，冲突按时间戳裁决
        merged_player = {}
        for key in set(local.player) | set(cloud.player):
            in_local = key in local.player
            in_cloud = key in cloud.player
            if in_local and not in_cloud:
                merged_player[key] = local.player[key]
            elif in_cloud and not in_local:
                merged_player[key] = cloud.player[key]
            elif local.player[key] == cloud.player[key]:
                merged_player[key] = local.player[key]
            elif base is not None and key in base.player:
                # 有共同祖先：谁相对 base 修改了就保留谁的值
                local_changed = local.player[key] != base.player[key]
                cloud_changed = cloud.player[key] != base.player[key]
                if local_changed and not cloud_changed:
                    merged_player[key] = local.player[key]
                elif cloud_changed and not local_changed:
                    merged_player[key] = cloud.player[key]
                else:
                    merged_player[key] = self._pick_by_timestamp(local, cloud, key)
            else:
                merged_player[key] = self._pick_by_timestamp(local, cloud, key)

        merged_inventory = list(local.inventory)
        for item in cloud.inventory:
            if item not in merged_inventory:
                merged_inventory.append(item)

        return SaveData(
            version=self.CURRENT_VERSION,
            player=merged_player,
            inventory=merged_inventory,
            timestamp=max(local.timestamp, cloud.timestamp),
        )

    @staticmethod
    def _pick_by_timestamp(local: SaveData, cloud: SaveData, key):
        if cloud.timestamp >= local.timestamp:
            return cloud.player[key]
        return local.player[key]

    def delete(self, path: str):
        # 删文件的同时清内存缓存，并标记为已删除
        if os.path.exists(path):
            os.remove(path)
        self.cache.pop(path, None)
        self.deleted.add(path)

    def get_cached(self, path: str):
        if path in self.deleted:
            return None
        return self.cache.get(path)
