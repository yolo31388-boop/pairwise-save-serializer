"""存档序列化 - 带7个bug"""
import json
import os
import tempfile
from dataclasses import dataclass, field

@dataclass
class SaveData:
    version: int
    player: dict
    inventory: list
    timestamp: float

class SaveSystem:
    CURRENT_VERSION = 3

    def __init__(self):
        self.migrations = {1: self._migrate_1_to_2, 2: self._migrate_2_to_3}
        self.cache: dict = {}

    def serialize(self, data: SaveData) -> bytes:
        # bug2: 字典键顺序不固定
        d = {"version": data.version, "player": data.player, "inventory": data.inventory, "timestamp": data.timestamp}
        return json.dumps(d).encode()

    def deserialize(self, raw: bytes) -> SaveData:
        d = json.loads(raw.decode())
        # bug1: 不做版本迁移
        return SaveData(**d)

    def _migrate_1_to_2(self, d):
        d["player"]["hp"] = d["player"].get("health", 100)
        return d

    def _migrate_2_to_3(self, d):
        d["inventory"] = [{"id": i, "count": 1} for i in d.get("inventory", [])]
        return d

    def save(self, path: str, data: SaveData):
        # bug5: 非原子写入
        with open(path, "wb") as f:
            f.write(self.serialize(data))

    def load(self, path: str) -> SaveData:
        with open(path, "rb") as f:
            return self.deserialize(f.read())

    def merge_cloud(self, local: SaveData, cloud: SaveData) -> SaveData:
        # bug6: 简单后写覆盖
        if cloud.timestamp > local.timestamp:
            return cloud
        return local

    def delete(self, path: str):
        # bug7: 删文件不清缓存
        if os.path.exists(path):
            os.remove(path)

    def get_cached(self, path: str):
        return self.cache.get(path)
