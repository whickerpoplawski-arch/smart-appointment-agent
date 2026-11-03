"""
API 层时间序列化工具

数据库中的时间以北京时间的 naive datetime 保存，API 统一输出为「YYYY-MM-DD HH:MM」
字符串，避免前端拿到带时区偏移的 ISO 串后再做二次转换。
"""

from datetime import datetime
from typing import Optional

# 面试记录与日程的默认展示格式
DATETIME_FORMAT = "%Y-%m-%d %H:%M"
TIME_FORMAT = "%H:%M"
DATE_FORMAT = "%Y-%m-%d"


def format_datetime(value, fmt: str = DATETIME_FORMAT) -> Optional[str]:
    """把 datetime 或字符串格式化为展示时间，空值返回 None"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return str(value)


def format_time(value) -> Optional[str]:
    """只保留时分，用于日程时间轴"""
    return format_datetime(value, TIME_FORMAT)


def parse_date(value: str) -> datetime:
    """
    解析查询参数中的日期（YYYY-MM-DD）

    返回 naive datetime：SQLite 中保存的是不带时区的时间，带时区的值在字符串
    比较时会失配，因此这里统一剥离时区。
    """
    try:
        return datetime.strptime(value.strip(), DATE_FORMAT)
    except (AttributeError, ValueError):
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到：{value!r}")
