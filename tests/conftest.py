"""pytest 全局 fixture。

安全底线：任何测试都不允许读写项目根目录的真实 config.json，也不允许发起真实的
第三方网络请求。isolated_config 这个 fixture 把 config.CONFIG_PATH 猴子补丁到
一个临时文件，config.py 里的函数都是运行时才读这个模块级全局名字，补丁之后
立刻生效，测试全程只碰 tmp_path。
"""

from __future__ import annotations

import pytest

from app import config as config_store


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把 config_store.CONFIG_PATH 指向临时文件，隔离真实配置。"""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", path)
    return path
