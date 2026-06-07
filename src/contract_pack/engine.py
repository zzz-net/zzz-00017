"""执行引擎 - 复制文件、生成 zip、执行回滚、按历史批次重跑"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import AppConfig, PackageConfig
from .manifest import ManifestEntry, group_by_package
from .precheck import PrecheckResult, run_precheck
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

    def run(
        self,
        entries: List[ManifestEntry],
        precheck: PrecheckResult,
        make_zip: bool = False,
        parent_batch_id: Optional[str] = None,
        rerun_params: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> EngineResult:
        """执行批次: 先复制，再按需打包 zip。失败路径不落已完成状态。"""
        batch = self.storage.create_batch(
            operator=self.config.operator,
            config_summary=self.config.summary(),
            parent_batch_id=parent_batch_id,
            rerun_params=rerun_params,
        )
        self.storage.update_batch_status(batch.id, BATCH_STATUS["RUNNING"])

        copied = 0
        failed = 0
        result = EngineResult(batch_id=batch.id, status=BATCH_STATUS["FAILED"])

        if not precheck.ok and not force:
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


@dataclass
class RerunResult:
    batch_id: str
    status: str
    parent_batch_id: str
    error: str = ""
    copied_files: int = 0
    zipped_files: int = 0
    failed_files: int = 0
    precheck: Optional[PrecheckResult] = None


class RerunError(Exception):
    """重跑过程中的错误"""

    def __init__(self, message: str, issues: Optional[List] = None):
        super().__init__(message)
        self.issues = issues or []


def rebuild_config_from_batch(
    batch: Batch,
    base_dir: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    source_root: Optional[Path] = None,
    output_root: Optional[Path] = None,
    allow_overwrite: bool = False,
) -> AppConfig:
    """从历史批次的 config_summary 重建 AppConfig，可选择性替换关键字段。

    参数替换优先级：显式传入 > 原批次 config_summary。
    output_root 替换时，所有包的 output_dir 和 zip_output 都会被重写到新根下。
    """
    summary = batch.config_summary or {}
    if base_dir is None:
        base_dir = Path.cwd()

    packages_data = summary.get("packages", [])
    if not packages_data:
        raise RerunError("历史批次 config_summary 缺少 packages 字段，无法重建配置")

    new_packages: List[PackageConfig] = []
    for pd in packages_data:
        pkg_name = pd.get("name")
        if not pkg_name:
            raise RerunError(f"packages 中存在缺少 name 字段的条目: {pd}")
        if "output_dir" not in pd:
            raise RerunError(f"包 '{pkg_name}' 缺少 output_dir 字段")

        if output_root is not None:
            orig_out = Path(pd["output_dir"])
            orig_zip = Path(pd["zip_output"]) if pd.get("zip_output") else None
            new_out = output_root / orig_out.name
            new_zip = output_root / orig_zip.name if orig_zip else None
        else:
            new_out = Path(pd["output_dir"])
            new_zip = Path(pd["zip_output"]) if pd.get("zip_output") else None

        if not new_out.is_absolute():
            new_out = (base_dir / new_out).resolve()
        if new_zip and not new_zip.is_absolute():
            new_zip = (base_dir / new_zip).resolve()

        new_packages.append(
            PackageConfig(
                name=pkg_name,
                output_dir=new_out,
                zip_output=new_zip,
                file_mapping=pd.get("file_mapping", {}),
                version=pd.get("version"),
            )
        )

    if manifest_path is None:
        orig_manifest = summary.get("manifest_path")
        if not orig_manifest:
            raise RerunError("历史批次 config_summary 缺少 manifest_path 字段，且未指定新的 --manifest")
        manifest_path = Path(orig_manifest)
    if not manifest_path.is_absolute():
        manifest_path = (base_dir / manifest_path).resolve()

    if source_root is None:
        orig_src = summary.get("source_root")
        if not orig_src:
            raise RerunError("历史批次 config_summary 缺少 source_root 字段，且未指定新的 --source-root")
        source_root = Path(orig_src)
    if not source_root.is_absolute():
        source_root = (base_dir / source_root).resolve()

    db_path = summary.get("db_path", ".contract_pack.db")
    db_path_p = Path(db_path)
    if not db_path_p.is_absolute():
        db_path_p = (base_dir / db_path_p).resolve()

    operator = summary.get("operator") or "unknown"

    return AppConfig(
        manifest_path=manifest_path,
        source_root=source_root,
        packages=new_packages,
        operator=operator,
        db_path=db_path_p,
        allow_overwrite=allow_overwrite,
    )


def rebuild_manifest_from_batch(batch: Batch) -> List[ManifestEntry]:
    """从历史批次的 file_actions（COPY 动作）重建 ManifestEntry 列表。

    规则：
      - 只看 action=copy 的 FileAction（zip 是派生物，不在清单里）
      - category 取 fa.category，跳过 __zip__ 等内部分类
      - source_path: 取 fa.source_path（如果能相对 source_root 则用相对路径）
      - target_name: 取 Path(fa.target_path).name
      - version / description: 原批次不存储这些细节，留空
    """
    entries: List[ManifestEntry] = []
    for fa in batch.file_actions:
        if fa.action != FILE_ACTION["COPY"]:
            continue
        if fa.category == "__zip__":
            continue
        tgt_name = Path(fa.target_path).name
        entries.append(
            ManifestEntry(
                package=fa.package,
                category=fa.category,
                source_path=fa.source_path,
                target_name=tgt_name,
                version=None,
                description="",
                raw={"_rerun_from_batch": batch.id, "_fa_id": fa.id},
            )
        )
    return entries


def rerun_batch(
    parent_batch_id: str,
    storage: BatchStorage,
    base_dir: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    source_root: Optional[Path] = None,
    output_root: Optional[Path] = None,
    make_zip: bool = False,
    force: bool = False,
) -> RerunResult:
    """按历史批次重跑：
    1. 读取原批次，校验状态为 completed/partial
    2. 重建配置（可替换 manifest/source_root/output_root）
    3. 若未指定新 manifest，则从原批次 file_actions 重建
    4. 执行 dry-run 级别的预检
    5. 预检通过（或 --force）后创建新批次，记录 parent_batch_id 和 rerun_params
    6. 执行复制/压包
    """
    if base_dir is None:
        base_dir = Path.cwd()

    parent = storage.get_batch(parent_batch_id)
    if not parent:
        raise RerunError(f"父批次不存在: {parent_batch_id}")

    allowed_statuses = {BATCH_STATUS["COMPLETED"], BATCH_STATUS["PARTIAL"]}
    if parent.status not in allowed_statuses:
        raise RerunError(
            f"父批次状态为 '{parent.status}'，只能重跑 completed 或 partial 状态的批次"
        )

    rerun_params: Dict[str, Any] = {
        "manifest_path": str(manifest_path) if manifest_path else None,
        "source_root": str(source_root) if source_root else None,
        "output_root": str(output_root) if output_root else None,
        "make_zip": make_zip,
        "force": force,
    }

    new_cfg = rebuild_config_from_batch(
        parent,
        base_dir=base_dir,
        manifest_path=manifest_path,
        source_root=source_root,
        output_root=output_root,
        allow_overwrite=False,
    )

    if manifest_path is not None and manifest_path.exists():
        from .manifest import load_manifest
        entries = load_manifest(manifest_path)
    else:
        entries = rebuild_manifest_from_batch(parent)
        if not entries:
            raise RerunError("原批次没有可用的 COPY 文件动作，且未指定新的 manifest")

    new_storage = BatchStorage(new_cfg.db_path)
    precheck = run_precheck(new_cfg, entries, storage=new_storage, last_batch_id=parent_batch_id)

    if not precheck.ok and not force:
        err = f"重跑预检失败: 存在 {len(precheck.errors)} 个错误"
        dummy = new_storage.create_batch(
            operator=new_cfg.operator,
            config_summary=new_cfg.summary(),
            parent_batch_id=parent_batch_id,
            rerun_params=rerun_params,
        )
        new_storage.update_batch_status(dummy.id, BATCH_STATUS["FAILED"], error=err, finished=True)
        return RerunResult(
            batch_id=dummy.id,
            status=BATCH_STATUS["FAILED"],
            parent_batch_id=parent_batch_id,
            error=err,
            precheck=precheck,
        )

    engine = Engine(new_cfg)
    exec_result = engine.run(
        entries,
        precheck,
        make_zip=make_zip,
        parent_batch_id=parent_batch_id,
        rerun_params=rerun_params,
        force=force,
    )

    return RerunResult(
        batch_id=exec_result.batch_id,
        status=exec_result.status,
        parent_batch_id=parent_batch_id,
        error=exec_result.error,
        copied_files=exec_result.copied_files,
        zipped_files=exec_result.zipped_files,
        failed_files=exec_result.failed_files,
        precheck=precheck,
    )
