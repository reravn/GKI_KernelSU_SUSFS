#!/usr/bin/env python3
"""验证 6.12 补丁适配不会改写原有统计流程或破坏旧版补丁。"""

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from adapt_612 import (
    GUARD_CONDITION,
    GUARD_MARKER,
    adapt_patch,
    guard_old_rollup,
    main,
)

EXEC_SOURCE = """#include <linux/rseq.h>
#include <linux/ksm.h>

#include <linux/uaccess.h>
#include <asm/mmu_context.h>
"""

MMU_SOURCE = """static int show_smap(struct seq_file *m, void *v)
{
	struct vm_area_struct *vma = v;
	struct mem_size_stats mss = {};

	if (!vma_pages(vma))
		goto show_pad;
}

static int show_smaps_rollup(struct seq_file *m, void *v)
{
	vma_start = vma->vm_start;
	do {
		smap_gather_stats(vma, &mss, 0);
		last_vma_end = vma->vm_end;
		if (condition_one) {
			smap_gather_stats(vma, &mss, 0);
		}
		if (condition_two) {
			smap_gather_stats(vma, &mss, last_vma_end);
			last_vma_end = vma->vm_end;
		}
	} while (vma);
	return 0;
}

static const struct seq_operations proc_pid_smaps_op = {};
"""

EXEC_PATCH = (
    "diff --git a/fs/exec.c b/fs/exec.c\n"
    "--- a/fs/exec.c\n"
    "+++ b/fs/exec.c\n"
    "@@ -1,6 +1,9 @@\n"
    " #include <linux/rseq.h>\n"
    " #include <linux/ksm.h>\n"
    " #include <linux/dma-buf.h>\n"
    "+#ifdef CONFIG_KSU_SUSFS\n"
    "+#include <linux/susfs_def.h>\n"
    "+#endif\n"
    " \n"
    " #include <linux/uaccess.h>\n"
    " #include <asm/mmu_context.h>\n"
)

MMU_PATCH = (
    "diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n"
    "--- a/fs/proc/task_mmu.c\n"
    "+++ b/fs/proc/task_mmu.c\n"
    "@@ -3,6 +3,13 @@ static int show_smap(struct seq_file *m, void *v)\n"
    " \tstruct vm_area_struct *vma = v;\n"
    " \tstruct mem_size_stats mss = {};\n"
    " \n"
    "+#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
    "+\tif (vma->vm_file) {\n"
    "+\t\tif (SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n"
    "+\t\t\treturn 0;\n"
    "+\t}\n"
    "+#endif\n"
    "+\n"
    " \tif (!vma_data_pages(vma))\n"
    " \t\tgoto show_pad;\n"
    " }\n"
    "@@ -20,6 +27,11 @@ static int show_smaps_rollup(struct seq_file *m, void *v)\n"
    " \t\t\tgoto out_unlock;\n"
    " \t\t}\n"
    " \n"
    "+#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
    "+\t\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n"
    "+\t\t\tgoto bypass_orig_flow;\n"
    "+#endif\n"
    "+\n"
    " \t\tif (vma->vm_start < last_vma_end) {\n"
    " \t\t\tstatement;\n"
    " \t\t}\n"
    "@@ -30,6 +42,9 @@ static int show_smaps_rollup(struct seq_file *m, void *v)\n"
    " \t\t\tstatement;\n"
    " \t\t\tsmap_gather_stats(priv, vma, &mss);\n"
    " \t\t}\n"
    "+#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
    "+bypass_orig_flow:\n"
    "+#endif\n"
    " \t\tlast_vma_end = vma->vm_end;\n"
    " \n"
    " \t\tstatement;\n"
)


