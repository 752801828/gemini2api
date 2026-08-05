"""原子化落盘工具（VULN-010）。

写「同目录临时文件 + os.replace」覆盖目标，避免进程在写入中途崩溃/断电导致
凭据文件（accounts/cookies/api-keys）被截断成半截 JSON 而损坏。
os.replace 在同一文件系统上是原子操作，故临时文件必须与目标同目录。
"""

import json
import errno
import os
import tempfile
from pathlib import Path
from typing import Any, Union


def atomic_write_text(path: Union[str, Path], data: str, encoding: str = "utf-8") -> None:
    """原子写文本：同目录写 .tmp（fsync 落盘）后 os.replace 覆盖目标。失败不留残缺目标文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, target)
        except OSError as e:
            # Docker Desktop cannot replace a file that is itself a bind-mount
            # (EBUSY). The fully flushed temp file still prevents us from
            # preparing partial JSON; copy it into the mounted inode as the
            # only supported persistence path for this deployment shape.
            if e.errno != errno.EBUSY:
                raise
            with target.open("w", encoding=encoding) as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.unlink(tmp)
        # rename 本身（目录项更新）在多数 POSIX 文件系统上要 fsync 父目录后才保证
        # 崩溃后持久；否则断电可能丢失这次 rename，目标回退到旧版本。补 fsync 父目录。
        # （Windows 无 O_DIRECTORY，整体 try/except 兜底为 no-op，不破坏默认部署。）
        try:
            dfd = os.open(str(target.parent), os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except (OSError, AttributeError):
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Union[str, Path],
    obj: Any,
    *,
    indent: Union[int, None] = None,
    ensure_ascii: bool = False,
) -> None:
    """原子写 JSON（序列化参数与原生 json.dump 对齐，保持文件格式不变）。"""
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii))
