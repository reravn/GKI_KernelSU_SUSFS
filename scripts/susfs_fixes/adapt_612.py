#!/usr/bin/env python3
"""按目标源码形态适配 Android 16 / 6.12 的 SUSFS 主补丁。

参考 WildKernels/GKI_KernelSU_SUSFS 的 PR #342：只调整补丁上下文，
不把旧内核的 VMA 统计流程改成新接口；应用补丁后补齐旧版统计守卫。
"""

import re
import sys
import tempfile
from pathlib import Path

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@([^\n]*)\n")
GATHER_CALL = re.compile(r"(?m)^(\t+)smap_gather_stats\(vma, &mss, (0|last_vma_end)\);$")
GUARD_MARKER = "/* SUSFS 6.12 旧版 rollup 统计兼容守卫。 */"
GUARD_CONDITION = "if (!vma->vm_file || !(SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))))"


def rollup_bounds(source):
    """限定在 rollup 函数中修改，避免影响其他统计路径。"""
    start = source.index("static int show_smaps_rollup(")
    end = source.index("\nstatic const struct seq_operations proc_pid_smaps_op", start)
    return start, end


def guard_old_rollup(source):
    """只跳过隐藏映射的统计调用，保留迭代、锁操作和范围更新。"""
    start, end = rollup_bounds(source)
    rollup = source[start:end]
    markers = rollup.count(GUARD_MARKER)
    if markers:
        if markers != 3 or rollup.count(GUARD_CONDITION) != 3:
            raise ValueError("旧版 rollup 统计守卫只完成了一部分")
        return source

    calls = GATHER_CALL.findall(rollup)
    offsets = [offset for _, offset in calls]
    if len(calls) != 3 or offsets.count("0") != 2 or offsets.count("last_vma_end") != 1:
        raise ValueError("旧版 rollup 统计调用已变化，拒绝自动添加守卫")

    def guarded(match):
        indent, offset = match.groups()
        return (
            f"{indent}{GUARD_MARKER}\n"
            "#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
            f"{indent}{GUARD_CONDITION}\n"
            "#endif\n"
            f"{indent}\tsmap_gather_stats(vma, &mss, {offset});"
        )

    return source[:start] + GATHER_CALL.sub(guarded, rollup) + source[end:]


def edit_context_line(hunk, line, after=None):
    """增删一行纯上下文，并同步调整 hunk 的新旧行数。"""
    header = HUNK_HEADER.match(hunk)
    if header is None:
        raise ValueError("exec.c include hunk 格式已变化")
    old_line, old_count, new_line, new_count, context = header.groups()
    old_count, new_count = int(old_count or 1), int(new_count or 1)
    body = hunk[header.end():]
    if after is None:
        if body.count(line) != 1 or old_count < 1 or new_count < 1:
            raise ValueError("exec.c include 上下文或行数无效")
        body, delta = body.replace(line, "", 1), -1
    else:
        if body.count(after) != 1 or line in body:
            raise ValueError("exec.c include 插入位置不唯一")
        body, delta = body.replace(after, after + line, 1), 1
    return (
        f"@@ -{old_line},{old_count + delta} +{new_line},{new_count + delta} @@{context}\n"
        + body
    )


