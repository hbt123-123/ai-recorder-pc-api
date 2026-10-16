"""Transcription connectors 包。

本包刻意不急切导入连接器类：直接 ``import core.transcription.connectors.xxx``
不会连带加载其他连接器模块。注意 ``import core.transcription`` 顶层仍会经
registry 的模块级单例触发内置连接器加载（openai/numpy/soundfile/httpx 等
重依赖随之就绪）——惰性收益仅针对"绕过包入口、按需引用单个连接器模块"
的场景（如 pc_api worker 只导入 dashscope_realtime_stream）。
"""
