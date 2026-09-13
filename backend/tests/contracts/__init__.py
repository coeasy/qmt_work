"""Tier-0 契约基线（Phase 1）。

本包存放从**真实代码内省**生成的契约快照（JSON），以及内省实现（introspect）：
- 快照由 ``scripts/gen_contracts.py`` 生成/更新；
- ``tests/test_contracts_baseline.py`` 用快照做回归：基线 ⊆ 现网，静默删除/改名 = 0。
"""
