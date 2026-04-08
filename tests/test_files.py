import pytest

from xwing.files import human_size, is_editable, safe_path


class TestSafePath:
    def test_valid_subpath(self, tmp_path):
        result = safe_path(tmp_path, "subdir/file.txt")
        assert result == tmp_path / "subdir" / "file.txt"

    def test_empty_path_returns_root(self, tmp_path):
        assert safe_path(tmp_path, "") == tmp_path

    def test_leading_slash_stripped(self, tmp_path):
        assert safe_path(tmp_path, "/file.txt") == tmp_path / "file.txt"

    def test_traversal_rejected(self, tmp_path):
        with pytest.raises(PermissionError):
            safe_path(tmp_path, "../../etc/passwd")

    def test_traversal_with_leading_slash_rejected(self, tmp_path):
        with pytest.raises(PermissionError):
            safe_path(tmp_path, "/../../../etc/passwd")

    def test_double_dot_in_middle_rejected(self, tmp_path):
        with pytest.raises(PermissionError):
            safe_path(tmp_path, "subdir/../../etc/passwd")

    def test_double_slash_prefix_stays_within_root(self, tmp_path):
        # //etc stripped to etc — resolves within root, not to /etc
        result = safe_path(tmp_path, "//etc")
        assert result == tmp_path / "etc"
        assert str(result).startswith(str(tmp_path))

    def test_symlink_outside_root_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        with pytest.raises(PermissionError):
            safe_path(tmp_path, "link.txt")


class TestHumanSize:
    def test_bytes(self):
        assert human_size(0) == "0 B"
        assert human_size(500) == "500 B"
        assert human_size(1023) == "1023 B"

    def test_kilobytes(self):
        assert human_size(1024) == "1.0 KB"
        assert human_size(1536) == "1.5 KB"

    def test_megabytes(self):
        assert human_size(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self):
        assert human_size(1024**3) == "1.0 GB"


class TestIsEditable:
    def test_python_file(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("print('hi')")
        assert is_editable(f)

    def test_markdown_file(self, tmp_path):
        f = tmp_path / "README.md"
        f.write_text("# hello")
        assert is_editable(f)

    def test_env_file_not_editable(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("SECRET=hunter2")
        assert not is_editable(f)

    def test_binary_extension_not_editable(self, tmp_path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n")
        assert not is_editable(f)

    def test_oversized_text_file_not_editable(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        assert not is_editable(f)

    def test_extensionless_small_file_editable(self, tmp_path):
        f = tmp_path / "Makefile"
        f.write_text("all:\n\techo hi")
        assert is_editable(f)

    def test_extensionless_oversized_file_not_editable(self, tmp_path):
        f = tmp_path / "Makefile"
        f.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        assert not is_editable(f)
