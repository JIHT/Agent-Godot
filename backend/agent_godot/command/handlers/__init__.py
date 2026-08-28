"""command/handlers：命令处理器集合。

内置命令在 builtin.py，模块被 import 即通过 @register_command 完成登记
（与 M04 @register_tool / M13 @register 同一套路：注册表模式第四次落地）。
"""
from .builtin import (cmd_checkpoint, cmd_compact, cmd_help, cmd_model,
                      cmd_plan, cmd_rewind, cmd_skills)

__all__ = ["cmd_checkpoint", "cmd_compact", "cmd_help", "cmd_model",
           "cmd_plan", "cmd_rewind", "cmd_skills"]
