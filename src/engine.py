"""向后兼容入口：实现已迁移到 save/serializer.py。"""
from save.serializer import SaveData, SaveSystem

__all__ = ["SaveData", "SaveSystem"]
