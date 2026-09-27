"""TesterAgent 后端包。

分层依赖方向单向：api → runtime/graph → adapters → store；
store、adapters 不得 import graph；reme_writer 仅被 api/kb.py import。
"""

__version__ = "0.1.0"
