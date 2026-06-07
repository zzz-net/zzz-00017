"""执行引擎 - 复制文件、生成 zip、执行回滚"""

from __future__ import annotations

import hashlib
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


def _compute_fingerprint(path: Path) -> Tuple[str, int]:
    """计算文件 SHA1 哈希和字节大小。用于回滚时确认文件仍为批次产物。"""
    size = path.stat().st_size
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest(), size


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
                        file_hash, file_size = _compute_fingerprint(tgt)
                        self.storage.update_file_action(fa_id, FILE_STATUS["SUCCESS"], file_hash=file_hash, file_size=file_size)
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
                        file_hash, file_size = _compute_fingerprint(pkg.zip_output)
                        self.storage.update_file_action(fa_id, FILE_STATUS["SUCCESS"], file_hash=file_hash, file_size=file_size)
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

    def _verify_and_delete(self, fa: FileAction) -> Tuple[bool, str]:
        """校验目标文件指纹并删除。返回 (是否成功, 错误信息)。
        成功时已删除；失败时需要调用方决定是否继续。"""
        tgt = Path(fa.target_path)

        if not tgt.exists():
            return True, ""

        if tgt.is_dir():
            label = "zip" if fa.action == FILE_ACTION["ZIP"] else "文件"
            return False, f"目标路径是目录，无法安全删除: {tgt}"

        if fa.file_hash is None and fa.file_size is None:
            return False, (
                f"缺少文件指纹（批次产物，无法确认是否为本批次生成，拒绝删除以防误删: {tgt}"
            )

        try:
            cur_hash, cur_size = _compute_fingerprint(tgt)
        except Exception as e:
            return False, f"读取目标文件失败: {tgt}: {e}"

        if fa.file_size is not None and cur_size != fa.file_size:
            return False, (
                f"文件大小不匹配（疑似被替换），拒绝删除: {tgt} "
                f"(期望 {fa.file_size} 字节, 当前 {cur_size} 字节)"
            )

        if fa.file_hash is not None and cur_hash != fa.file_hash:
            return False, (
                f"文件内容不匹配（疑似被替换），拒绝删除: {tgt} "
                f"(期望 sha1={fa.file_hash[:12]}..., 当前 {cur_hash[:12]}...)"
            )

        try:
            tgt.unlink()
        except Exception as e:
            return False, f"删除目标文件失败 ({tgt}): {e}"

        return True, ""

    def rollback(self, batch_id: str) -> Tuple[bool, str]:
        """回滚指定批次: 只回滚 SUCCESS 且指纹匹配的文件动作。
        遇到内容不匹配、目录占用、zip 被替换等占用情况，立即停止并提示。"""
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

                ok, err = self._verify_and_delete(fa)
                if not ok:
                    action_label = "zip" if fa.action == FILE_ACTION["ZIP"] else "copy"
                    msg = f"回滚终止 (动作={action_label}, 目标={fa.target_path}: {err}"
                    self.storage.update_batch_status(batch.id, BATCH_STATUS["ROLLBACK_FAILED"], error=msg, finished=True)
                    return False, msg
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
