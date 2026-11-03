"""每案一个干净的 SQLite 库，并把所有 service 显式指向它（spec §6）。

三个坑必须在这里一次性堵住：
1. `InterviewService()` / `InterviewDatabase()` / `InterviewerFinder()` 的默认参数都是
   `sqlite:///data/interview_agent.db`——任何一个漏传就会写进仓库真实库。
2. `SessionManager` 构造即 `create_all`，所以「只是构造一下」也算碰库。
3. `interviewer_busy_periods` 是模块级全局 dict，且被两个模块 from-import 绑定。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from evals.schema import BEIJING_TZ, parse_local_time

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REAL_DB = REPO_ROOT / "data" / "interview_agent.db"
EXPECTED_INTERVIEWER_COUNT = 10


def sqlite_url_for(db_file: Path) -> str:
    """把临时文件路径转成 SQLAlchemy 的 sqlite URL（Windows 反斜杠要转正斜杠）。"""
    return "sqlite:///" + Path(db_file).resolve().as_posix()


def file_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    """mtime + sha256，用来证明真实库没有被写。"""
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    stat = path.stat()
    return {"path": str(path), "sha256": digest.hexdigest(), "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


@dataclass
class Workspace:
    """一个 case 的全部依赖，显式携带 db_path，无任何默认回退。"""

    case_id: str
    db_dir: Path
    db_url: str
    interviewer_service: Any
    interview_service: Any
    behavior_service: Any
    name_to_id: Dict[str, int]

    def interviewer_id(self, name: str) -> int:
        try:
            return self.name_to_id[name]
        except KeyError as exc:
            raise ValueError(f"未知面试官：{name!r}；可用：{sorted(self.name_to_id)}") from exc

    def close(self) -> None:
        for service, attr in (
            (self.interviewer_service, "db"),
            (self.interview_service, "db_router"),
            (self.behavior_service, "db_router"),
        ):
            router = getattr(service, attr, None)
            close = getattr(router, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _clear_busy_caches() -> None:
    """清掉两个模块对同一个全局 dict 的引用（spec §6）。"""
    from config import constants

    constants.interviewer_busy_periods.clear()
    try:
        from agents.interview import interview_database

        interview_database.interviewer_busy_periods.clear()
    except Exception:
        pass


def create_workspace(case_id: str, root: Path, pre_conditions: Optional[Dict[str, Any]] = None) -> Workspace:
    """建立 case 专属目录与数据库，seed 10 位默认面试官 + 造忙时段，返回 name→id 映射。"""
    from services.candidate_behavior_service import CandidateBehaviorService
    from services.interview_service import InterviewService
    from services.interviewer_service import InterviewerService

    _clear_busy_caches()

    db_dir = Path(root) / case_id
    db_dir.mkdir(parents=True, exist_ok=True)
    db_url = sqlite_url_for(db_dir / "eval.db")

    interviewer_service = InterviewerService(db_path=db_url)
    if not interviewer_service.initialize_default_interviewers():
        raise RuntimeError(f"{case_id}: 默认面试官初始化失败")
    interview_service = InterviewService(db_path=db_url)
    behavior_service = CandidateBehaviorService(db_path=db_url)

    interviewers = interviewer_service.get_all_interviewers()
    if len(interviewers) != EXPECTED_INTERVIEWER_COUNT:
        raise RuntimeError(
            f"{case_id}: seed 后应有 {EXPECTED_INTERVIEWER_COUNT} 位面试官，实际 {len(interviewers)}"
        )
    name_to_id = {item["name"]: item["id"] for item in interviewers}

    workspace = Workspace(
        case_id=case_id,
        db_dir=db_dir,
        db_url=db_url,
        interviewer_service=interviewer_service,
        interview_service=interview_service,
        behavior_service=behavior_service,
        name_to_id=name_to_id,
    )
    # 直接走 repository：InterviewerService 没有透出 add_schedule
    for busy in (pre_conditions or {}).get("busy") or []:
        schedule_busy(workspace, busy["interviewer"], busy["start"], int(busy["duration_minutes"]))

    return workspace


def destroy_workspace(workspace: Workspace) -> None:
    """关闭连接、清理全局缓存、删除临时库目录。"""
    workspace.close()
    _clear_busy_caches()
    shutil.rmtree(workspace.db_dir, ignore_errors=True)


def schedule_busy(workspace: Workspace, interviewer: str, start: str, duration_minutes: int) -> int:
    """在运行期造一条忙时段（供用例行动列表里的前置占用使用）。"""
    from datetime import timedelta

    begin = parse_local_time(start)
    return workspace.interviewer_service.db.interviewers.add_schedule(
        workspace.interviewer_id(interviewer), begin, begin + timedelta(minutes=duration_minutes), "busy"
    )


__all__ = [
    "BEIJING_TZ",
    "DEFAULT_REAL_DB",
    "REPO_ROOT",
    "Workspace",
    "create_workspace",
    "destroy_workspace",
    "file_fingerprint",
    "schedule_busy",
    "sqlite_url_for",
]
