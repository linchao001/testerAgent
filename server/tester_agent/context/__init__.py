"""L4 上下文层（tech-design §2）：精准策略 + 窗口组装。

子模块分两类：

1. **叶子模块**（models / store / policy / budget / assembler / scopes / …）：
   三分区存放与 LLM 调用窗口组装（见 context-management 设计）。只允许依赖
   domain / prompts / errors / logging / 第三方库；禁止 import runtime / graph /
   memory / adapters / store。
2. **retrieval/**：召回→裁剪→注入管线（可依赖 adapters / store / runtime.TaskContext）；
   由 L3 能力节点 / playground / eval 调用，不挂在 graph 拓扑内。
"""
