"""执行引擎 - 复制文件、生成 zip、执行回滚"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import AppConfig
from .manifest import ManifestEntry, group_by_package
from .precheck import PrecheckResult
from .storage import (
    BATCH_STATUS,
    FILE_ACTION,
    FILE_STATUS,
    Batch,
    BatchStorage,
    FileAction,
)


@dataclass
class EngineResult:
    batch_id: str
    status: str
    error: str = ""
    copied_files: int = 0
    zipped_files: int = 0
    failed_files: int = 0


class Engine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.storage = BatchStorage(config.db_path)

    def run(self, entries: List[ManifestEntry], precheck: PrecheckResult, make_zip: bool = False) -> EngineResult:
        """执行批次: 先复制，再按需打包 zip。失败路径不落已完成状态。"""
        batch = self.storage.create_batch(
            operator=self.config.operator,
            config_summary=self.config.summary(),
        )
        self.storage.update_batch_status(batch.id, BATCH_STATUS["RUNNING"])

        copied = 0
        failed = 0
        result = EngineResult(batch_id=batch.id, status=BATCH_STATUS["FAILED"])

        if not precheck.ok:
            err = f"预检失败: 存在 {len(precheck.errors)} 个错误"
            self.storage.update_batch_status(batch.id, BATCH_STATUS["FAILED"], error=err, finished=True)
            result.error = err
            return result

        try:
            grouped = group_by_package(entries)
            pkg_configs = {p.name: p for p in self.config.packages}

            for pkg_name, plan in precheck.plan.items():
                pkg_cfg = pkg_configs.get(pkg_name)
                if not pkg_cfg:
                    continue

                output_dir = pkg_cfg.output_dir
                output_dir.mkdir(parents=True, exist_ok=True)

                for entry, src, tgt in plan:
                    fa_id = self.storage.add_file_action(
                        batch_id=batch.id,
                        package=pkg_name,
                        action=FILE_ACTION["COPY"],
                        source_path=str(src),
                        target_path=str(tgt),
                        category=entry.category,
                        status=FILE_STATUS["PENDING"],
                    )
                    try:
                        if not src.exists():
                            raise FileNotFoundError(f"源文件不存在: {src}")
                        tgt.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, tgt)
                        self.storage.update_file_action(fa_id, FILE_STATUS["SUCCESS"])
                        copied += 1
                    except Exception as e:
                        self.storage.update_file_action(fa_id, FILE_STATUS["FAILED"], error=str(e))
                        failed += 1

            if make_zip:
                for pkg in self.config.packages:
                    if not pkg.zip_output:
                        continue
                    output_dir = pkg.output_dir
                    if not output_dir.exists():
                        continue
                    fa_id = self.storage.add_file_action(
                        batch_id=batch.id,
                        package=pkg.name,
                        action=FILE_ACTION["ZIP"],
                        source_path=str(output_dir),
                        target_path=str(pkg.zip_output),
                        category="__zip__",
                        status=FILE_STATUS["PENDING"],
                    )
                    try:
                        pkg.zip_output.parent.mkdir(parents=True, exist_ok=True)
                        with zipfile.ZipFile(pkg.zip_output, "w", zipfile.ZIP_DEFLATED) as zf:
                            for f in output_dir.rglob("*"):
                                if f.is_file():
                                    arcname = f.relative_to(output_dir)
                                    zf.write(f, arcname)
                        self.storage.update_file_action(fa_id, FILE_STATUS["SUCCESS"])
                        result.zipped_files += 1
                    except Exception as e:
                        self.storage.update_file_action(fa_id, FILE_STATUS["FAILED"], error=str(e))
                        failed += 1

            if failed == 0:
                final_status = BATCH_STATUS["COMPLETED"]
            elif copied > 0:
                final_status = BATCH_STATUS["PARTIAL"]
            else:
                final_status = BATCH_STATUS["FAILED"]

            self.storage.update_batch_status(batch.id, final_status, finished=True)
            result.status = final_status
            result.copied_files = copied
            result.failed_files = failed

        except Exception as e:
            self.storage.update_batch_status(batch.id, BATCH_STATUS["FAILED"], error=str(e), finished=True)
            result.error = str(e)

        return result

    def rollback(self, batch_id: str) -> Tuple[bool, str]:
        """回滚指定批次: 只回滚 SUCCESS 动作。若目标路径已被其他文件占用则停止并提示。"""
        batch = self.storage.get_batch(batch_id)
        if not batch:
            return False, f"批次不存在: {batch_id}"

        if batch.status in (BATCH_STATUS["ROLLED_BACK"],):
            return True, "批次已回滚"

        self.storage.update_batch_status(batch.id, BATCH_STATUS["RUNNING"], error="rollback in progress")

        try:
            for fa in reversed(batch.file_actions):
                if fa.status != FILE_STATUS["SUCCESS"]:
                    continue

                if fa.action == FILE_ACTION["COPY"]:
                    tgt = Path(fa.target_path)
                    if tgt.exists():
                        if tgt.is_dir():
                            msg = f"回滚终止: 目标路径是目录且被占用，无法安全删除: {tgt}"
                            self.storage.update_batch_status(batch.id, BATCH_STATUS["ROLLBACK_FAILED"], error=msg, finished=True)
                            return False, msg
                        try:
                            tgt.unlink()
                            self.storage.update_file_action(fa.id, FILE_STATUS["ROLLED_BACK"])
                        except Exception as e:
                            msg = f"回滚终止: 删除目标文件失败 ({tgt}): {e}"
                            self.storage.update_batch_status(batch.id, BATCH_STATUS["ROLLBACK_FAILED"], error=msg, finished=True)
                            return False, msg
                    else:
                        self.storage.update_file_action(fa.id, FILE_STATUS["ROLLED_BACK"])

                elif fa.action == FILE_ACTION["ZIP"]:
                    zp = Path(fa.target_path)
                    if zp.exists():
                        try:
                            zp.unlink()
                            self.storage.update_file_action(fa.id, FILE_STATUS["ROLLED_BACK"])
                        except Exception as e:
                            msg = f"回滚终止: 删除 zip 失败 ({zp}): {e}"
                            self.storage.update_batch_status(batch.id, BATCH_STATUS["ROLLBACK_FAILED"], error=msg, finished=True)
                            return False, msg
                    else:
                        self.storage.update_file_action(fa.id, FILE_STATUS["ROLLED_BACK"])

            for pkg_cfg in self.config.packages:
                out = pkg_cfg.output_dir
                if out.exists() and out.is_dir():
                    try:
                        if not any(out.iterdir()):
                            out.rmdir()
                    except OSError:
                        pass

            self.storage.update_batch_status(batch.id, BATCH_STATUS["ROLLED_BACK"], finished=True)
            return True, "回滚成功"

        except Exception as e:
            msg = f"回滚异常: {e}"
            self.storage.update_batch_status(batch.id, BATCH_STATUS["ROLLBACK_FAILED"], error=msg, finished=True)
            return False, msg
