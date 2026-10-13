"""lingxi 重写后的新核心包（strangler 做法：旧 core/ 继续跑，新代码全部放在 lx/ 里，最后一次性切换）。

铁规矩：
1. lx/ 里除 lx/adapters/ 之外，**任何模块都不能 import astrbot**；这样所有核心逻辑都能用标准库 unittest 直接测，不需要假模块。
2. lx/ 内部一律用相对导入（from .contracts import ...），测试里用 import lx.xxx。
3. 契约在 lx/contracts.py，只有协调者（苏灵卿）改；要改契约，写进汇报。
"""
