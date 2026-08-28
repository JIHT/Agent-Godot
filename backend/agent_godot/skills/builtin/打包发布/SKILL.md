---
name: 打包发布
triggers: [打包, 导出, 发布, export, build, 安装包]
tools_needed: [godot_check, godot_run_tests, godot_project_overview]
version: 2
---
# Godot 打包发布检查清单

导出一次失败的成本远高于照清单走一遍：**导出模板缺失 / 资源 remap / 版本号没改**
三大坑占了失败原因的八成。按序执行，每步都要有证据（命令输出或文件内容），不要凭印象跳过。

## 1. 导出前置检查
- `export_presets.cfg` 存在，且 preset 名与目标平台严格匹配（大小写敏感）
- `project.godot` 里 `config/name`、`config/version` 与本次发布一致
- 图标：`config/icon` 指向的文件存在，Windows 平台建议 ≥256×256 的 .ico
- 主场景：`config/run/main_scene` 指向的场景文件存在且能被 `godot_read_scene` 解析

## 2. 导出前验证（先跑通再导出）
1. `godot_check`（L1 语法）——零错误才继续
2. `godot_run_tests`（L3 累积）——有测试就跑，失败不进下一步
3. C# 项目：先 `dotnet build`，Godot 不会替你编译 C#

## 3. headless 导出
```bash
# Windows Desktop（-headless 不打开编辑器窗口，CI/服务器必备）
godot --headless --export-release "Windows Desktop" build/game.exe
# 校验产物确实落地（导出"成功"但产物为零字节是常见静默失败）
ls -l build/
```

## 4. 常见坑（逐个对照）
- **导出模板未安装**：报错 `Export templates not found` → 编辑器里 编辑器→管理导出模板 下载
- **资源 remap**：`.import/` 目录没提交，换机器后贴图全丢 → 提交 `.import/` 与 `*.import`
- **路径大小写**：Windows 开发/Linux 导出，`res://Scenes/Player.tscn` 大小写不一致直接资源丢失
- **导出过滤**：`export_presets.cfg` 里 `include_filter`/`exclude_filter` 把 `.gd` 一起排除了
- **产物被占用**：目标 exe 正在运行 → 导出静默失败，先关掉进程

## 5. 收尾
- 记录本次版本号与产物路径（后续 /rewind 或问题回溯要用）
- 产物清单与体积写入会话摘要，方便用户核对
