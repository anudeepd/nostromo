import pytest

from xwing.config import Settings, UserConfig, UserPerms


def test_audit_db_uses_environment_override(monkeypatch, tmp_path):
    audit_db = tmp_path / "audit" / "xwing.db"
    monkeypatch.setenv("XWING_AUDIT_DB", str(audit_db))

    settings = Settings(root_dir=tmp_path)

    assert settings.audit_db == audit_db


def test_admin_users_are_loaded_separately_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XWING_ADMIN_USERS", " Alice,OPS ")
    settings = Settings(root_dir=tmp_path)
    assert settings.admin_users == ["alice", "ops"]
    assert settings.is_admin_user("ALICE")


class TestUserConfigCompact:
    def test_rwd(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice: rwd\n")
        cfg = UserConfig(f)
        assert cfg.get("alice") == UserPerms(read=True, write=True, delete=True)

    def test_admin_compact_permission_is_rejected(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice: rwda\n")
        with pytest.raises(ValueError, match="only 'r', 'w', 'd'"):
            UserConfig(f)

    def test_admin_verbose_permission_is_rejected(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice:\n    read: true\n    admin: true\n")
        with pytest.raises(ValueError, match="configured separately"):
            UserConfig(f)

    def test_rw_no_delete(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  bob: rw\n")
        cfg = UserConfig(f)
        assert cfg.get("bob") == UserPerms(read=True, write=True, delete=False)

    def test_r_only(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  charlie: r\n")
        cfg = UserConfig(f)
        assert cfg.get("charlie") == UserPerms(read=True, write=False, delete=False)

    def test_empty_string_denies_all(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text('users:\n  ghost: ""\n')
        cfg = UserConfig(f)
        assert cfg.get("ghost") == UserPerms(read=False, write=False, delete=False)

    def test_unknown_chars_raise(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice: rxyz\n")
        with pytest.raises(ValueError, match="only 'r', 'w', 'd'"):
            UserConfig(f)

    def test_case_insensitive_lookup(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  Alice: rwd\n")
        cfg = UserConfig(f)
        assert cfg.get("alice") == UserPerms(read=True, write=True, delete=True)
        assert cfg.get("ALICE") == UserPerms(read=True, write=True, delete=True)


class TestUserConfigVerbose:
    def test_full_verbose(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text(
            "users:\n  alice:\n    read: true\n    write: true\n    delete: true\n"
        )
        cfg = UserConfig(f)
        assert cfg.get("alice") == UserPerms(read=True, write=True, delete=True)

    def test_verbose_defaults(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice:\n    write: true\n")
        cfg = UserConfig(f)
        # read defaults true, delete defaults false
        assert cfg.get("alice") == UserPerms(read=True, write=True, delete=False)


class TestUserConfigFallback:
    def test_wildcard_applies_to_unlisted(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text('users:\n  alice: rwd\n  "*": r\n')
        cfg = UserConfig(f)
        assert cfg.get("bob") == UserPerms(read=True, write=False, delete=False)

    def test_no_wildcard_denies_unlisted(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice: rwd\n")
        cfg = UserConfig(f)
        assert cfg.get("unknown") == UserPerms(read=False, write=False, delete=False)

    def test_explicit_entry_takes_precedence_over_wildcard(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text('users:\n  alice: rwd\n  "*": r\n')
        cfg = UserConfig(f)
        assert cfg.get("alice") == UserPerms(read=True, write=True, delete=True)


class TestUserConfigErrors:
    def test_malformed_yaml_raises_value_error(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice: [unclosed\n")
        with pytest.raises(ValueError, match="Invalid YAML"):
            UserConfig(f)

    def test_missing_file_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            UserConfig(tmp_path / "nonexistent.yaml")

    def test_missing_users_key_raises_value_error(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("something_else:\n  alice: rwd\n")
        with pytest.raises(ValueError, match="no 'users' key"):
            UserConfig(f)

    def test_empty_file_raises_value_error(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("")
        with pytest.raises(ValueError, match="no 'users' key"):
            UserConfig(f)

    def test_invalid_compact_chars_raises_value_error(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice: read\n")
        with pytest.raises(ValueError, match="only 'r', 'w', 'd'"):
            UserConfig(f)

    def test_write_word_rejected(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  bob: write\n")
        with pytest.raises(ValueError, match="only 'r', 'w', 'd'"):
            UserConfig(f)

    def test_permission_entry_must_be_string_or_mapping(self, tmp_path):
        f = tmp_path / "u.yaml"
        f.write_text("users:\n  alice:\n    - read\n")
        with pytest.raises(ValueError, match="must be a string or mapping"):
            UserConfig(f)
