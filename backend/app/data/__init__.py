"""Phase 4 统一数据访问层（本地 canonical + 在线 CapabilityChain）。

本包把「选股/研究取数」与具体 Provider 解耦：调用方只描述「要哪些代码、哪个周期、
什么复权、什么策略」，由 BarsProvider / UniverseProvider / FundamentalsProvider 决定
走本地仓还是按能力链在线取数。**引擎与业务代码内不得出现任何 provider 名字**（D-J §J.1）。
"""
