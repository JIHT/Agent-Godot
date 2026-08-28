---
name: 本地化
triggers: [本地化, 翻译, 多语言, i18n, localization, 中文化]
tools_needed: [read_file, write_file, search_files, godot_project_overview]
version: 1
---
# Godot 本地化（i18n）落地清单

本地化最容易做错的地方是**中途接入**：几百处硬编码字符串散落在 .gd 与 .tscn 里，
事后改代价极高。所以第一步永远是"先改写法，再补翻译"。

## 1. 配置翻译资源（project.godot）
```ini
[internationalization]
locale/translations=PackedStringArray("res://locales/messages.zh_CN.translation")
```
- 翻译文件放 `locales/`，命名 `<域>.<语言代码>.translation`
- 语言代码用 Godot 规范（`zh_CN` / `en` / `ja`），不是 `zh-CN`

## 2. 代码里的写法（先统一，再翻译）
- 用户可见文本一律 `tr("KEY")`；开发者看的日志用普通字符串
- 带参数：`tr_n("有 %d 条消息", "有 %d 条消息", n) % n`（复数形态）
- .tscn 里的文本属性：在编辑器里标记为"可翻译"（属性旁的翻译图标），
  否则改语言不生效

## 3. 抽取与回填
```bash
# 从场景与脚本里抽全部 tr() 键，生成 CSV（Godot 4.x）
godot --headless --script tools/extract_translations.gd
# CSV 交翻译 → 回来后用编辑器导入成 .translation（或 --headless 导入）
```
手工流程（无抽取脚本时）：
1. `search_files` 搜 `tr("` 列出全部键
2. 按 `key,en,zh_CN` 三列整理成 CSV
3. 导入生成 `.translation`，路径写进 project.godot

## 4. 运行时切换与测试
```gdscript
TranslationServer.set_locale("zh_CN")     # 立即切换，已实例化节点需手动刷新
```
- 切换后界面文本不会自动更新 → 重新加载场景，或让 UI 监听 `TranslationServer`
  的语言变更信号后重设文本
- 测试：项目设置 → 常规 → 本地化里改测试语言，不用真改系统语言

## 5. 常见坑
- **字体缺字形**：中文不显示 = 字体没含 CJK 字形，换 Noto Sans SC 之类并调 `fallback`
- **文本溢出**：中译英长度差 2~3 倍 → UI 容器必须自适应（`size_flags` + `expand`）
- **硬编码漏网**：字符串拼接（`"生命: " + str(hp)`）抽不出来 → 一律改成带参 `tr`
- **CSV 编码**：必须 UTF-8，Excel 另存会带 BOM → Godot 读入后首键名乱码
- **键名语义化**：用 `MSG_LOW_HP` 而不是 `STR_001`，否则三个月后没人敢改

## 验收
- 切到目标语言后，主流程（菜单 → 设置 → HUD）无残留源语言文本
- 数字/日期/货币走本地化格式，不是硬编码拼接