class Adapt612Tests(unittest.TestCase):
    def test_old_source_adapts_context_and_requests_guards(self):
        adapted, needs_guard = adapt_patch(EXEC_PATCH + MMU_PATCH, EXEC_SOURCE, MMU_SOURCE)
        self.assertTrue(needs_guard)
        self.assertIn("@@ -1,5 +1,8 @@", adapted)
        self.assertNotIn(" #include <linux/dma-buf.h>\n", adapted)
        self.assertIn(" \tif (!vma_pages(vma))\n", adapted)
        self.assertNotIn("bypass_orig_flow", adapted)
        self.assertIn("+\t\t\treturn 0;", adapted)

    def test_existing_dma_include_is_preserved(self):
        source = EXEC_SOURCE.replace("#include <linux/ksm.h>\n", "#include <linux/ksm.h>\n#include <linux/dma-buf.h>\n")
        adapted, _ = adapt_patch(EXEC_PATCH + MMU_PATCH, source, MMU_SOURCE)
        self.assertTrue(adapted.startswith(EXEC_PATCH))

    def test_old_exec_patch_keeps_the_source_include(self):
        source = EXEC_SOURCE.replace("#include <linux/ksm.h>\n", "#include <linux/ksm.h>\n#include <linux/dma-buf.h>\n")
        patch = EXEC_PATCH.replace(" #include <linux/dma-buf.h>\n", "").replace("@@ -1,6 +1,9 @@", "@@ -1,5 +1,8 @@")
        adapted, needs_guard = adapt_patch(patch, source, MMU_SOURCE)
        self.assertEqual(adapted, EXEC_PATCH)
        self.assertFalse(needs_guard)

    def test_current_source_keeps_the_upstream_patch(self):
        source = MMU_SOURCE.replace("vma_pages(vma)", "vma_data_pages(vma)").replace("\tdo {", "\twhile (vma) {")
        exec_source = EXEC_SOURCE + "#include <linux/dma-buf.h>\n"
        patch = EXEC_PATCH + MMU_PATCH
        adapted, needs_guard = adapt_patch(patch, exec_source, source)
        self.assertEqual(adapted, patch)
        self.assertFalse(needs_guard)

    def test_fixed_old_patch_is_not_replaced(self):
        patch = MMU_PATCH.replace("vma_data_pages(vma)", "vma_pages(vma)")
        patch = patch.replace("if (vma->vm_start < last_vma_end) {", "smap_gather_stats(vma, &mss, 0);")
        patch = patch.replace("smap_gather_stats(priv, vma, &mss);", "smap_gather_stats(vma, &mss, last_vma_end);")
        adapted, needs_guard = adapt_patch(patch, EXEC_SOURCE, MMU_SOURCE)
        self.assertEqual(adapted, patch)
        self.assertFalse(needs_guard)

    def test_patch_adaptation_is_idempotent(self):
        adapted, _ = adapt_patch(EXEC_PATCH + MMU_PATCH, EXEC_SOURCE, MMU_SOURCE)
        repeated, _ = adapt_patch(adapted, EXEC_SOURCE, MMU_SOURCE)
        self.assertEqual(repeated, adapted)

    def test_guard_all_three_calls_without_changing_flow(self):
        guarded = guard_old_rollup(MMU_SOURCE)
        self.assertEqual(guarded.count(GUARD_MARKER), 3)
        self.assertEqual(guarded.count(GUARD_CONDITION), 3)
        self.assertEqual(guarded.count("smap_gather_stats(vma, &mss,"), 3)
        self.assertEqual(guarded.count("last_vma_end = vma->vm_end;"), 2)
        self.assertIn("\tdo {", guarded)
        self.assertIn("\t} while (vma);", guarded)
        self.assertEqual(guard_old_rollup(guarded), guarded)

    def test_partial_guards_are_rejected(self):
        source = MMU_SOURCE.replace("\tdo {", "\tdo {\n" + GUARD_MARKER)
        with self.assertRaises(ValueError):
            guard_old_rollup(source)

    def test_changed_gather_calls_fail_before_patch_rewrite(self):
        source = MMU_SOURCE.replace("smap_gather_stats(vma, &mss, last_vma_end);", "unexpected_call();")
        with self.assertRaises(ValueError):
            adapt_patch(EXEC_PATCH + MMU_PATCH, EXEC_SOURCE, source)

    def test_partial_upstream_rollup_hunks_are_rejected(self):
        patch = MMU_PATCH.replace("smap_gather_stats(priv, vma, &mss);", "unexpected_call();")
        with self.assertRaises(ValueError):
            adapt_patch(patch, EXEC_SOURCE, MMU_SOURCE)

    @unittest.skipUnless(shutil.which("gcc"), "统计守卫运行验证需要 gcc")
    def test_guard_runtime_with_feature_enabled_and_disabled(self):
        source = """struct inode { int hidden; };
struct file { struct inode *inode; };
struct vm_area_struct { unsigned long vm_start, vm_end; struct file *vm_file; };
struct seq_file { struct vm_area_struct *vma; };
struct mem_size_stats { int calls; };
struct seq_operations { int unused; };
#define file_inode(file) ((file)->inode)
#define SUSFS_IS_INODE_SUS_MAP(inode) ((inode)->hidden)
static void smap_gather_stats(struct vm_area_struct *vma, struct mem_size_stats *mss, unsigned long start)
{
    (void)vma;
    (void)start;
    mss->calls++;
}
static int show_smaps_rollup(struct seq_file *m, void *v)
{
	struct vm_area_struct *vma = m->vma;
	struct mem_size_stats mss = {};
	unsigned long vma_start, last_vma_end = 0;
	(void)v;
	vma_start = vma->vm_start;
	do {
		smap_gather_stats(vma, &mss, 0);
		last_vma_end = vma->vm_end;
		if (vma->vm_start < last_vma_end) {
			smap_gather_stats(vma, &mss, 0);
		}
		if (vma->vm_end == last_vma_end) {
			smap_gather_stats(vma, &mss, last_vma_end);
			last_vma_end = vma->vm_end;
		}
	} while (0);
	return mss.calls + ((vma_start == 7 && last_vma_end == 11) ? 0 : 1000);
}
static const struct seq_operations proc_pid_smaps_op = {};
int main(void)
{
    struct inode inode = {0};
    struct file file = {&inode};
    struct vm_area_struct vma = {7, 11, &file};
    struct seq_file seq = {&vma};
    (void)proc_pid_smaps_op;
    if (show_smaps_rollup(&seq, 0) != 3) return 1;
    inode.hidden = 1;
#ifdef CONFIG_KSU_SUSFS_SUS_MAP
    if (show_smaps_rollup(&seq, 0) != 0) return 2;
#else
    if (show_smaps_rollup(&seq, 0) != 3) return 2;
#endif
    vma.vm_file = 0;
    if (show_smaps_rollup(&seq, 0) != 3) return 3;
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / "guard.c"
            program.write_text(guard_old_rollup(source))
            for enabled in (False, True):
                binary = root / ("enabled" if enabled else "disabled")
                command = ["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror"]
                if enabled:
                    command.append("-DCONFIG_KSU_SUSFS_SUS_MAP")
                build = subprocess.run(command + [str(program), "-o", str(binary)], capture_output=True, text=True)
                self.assertEqual(build.returncode, 0, build.stderr)
                self.assertEqual(subprocess.run([str(binary)], check=False).returncode, 0)

    def test_cli_keeps_files_unchanged_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fs/proc").mkdir(parents=True)
            patch = root / "susfs.patch"
            patch.write_text(EXEC_PATCH + MMU_PATCH)
            (root / "fs/exec.c").write_text(EXEC_SOURCE)
            source = MMU_SOURCE.replace("smap_gather_stats(vma, &mss, last_vma_end);", "unexpected_call();")
            mmu = root / "fs/proc/task_mmu.c"
            mmu.write_text(source)
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["prepare", str(patch), str(root)]), 1)
            self.assertIn("6.12 SUSFS 补丁适配失败", errors.getvalue())
            self.assertEqual(patch.read_text(), EXEC_PATCH + MMU_PATCH)
            self.assertEqual(mmu.read_text(), source)


if __name__ == "__main__":
    unittest.main()
