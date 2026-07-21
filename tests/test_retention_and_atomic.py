"""
数据保留 + 原子写 单元测试。

覆盖 P0 第三项「隐私/合规」+ 第四项的「原子写」核心契约：
- ConfigManager.get_retention_days: 默认值、用户配置、非法值兜底
- HistoryRepository.save_analysis_result: 原子写（临时文件 + os.replace），
  写入失败不留半截文件
- HistoryRepository._atomic_write_json: 中途异常会清理临时文件
- HistoryRepository.delete_old_history: 按 keep_days 删除过期日期 key
- LLMAnalyzer.cleanup_debug_data: 按 mtime 清理过期 debug dump
- HistoryManager.cleanup_old_summaries: 按索引清理过期 KV 摘要
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import tests.conftest  # noqa: F401  触发 mock 注入
from src.infrastructure.config.config_manager import ConfigManager
from src.infrastructure.persistence.history_manager import HistoryManager
from src.infrastructure.persistence.history_repository import HistoryRepository
from tests.conftest import AstrBotConfig


# ============================================================
# ConfigManager.get_retention_days
# ============================================================


class TestGetRetentionDays:
    def test_default_is_30(self):
        cm = ConfigManager(AstrBotConfig({"basic": {}}))
        assert cm.get_retention_days() == 30

    def test_reads_user_value(self):
        cm = ConfigManager(AstrBotConfig({"basic": {"retention_days": 90}}))
        assert cm.get_retention_days() == 90

    def test_zero_means_no_cleanup(self):
        """用户显式填 0 表示不清理。"""
        cm = ConfigManager(AstrBotConfig({"basic": {"retention_days": 0}}))
        assert cm.get_retention_days() == 0

    def test_negative_normalized_to_zero(self):
        cm = ConfigManager(AstrBotConfig({"basic": {"retention_days": -5}}))
        assert cm.get_retention_days() == 0

    def test_non_int_falls_back_to_default(self):
        """字符串/None 等非法值兜底默认 30。"""
        cm = ConfigManager(AstrBotConfig({"basic": {"retention_days": "一周"}}))
        assert cm.get_retention_days() == 30


# ============================================================
# HistoryRepository 原子写
# ============================================================


class TestAtomicWrite:
    def setup_method(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="history_test_"))
        self.repo = HistoryRepository(str(self.tmp))

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_writes_valid_json(self):
        """正常保存：目标文件是合法 JSON，内容正确。"""
        ok = self.repo.save_analysis_result(
            "g1", {"statistics": {"msg_count": 10}}, date_str="2024-01-01"
        )
        assert ok is True
        target = self.tmp / "history" / "group_g1.json"
        assert target.exists()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["daily"]["2024-01-01"]["statistics"]["msg_count"] == 10
        assert "last_updated" in data

    def test_no_temp_files_left_after_success(self):
        """成功写入后，目录里不应残留 .tmp 临时文件。"""
        self.repo.save_analysis_result("g2", {"x": 1}, date_str="2024-01-01")
        temps = list((self.tmp / "history").glob(".*.tmp"))
        assert temps == [], f"残留临时文件: {temps}"

    def test_write_failure_does_not_corrupt_existing(self):
        """写入失败时，原有文件应保持完整（原子写的核心契约）。

        模拟方式：先写入 v1，然后让 _atomic_write_json 在 os.replace 前抛异常，
        验证 v1 仍可读。
        """
        # 先写入 v1
        self.repo.save_analysis_result(
            "g3", {"version": 1}, date_str="2024-01-01"
        )
        target = self.tmp / "history" / "group_g3.json"

        # 模拟 os.replace 失败（monkey-patch）
        import src.infrastructure.persistence.history_repository as mod

        original_replace = mod.os.replace

        def fail_replace(src, dst):
            raise OSError("simulated replace failure")

        mod.os.replace = fail_replace
        try:
            # 应当抛异常（save_analysis_result 内部 except 会捕获并返回 False）
            ok = self.repo.save_analysis_result(
                "g3", {"version": 2}, date_str="2024-01-02"
            )
            assert ok is False
        finally:
            mod.os.replace = original_replace

        # 原文件未被破坏，v1 仍可读
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["daily"]["2024-01-01"]["version"] == 1
        assert "2024-01-02" not in data["daily"]
        # 临时文件也被清掉了
        temps = list((self.tmp / "history").glob(".*.tmp"))
        assert temps == []

    def test_load_after_atomic_save(self):
        """保存后能立即 load 回来。"""
        self.repo.save_analysis_result(
            "g4", {"stats": {"a": 1}}, date_str="2024-02-02"
        )
        loaded = self.repo.load_group_history("g4")
        assert loaded["daily"]["2024-02-02"]["stats"]["a"] == 1


# ============================================================
# HistoryRepository.delete_old_history
# ============================================================


class TestDeleteOldHistory:
    def setup_method(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="history_cleanup_"))
        self.repo = HistoryRepository(str(self.tmp))

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_deletes_old_entries_keeps_recent(self):
        """超过 keep_days 的日期被删，未超过的保留。"""
        # 写两条：一条很老，一条今天
        self.repo.save_analysis_result("g1", {"v": "old"}, date_str="2020-01-01")
        self.repo.save_analysis_result("g1", {"v": "new"}, date_str="2099-01-01")
        # 2099 是未来日期，cutoff 是 now-30d，所以 2099 不算过期
        deleted = self.repo.delete_old_history("g1", keep_days=30)
        assert deleted == 1
        remaining = self.repo.load_group_history("g1")["daily"]
        assert "2020-01-01" not in remaining
        assert "2099-01-01" in remaining

    def test_no_entries_to_delete_returns_zero(self):
        """全是近期数据时返回 0，不写文件。"""
        self.repo.save_analysis_result("g1", {"v": "new"}, date_str="2099-01-01")
        deleted = self.repo.delete_old_history("g1", keep_days=30)
        assert deleted == 0


# ============================================================
# LLMAnalyzer.cleanup_debug_data
# ============================================================


class TestCleanupDebugData:
    def test_cleanup_removes_old_json_keeps_recent(self, monkeypatch, tmp_path):
        """超过 max_age_days 的 .json 被清，未超过的保留；非 .json 不动。"""
        from src.infrastructure.analysis.llm_analyzer import LLMAnalyzer

        debug_dir = tmp_path / "debug_data"
        debug_dir.mkdir()

        # 模拟 StarTools.get_data_dir 返回我们控制的目录
        class FakeStarTools:
            @staticmethod
            def get_data_dir(_name=None):
                return tmp_path

        # astrbot.api.star.StarTools 在 conftest mock 里
        import astrbot.api.star as star_mod

        monkeypatch.setattr(star_mod, "StarTools", FakeStarTools)

        # 创建 3 个文件：1 个老的 .json、1 个新的 .json、1 个老的 .txt
        old_json = debug_dir / "old.json"
        old_json.write_text("[]")
        # 把 mtime 改成 10 天前
        old_time = time.time() - 10 * 86400
        os.utime(old_json, (old_time, old_time))

        new_json = debug_dir / "new.json"
        new_json.write_text("[]")

        old_txt = debug_dir / "old.txt"
        old_txt.write_text("keep me")
        os.utime(old_txt, (old_time, old_time))

        analyzer = LLMAnalyzer.__new__(LLMAnalyzer)  # 跳过 __init__
        deleted = analyzer.cleanup_debug_data(max_age_days=7)

        assert deleted == 1
        assert not old_json.exists()
        assert new_json.exists()
        assert old_txt.exists()  # 非 .json 不动

    def test_cleanup_no_dir_returns_zero(self, monkeypatch, tmp_path):
        """目录不存在时返回 0，不抛异常。"""
        from src.infrastructure.analysis.llm_analyzer import LLMAnalyzer

        import astrbot.api.star as star_mod

        class FakeStarTools:
            @staticmethod
            def get_data_dir(_name=None):
                return tmp_path / "nonexistent"

        monkeypatch.setattr(star_mod, "StarTools", FakeStarTools)

        analyzer = LLMAnalyzer.__new__(LLMAnalyzer)
        assert analyzer.cleanup_debug_data(max_age_days=7) == 0


# ============================================================
# HistoryManager.cleanup_old_summaries
# ============================================================


class TestCleanupOldSummaries:
    @pytest.mark.asyncio
    async def test_zero_keep_days_does_nothing(self):
        """keep_days<=0 视为不清理，立即返回 0。"""
        plugin = MagicMock()
        plugin.delete_kv_data = AsyncMock()
        plugin.get_kv_data = AsyncMock(return_value=[])
        hm = HistoryManager(plugin)
        deleted = await hm.cleanup_old_summaries("g1", keep_days=0)
        assert deleted == 0
        plugin.delete_kv_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_expired_entries_via_index(self):
        """按索引中 ts 清理过期 KV key。"""
        now_ts = time.time()
        old_ts = now_ts - 100 * 86400  # 100 天前
        plugin = MagicMock()
        plugin.get_kv_data = AsyncMock(
            return_value=[
                {"key": "analysis_g1_old", "ts": old_ts},
                {"key": "analysis_g1_new", "ts": now_ts},
            ]
        )
        plugin.delete_kv_data = AsyncMock()
        plugin.put_kv_data = AsyncMock()
        hm = HistoryManager(plugin)

        deleted = await hm.cleanup_old_summaries("g1", keep_days=30)
        assert deleted == 1
        # 只删了 old key
        plugin.delete_kv_data.assert_called_once_with("analysis_g1_old")
        # 索引被收缩（只剩 new）
        plugin.put_kv_data.assert_called_once()
        saved_key, saved_index = plugin.put_kv_data.call_args.args
        assert saved_key == "history_index_g1"
        assert len(saved_index) == 1
        assert saved_index[0]["key"] == "analysis_g1_new"

    @pytest.mark.asyncio
    async def test_no_index_does_nothing(self):
        """群没有索引（没存过摘要）时返回 0，不抛异常。"""
        plugin = MagicMock()
        plugin.get_kv_data = AsyncMock(return_value=None)
        hm = HistoryManager(plugin)
        deleted = await hm.cleanup_old_summaries("never_seen", keep_days=30)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_failure_keeps_index_entry(self):
        """某条 KV 删除失败时，索引项保留（下次再试），不影响其他条目。"""
        now_ts = time.time()
        old_ts = now_ts - 100 * 86400
        plugin = MagicMock()
        plugin.get_kv_data = AsyncMock(
            return_value=[
                {"key": "fail_key", "ts": old_ts},
                {"key": "ok_key", "ts": old_ts},
            ]
        )

        call_count = {"n": 0}

        async def flaky_delete(key):
            call_count["n"] += 1
            if key == "fail_key":
                raise RuntimeError("simulated")

        plugin.delete_kv_data = flaky_delete
        plugin.put_kv_data = AsyncMock()
        hm = HistoryManager(plugin)

        deleted = await hm.cleanup_old_summaries("g1", keep_days=30)
        # ok_key 删成功，fail_key 失败
        assert deleted == 1
        # 索引保留了 fail_key
        saved_key, saved_index = plugin.put_kv_data.call_args.args
        assert len(saved_index) == 1
        assert saved_index[0]["key"] == "fail_key"