def adapt_patch(patch, exec_source, mmu_source):
    """保留已经匹配的新版或固定旧版补丁，仅适配已知的新版 hunk。"""
    sections = re.split(r"(?=^diff --git )", patch, flags=re.M)
    needs_guard = False
    for index, section in enumerate(sections):
        if section.startswith("diff --git a/fs/exec.c "):
            context_line = " #include <linux/dma-buf.h>\n"
            hunks = re.split(r"(?=^@@ )", section, flags=re.M)
            has_include = re.search(r"^#include <linux/dma-buf.h>$", exec_source, re.M)
            if not has_include and context_line in section:
                if len(hunks) < 2 or section.count(context_line) != 1:
                    raise ValueError("exec.c dma-buf.h 上下文不唯一")
                hunks[1] = edit_context_line(hunks[1], context_line)
            elif has_include and context_line not in section and len(hunks) >= 2:
                # 固定旧版补丁没有这一行；只补上下文，不删除源码已有的头文件。
                anchor = " #include <linux/ksm.h>\n"
                source_anchor = "#include <linux/ksm.h>\n#include <linux/dma-buf.h>\n"
                if source_anchor in exec_source and anchor in hunks[1]:
                    hunks[1] = edit_context_line(hunks[1], context_line, after=anchor)
            sections[index] = "".join(hunks)

        elif section.startswith("diff --git a/fs/proc/task_mmu.c "):
            old_context = " \tif (!vma_data_pages(vma))\n"
            if "if (!vma_pages(vma))" in mmu_source and old_context in section:
                if section.count(old_context) != 1:
                    raise ValueError("show_smap 的 VMA 上下文不唯一")
                section = section.replace(old_context, " \tif (!vma_pages(vma))\n", 1)

            start, end = rollup_bounds(mmu_source)
            old_rollup = "\tvma_start = vma->vm_start;\n\tdo {" in mmu_source[start:end]
            if old_rollup:
                hunks = re.split(r"(?=^@@ )", section, flags=re.M)
                rollup_hunks = [
                    hunk for hunk in hunks[1:]
                    if "show_smaps_rollup" in hunk.split("\n", 1)[0]
                    and ("+\t\t\tgoto bypass_orig_flow;\n" in hunk
                         or "\n+bypass_orig_flow:\n" in hunk)
                ]
                new_contexts = (
                    " \t\tif (vma->vm_start < last_vma_end) {\n",
                    " \t\t\tsmap_gather_stats(priv, vma, &mss);\n",
                )
                if any(context in hunk for context in new_contexts for hunk in rollup_hunks):
                    if len(rollup_hunks) != 2 or any(
                        sum(context in hunk for hunk in rollup_hunks) != 1
                        for context in new_contexts
                    ):
                        raise ValueError("新版 rollup 的两个 SUS_MAP hunk 已变化")
                    # 在改写补丁前验证补偿路径，避免应用后才发现无法补齐隐藏功能。
                    guard_old_rollup(mmu_source)
                    section = "".join(hunk for hunk in hunks if hunk not in rollup_hunks)
                    needs_guard = True
            sections[index] = section

    return "".join(sections), needs_guard


def write_changed(path, original, updated):
    """内容有变化时才原子替换，保留原文件权限。"""
    if original == updated:
        return False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=path.name + ".susfs-",
            dir=path.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(updated)
        temporary.chmod(path.stat().st_mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def main(argv):
    try:
        if len(argv) == 3 and argv[0] == "prepare":
            patch_file, tree = Path(argv[1]), Path(argv[2])
            original = patch_file.read_text()
            updated, needs_guard = adapt_patch(
                original,
                (tree / "fs/exec.c").read_text(),
                (tree / "fs/proc/task_mmu.c").read_text(),
            )
            if write_changed(patch_file, original, updated):
                print("6.12 SUSFS 补丁已按实际源码适配", file=sys.stderr)
            print(int(needs_guard))
        elif len(argv) == 2 and argv[0] == "finish":
            source_file = Path(argv[1]) / "fs/proc/task_mmu.c"
            original = source_file.read_text()
            if write_changed(source_file, original, guard_old_rollup(original)):
                print("已补齐旧版 rollup 的三处 SUS_MAP 统计守卫")
            else:
                print("旧版 rollup 统计守卫已存在，无需重复添加")
        else:
            raise ValueError("用法：adapt_612.py prepare <补丁> <源码目录> 或 finish <源码目录>")
    except (OSError, ValueError) as error:
        print(f"::error::6.12 SUSFS 补丁适配失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
