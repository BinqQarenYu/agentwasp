import pytest
import tempfile
from pathlib import Path
import importlib.util

_path = Path(__file__).resolve().parent.parent / "src" / "skills" / "builtin" / "file_ops.py"
_spec = importlib.util.spec_from_file_location("src.skills.builtin.file_ops", _path)
_mod = importlib.util.module_from_spec(_spec)
_mod.__package__ = "src.skills.builtin"
_spec.loader.exec_module(_mod)

ReadFileSkill = _mod.ReadFileSkill
WriteFileSkill = _mod.WriteFileSkill
EditFileSkill = _mod.EditFileSkill
ListDirSkill = _mod.ListDirSkill
SearchFilesSkill = _mod.SearchFilesSkill

@pytest.mark.asyncio
async def test_file_ops_workbench_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "workbench_test.txt"

        # 1. Write file
        writer = WriteFileSkill()
        res_write = await writer.execute(
            path=str(test_file),
            content="line 1: Alpha\nline 2: Beta\nline 3: Gamma\n",
        )
        assert res_write.success is True
        assert test_file.exists()

        # 2. Prevent accidental overwrite without overwrite=True
        res_fail_overwrite = await writer.execute(
            path=str(test_file),
            content="overwritten",
        )
        assert res_fail_overwrite.success is False
        assert "already exists" in res_fail_overwrite.error

        # 3. Read file with line numbering
        reader = ReadFileSkill()
        res_read = await reader.execute(path=str(test_file), start_line=1, max_lines=2)
        assert res_read.success is True
        assert "1: line 1: Alpha" in res_read.output
        assert "2: line 2: Beta" in res_read.output
        assert "Gamma" not in res_read.output  # sliced out

        # 4. Surgical edit
        editor = EditFileSkill()
        res_edit = await editor.execute(
            path=str(test_file),
            target="Beta",
            replacement="Delta",
        )
        assert res_edit.success is True
        assert "replaced 1 occurrence" in res_edit.output

        # Verify edited content
        res_read_after_edit = await reader.execute(path=str(test_file))
        assert "Delta" in res_read_after_edit.output
        assert "Beta" not in res_read_after_edit.output

        # 5. List dir
        lister = ListDirSkill()
        res_list = await lister.execute(path=tmpdir)
        assert res_list.success is True
        assert "workbench_test.txt" in res_list.output

        # 6. Search files
        searcher = SearchFilesSkill()
        res_search = await searcher.execute(path=tmpdir, pattern=r"Delta")
        assert res_search.success is True
        assert "workbench_test.txt" in res_search.output
        assert "Delta" in res_search.output
