# M16 STT 与语音分析（多引擎 ASR · VAD · 口语诊断 · 实时对话）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 11 · 里程碑 MI-5「语音」 |
| 代码落点 | `backend/agent_godot/voice/`（**18 个文件 / 4250 行**）+ `config/voice.yaml`（115 行）+ `tests/test_voice/`（**8 个文件 / 1286 行**）—— 完整清单见 **§9.3**，落地状态见 **§9.8** |
| 交付状态 | ✅ **代码已全部落地**（2026-08）：**104 条单测全绿**；项目整体 **572 passed**。零 GPU / 零模型环境下可跑通全链路（MockBackend + 合成音频 + 能量 VAD）。中文默认引擎 **SenseVoice-Small** |
| 前置模块 | M02（ASR/TTS 服务接入复用网关韧性管道·**第四次复用**）· M04（transcribe/analyze 注册为工具）· M03（实时对话的推理环复用 Agent Loop）· M19（WS 实时端点）· M21（可观测埋点）· M22（评估框架复用）· M23（配置外置） |
| 手写比例 | 接入层与应用层 100% 手写；声学/声码模型（ASR/TTS）只用不写——**且必须是可替换件**（§9.4.1） |
| 教程映射 | 📘 zero2Agent 10 课 · 📝笔记 STT/语音 · faster-whisper 文档 · OpenAI Realtime API（对标形态）· **§10 已按 2026 对标扩充** |
| **修订记录** | **2026-08 §9 对标复核**：原方案把模块押在 whisper 上，而 whisper 中文 CER 5.14%（会议 18.87%）已被国产方案（0.57%~3.76%）拉开代差 → 抽象 `ASRBackend` 多引擎路由；另补 16 项缺口。**§1.1/§1.2/§4/§5/§7 中的相关部分已同步修订** |

---

## 0. 本模块在项目中的位置

**大白话**：给 Agent 装**耳朵和嘴巴**。三条产品线：①**语音输入**——对麦克风说"给玩家加双跳"，转文字进 Query Engine（M12）走原管线（语音只是另一个入口，后面全部复用）；②**口语诊断**——分析录音的语速/停顿/填充词产出报告（面试练习场景移植；Godot 侧用于录制旁白/台词质检）；③**实时语音对话**——全双工：边听你说、边用语音答你、可随时打断（对标 OpenAI Realtime/Gemini Live 的产品形态；自研编排、声学模型只用不写——工具调用必须走自家 Agent Loop，这是不直接托管 Realtime API 的原因，§1.6 详解）。①②的核心管线一句话：**先切后转再算**——VAD 切出"有人说话的段"（省算力+防幻觉）→ whisper 转写（附带词级时间戳）→ 从时间戳**算特征**（语速/停顿/填充词，零额外模型）→ LLM 综合诊断。③的管线一句话：**三环重叠 + 随时清场**——流式 ASR 没说完 LLM 先热身、LLM 没说完 TTS 先开口（重叠压延迟），用户一开口立即掐断播放（打断保对话权）。

**交付后状态**：`voice transcribe interview.wav` 出带时间戳转写（含说话人与情感标签）；`voice analyze interview.wav` 出诊断报告（语速 182 **字/分**、填充词"就是"×23、最长停顿 4.2s@08:31…）并导出 SRT 字幕与特征 JSON；聊天里语音消息自动转文字；`voice chat` 进实时对话——说"给玩家加双跳"，**1.0 秒内**听到语音应答且工具卡照常执行；Agent 说话中插话"改成三段跳"，300ms 内闭嘴并听懂新指令。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

> ⚠️ **本表已被 §9.3 修订**（2026-08 对标复核后由 8 个文件扩到 **16 个**）：新增 `preprocess / align / normalize / diarize / metrics / export / config/voice.yaml / tests`，并改造 `stt / stream_asr / tts / realtime`。**开工前以 §9.3 为准**，下表保留作"最小可跑版"参照。

**最小可跑版：8 个文件**

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `voice/__init__.py` | 空包 | — | 1 | 步骤 0 | — |
| 2 | `voice/vad.py` | 静音切割 | `vad_segments` | 40 | 步骤 1 | silero |
| 3 | `voice/stt.py` | **多引擎 ASR 抽象**（见 §9.4.1） | `ASRBackend`、`Transcriber` | 90→160 | 步骤 2 | qwen-asr / faster-whisper |
| 4 | `voice/features.py` | 时间戳→特征指标 | `extract_features`、`pauses`、`speech_rate` | 80 | 步骤 3 | stt |
| 5 | `lab/m16/` | 自录样本+人工标注（**补 CER 标注集**） | — | — | 步骤 1 前置 | 麦克风 |
| 6 | `voice/stream_asr.py` | 流式转写（**AlignAtt / LocalAgreement**） | `StreamingTranscriber`、`AlignAttPolicy`、`LocalAgreementPolicy` | 80→140 | 步骤 6 | stt |
| 7 | `voice/tts.py` | 流式合成（分句进、音频块出） | `Synthesizer`、`QwenTtsSynthesizer`、`EdgeTtsSynthesizer` | 90 | 步骤 7 | qwen-tts / edge-tts |
| 8 | `voice/realtime.py` | 全双工状态机（重叠+打断+**语义端点**） | `RealtimeSession`、`RealtimeState` | 130→180 | 步骤 8 | stream_asr+tts+M03 |

**完成后你拥有**：两个工具注册（TranscribeTool/AnalyzeSpeechTool）；诊断报告数值可回链；`voice chat` 实时对话（首音 <1.0s、打断静音 <300ms）。

---

## 1. 知识点详解（每节五段：定义 → 大白话 · 举例 · 演进 · 易错点）

### 1.1 语音转文字管线（VAD → ASR）

**① 严格定义**：**VAD**（Voice Activity Detection）先切"有人说话的段"再送 ASR——Silero-VAD 逐帧输出语音概率，按阈值+最短段长+最短间隔合并成段。**whisper 三合一输出**：转写文本＋**词/段级时间戳**（word_timestamps=True）＋语言检测——时间戳是特征工程的全部数据源。

**② 大白话**：**先分诊后化验**。把整段录音（含一半静音）直接扔给 whisper，就像把全血连血浆一起化验——又贵又容易被污染：whisper 对长静音会**幻觉出内容**（训练数据来自 YouTube 字幕，静音段它爱"脑补"片尾的"谢谢观看"——著名工程坑）。VAD 是分诊护士：把"确实在说话"的段挑出来，段前后各补 200ms 防切字，化验（ASR）又快又准。

**③ 举例**：

```python
# ★ 注意：中文默认引擎已是 SenseVoice-Small（§9.2 缺口 1 + §9.8）。
#   （whisper large-v3 中文 AISHELL-1 CER 5.14%，会议场景高达 18.87%；
#    SenseVoice 约 3.0%、10s 音频仅 70ms，且白送情感与音频事件标签）
#   这里保留 faster-whisper 写法作为"英文/多语路径 + 工程参数范式"的范例。

model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
segments, info = model.transcribe(
    path,
    language=None,                       # ★ 让语言检测真的发生（§1.2 语速单位依赖它）
    vad_filter=True,                     # ★ 治本：静音从输入里物理删除
    vad_parameters=dict(
        min_silence_duration_ms=500,     # 默认 2000 太钝；>500ms 才切段
        speech_pad_ms=200),              # 默认 400；段前后各补，防切字
    word_timestamps=True,                # 特征工程的全部数据源
    condition_on_previous_text=False,    # ★ 治标：切断跨段幻觉传播
    hallucination_silence_threshold=2.0, # 第三道防线（需 word_timestamps=True）
    hotwords="Godot, GDScript, Tween, player.gd",  # 补偿关掉前文条件后的术语一致性
)

assert info.language_probability >= 0.5, f"语言不确定：{info.all_language_probs[:3]}"
words = [w for seg in segments for w in seg.words]   # ★ 只用 word 级，别碰 segment.start/end
```

chunk 策略：VAD 段 >30s 再切（whisper 窗口 30s）；**段间不重叠**——重叠会让"拼接轴 → 原始轴"的映射变成多对一，改用 `speech_pad_ms` 防切字（机制更干净，见 ⑤）。

**④ 演进**：HMM-GMM（需发音词典，逐字建模）→ E2E 神经 ASR（DeepSpeech/Conformer）→ **whisper**（2022：68 万小时弱监督，多语言+零样本鲁棒性革命）→ faster-whisper（CTranslate2 重写：4 倍速+显存减半）→ **2024~2026 中文专精模型爆发（SenseVoice / Paraformer / FireRedASR / Qwen3-ASR：中文 CER 从 whisper 的 5.14% 压到 0.57%，且原生流式、带时间戳、Apache 2.0）** → 多模态语音大模型（一个模型同时出文本+情感+事件）。

> **本模块最重要的工程锚点**：**声学模型是可替换件，我们只写管线与后处理**——所以 §9.4.1 要抽象 `ASRBackend` 协议，**换引擎不得改变 `TranscriptionResult` 契约**。这条抽象不是过度设计：它是"今天用 Qwen3-ASR、明天换 FireRedASR3"的唯一保险，而这类换代在 2026 年**每年都在发生**。

**⑤ 易错点（三条互相咬合，逐条给出正确做法）**：

**(a) word_timestamps 与 vad_filter 同开时要重对齐**

VAD 会把静音**物理剪掉再拼接**（faster-whisper 源码：`audio = np.concatenate(audio_chunks, axis=0)`），whisper 跑在**压缩后的时间轴**上，吐出的 `start/end` 是压缩轴坐标。最后靠 `restore_speech_timestamps()` 映射回原始轴——**交给官方 `vad_filter` 时它已经帮你做了，不用你再加 pad**。

三个必须知道的细节：
- **段级时间戳被词级覆盖**：开了 `word_timestamps` 后 `segment.start = words[0].start`。别混用两套，算时间特征统一用 `word`。
- **自己切片的路径没人管你**：若自己 VAD 切片再喂，偏移是 `原始 = 二级窗口起点 + 切片起点(speech_s − pad) + whisper 输出时间`。**pad 只出现一次，不要重复减**。
- **致命后果**：VAD 拼接后，**任意相邻段之间的间隙恒等于 `2 × speech_pad_ms`（约 0.4~0.8s），与真实停顿多长无关**。举例：真实 6.2s 的卡壳，在压缩轴上只有 0.6s → 掉到 0.8s 阈值以下 → **"卡壳"被判成"流利"，停顿指标整体归零**。

**(b) 中文"字/分"、英文"词/分"——先语言检测再选单位**

混算出的数没法看：3 分钟 600 字的中文演讲，若误按"词"算（中文 whisper 切出约 150 个 token 碎片）→ 50 词/分 → 报告"疑似表达障碍"；真实是 200 字/分（完全正常）。反过来英文 450 词/3min 若按"字"算 → 800 字/分 → "语速是常人 4 倍"。**阈值表也必须按语言分开**（中文 180~240 字/分 vs 英文 130~170 词/分）。

**注意与 ③ 的一致性**：写死 `language="zh"` 就等于没做检测，单位被悄悄钉死成"字/分"。要"先检测"就必须 `language=None` 并用 `info.language` + `info.language_probability` 兜底；**单位在文件级定死一次，不许分段各自检测再汇总**（中英混说时 A 段判 zh、B 段判 en，加法混算就是鬼数）。

**(c) 幻觉双防 = VAD 前置 + `condition_on_previous_text=False`**

- **VAD 前置 = 治本**（让幻觉不产生）：训练数据来自 YouTube 字幕，静音常对应"无字幕"，模型没学过"输出空"，于是按先验吐"谢谢观看"。VAD 把静音从输入删除 = 消灭触发条件。
- **`condition_on_previous_text=False` = 治标**（让幻觉不传播）：默认为 True 时，上一段 token 会作为 `<|startofprev|>` 后的提示喂给下一段。上一段若幻觉出错，错误会自我复制（官方 docstring 原话：`repetition looping or timestamps going out of sync`）。关掉后每段独立解码，**代价是跨段术语不一致 → 用 `hotwords` 补偿**（hotwords 是每个窗口都注入的确定事实，比"沿用模型自己的猜测"安全）。
- **第三道**：`hallucination_silence_threshold=2.0`（需 `word_timestamps=True`）——低置信 + 异常时长 + 前后长静音的段落直接跳过。
- **为什么必须双防**：只有 VAD，音乐/噪声段照样幻觉；只有 `condition=False`，静音段照样幻觉且各段各幻觉各的，更难清理。

### 1.2 语音特征工程（时间戳 → 诊断指标）

**① 严格定义**：转写只是数据，特征才是洞见。三类指标全部从词级时间戳推导（零额外模型）：**流利度**（语速=有效语音时长内字数/分钟；停顿=相邻词间隙>0.8s 思考型 / >2s 卡壳型；节奏方差=语速滑动窗标准差）；**填充词**（词表匹配："就是/然后/那个/嗯/呃/like"，密度=填充词/总词数，中文口语正常 5~8%，>12% 显著）；**结构**（STAR/要点覆盖——LLM 读转写判，规则判不了）。

**② 大白话**：**从录像带出统计表**。教练回看训练录像（转写+时间戳）不是看热闹，而是掐表统计：有效说话时间多长（剔除发呆）、哪里停了 4 秒（卡壳）、"就是"说了多少次（口头禅密度）——**把主观印象变成客观数字**。方法论三步（可迁移到任何领域）：**原始信号 → 可计算指标 → 业务化解释**——"182 字/分"要翻译成"语速偏快，听众易疲劳，建议 150~170"（阈值标定来自领域知识，进配置可调）。

**③ 举例**：

```python
_PUNCT = set("，。！？、；：\"'“”‘’()（）…—,.!?;:-")

def _is_cjk(c): return '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf'

def pauses(words, threshold=0.8, stuck=2.0):
    """★ 前提：words 的时间戳已还原到原始音频轴（§1.1 ⑤a）。
       未还原时所有间隙恒等于 2×speech_pad_ms，此函数直接失效。"""
    return [Pause(start=a.end, duration=b.start - a.end,
                  kind="卡壳" if b.start - a.end > stuck else "思考")
            for a, b in zip(words, words[1:]) if b.start - a.end >= threshold]

def count_units(words, lang: str, en_weight: float = 1.5) -> float:
    """★ 分子：中文数字、英文数词；非主语言片段按比例折算（魔法数进配置）。"""
    if lang.startswith("en"):
        return float(sum(1 for w in words if w.text.strip() and w.text.strip() not in _PUNCT))
    n = 0.0
    for w in words:
        for c in w.text.strip():
            if c in _PUNCT or c.isspace():
                continue
            n += 1.0 if _is_cjk(c) else en_weight   # 中文里的 "Godot" 不按 1:1 字符算
    return n

def speech_rate(words, lang: str, pause_threshold: float = 0.8) -> float:
    """★ 分母 = 首词起点到尾词终点的跨度 − 所有超阈值间隙。
       不引用音频总长 → 对 VAD 剪切/时间戳压缩天然免疫。"""
    if len(words) < 2:
        return 0.0
    span = words[-1].end - words[0].start
    if span <= 0:
        return 0.0
    gaps = [b.start - a.end for a, b in zip(words, words[1:])]
    silent = sum(g for g in gaps if g >= pause_threshold)
    effective = max(span - silent, 1e-3)          # 下界保护，防负数/除零
    return count_units(words, lang) / (effective / 60)
```

> **为什么不用 `total_s - pause_s`**：§5 原测试"60s 音频含 10s 静音、100 字 → 120 字/分"隐藏着一个 bug——`pauses()` 只看**相邻词之间**的间隙，**开头/结尾的静音不产生 gap**。若那 10 秒静音在录音开头（说话人愣了 10 秒才开口），`pause_s = 0`，算得 100 字/分；而真实有效说话时长只有 50 秒，正确答案是 **150 字/分**。**同一个音频，静音位置不同就差 30%。** 用"首尾词跨度 − 长间隙"则两种位置都得到 150。

**④ 演进**：人工听录音评估（主观不可复现）→ 规则指标（客观但零散）→ 指标+LLM 综合诊断（结构化输入，报告有据）。与 RAG 引用同理：**LLM 的判断要挂在可复核的数值上**。

**⑤ 易错点**：
- 语速分母是"纯说话时间"不是音频总长——含 30s 静音的录音直接腰斩语速。**更精确的说法**：分母应是**首尾词跨度 − 长间隙**，而不是"音频总长 − 停顿"——后者漏算开头/结尾静音（见上文对 §5 原测试的驳正）
- 填充词匹配要归一化（"就是就是"连说、繁简、大小写）且引述语境豁免（"他说'那个'一词"不算）
- 阈值（0.8s/2s/5~8%）进配置文件并注明数据来源——魔法数可追溯
- **ITN 会改变分子**："百分之三十"→"30%"后字数从 5 变 3。归一化必须在算特征**之前**完成，且全文用同一口径（否则不同音频不可比）
- **低置信要标注而非静默**：`word.probability < 0.15` 或 `segment.compression_ratio > 2.4` 的段落，报告里应注明"该段置信度低，数值仅供参考"——给个错数字比标注不确定更糟

### 1.3 诊断报告生成与语音入口集成

**① 严格定义**：特征值+转写全文喂 LLM 生成报告——**输入结构化+输出模板化**（总评/流利度/填充词 Top3/结构建议/3 条改进项；每个论断标注数据依据；数据与内容矛盾以数据为准）。集成双身份：**transcribe 是工具**（模型可用，M04 注册）+**语音入口是产品功能**（用户直接用，M19 API/M20 录音组件）。

**② 大白话**：诊断报告=**体检报告**：化验单数值（特征）+ 医生解读（LLM）——解读必须指着化验单说话（"谷丙转氨酶 62，参考区间 9-50，偏高"），不许空口"肝功能欠佳"。语音入口集成：录音文件统一 ffmpeg 转 16kHz mono wav（体检前的采样标准化）；隐私默认：**录音不落库只存转写与特征**（设置可开关）。

**③ 举例**：报告提示（§2 前的最后一眼）：

```python
DIAGNOSE_PROMPT = """基于实测数据与转写，生成口语诊断报告。
实测数据（优先采信，不得改写数值）：{features_json}
转写全文（用于结构与内容分析）：{transcript}
输出：总评(10分制)/流利度(实测值对照参考区间)/填充词Top3(语境)/结构建议/3条改进项
要求：每个论断标注数据依据；必须指出最严重的一个问题。"""
```

**④ 演进**：纯 LLM 听音频（贵慢不可复核）→ 特征+LLM 综合（当前）→ 多模态直评（未来，可复核性仍不如数值）。

**⑤ 易错点**：
- 长转写（>30min）超上下文：分段诊断再汇总（复用 M07 压缩思想）
- LLM 和稀泥评语——提示强制"指出最严重问题"
- 报告落盘同时存特征 JSON（可复核可二次分析——审计思维）

### 1.4 TTS 流式合成（分句进、音频块出，首音时延是唯一 KPI）

**① 严格定义**：TTS（Text-To-Speech）= 文本→音频波形的合成；**流式 TTS** 指输入端吃**增量文本**（不必等全文）、输出端吐**音频块**（不必等整段合成完）。生产形态是**分句流式**：LLM 流式输出按标点/韵律切句，逐句送合成、逐块播放。衡量指标不是总合成时长而是**首音时延**（首块音频可播时刻 − 用户说完时刻）。

**② 大白话**：**同声传译不等人说完**。整段合成再播 = 译员听完一整段演讲才开口（用户盯着转圈 3 秒）；分句流式 = 译员听到第一个逗号就开口翻。首音时延决定"对话感"——人一旦听到声音，耐心计时器就重置（§7.10 的结论在输出侧同样成立）。

**③ 举例**：LLM 流式吐出"好的。我先看 player.gd 的 jump 函数。修改重力参数…"。分句器在第一个"。"切出首句 2 字 → 立即送 TTS → 合成 200ms + 播放启动 50ms → **首音 ≈250ms**（此时 LLM 还在吐第二句、TTS 在合成第一句——三件事同时发生）。分句双阈值：韵律边界（。！？；）优先 + **长度兜底**（18 字强制切，防长句饿死首音）。

**④ 演进**：拼接式（音库拼接，机械感）→ 统计参数 HTS（小而糊）→ 神经两段式（Tacotron 声学 + WaveNet 声码器，质量革命但慢）→ 端到端神经 TTS（VITS/CosyVoice：零样本音色克隆）→ **实时流式服务**（edge-tts/OpenAI TTS：HTTP 分句流式、多音色）。工程锚点与 ASR 同款：**声学/声码模型是可替换件，我们只写分句与调度**。

**⑤ 易错点**：
- 分句贪长：等"。"再切，首句可能 40 字——必须长度兜底强制切
- 代码符号读法："player.gd"被读成拼音——词表正则预替换为读法（"player dot G D"）
- 采样率三处一致（合成器/播放器/录音器），48k 合成 16k 播放 = 变速怪声
- 音色/语速参数进 `config/voice.yaml` 并注明来源——与 §1.2 阈值同款"可追溯魔法数"纪律

### 1.5 端点检测与流水线重叠（把 1.5 秒预算拆着省）

**① 严格定义**：**端点检测**（endpointing）= 实时判断"用户这句话说完了"：静音 ≥400~500ms 且带迟滞（换气 200ms 不误切、最短话长 250ms 防咳嗽误触发）。**流水线重叠** = ASR/LLM/TTS 三段不串行等待而部分并行：ASR 输出**部分结果**（partial，可被推翻）时 LLM 即**预热**（warmup：构造请求上下文、预取首 token），**定稿**（final）后校正并继续。

**② 大白话**：**接力赛交棒不站定**。串行 = 每一棒跑完站住，下一棒才起跑；重叠 = 下一棒看到前一棒冲过来就先起步（partial 预热），棒到手（final）时已在途中。端点检测是"何时交棒"的裁判：判早了切断语义（"我要……加双跳"被腰斩），判晚了白等半秒——**它是全链路最难压的一段，因为它的原料是"未来的静音"——你只能等它发生**。

**③ 举例**：数字对拍（同一套模型，只改调度）：串行 = VAD 断句 300 + ASR 尾块 400 + LLM 首 token 600 + TTS 首音 300 + 播放 50 = **1.65s**。重叠后：ASR 尾块期间 LLM 已用 partial 预热（省 ~300ms）、LLM 首 token 期间分句器已切出首句（省 ~100ms）→ **~1.25s**。重叠是零模型成本的延迟优化——**先调度后模型，是延迟优化的正确顺序**。

**④ 演进**：按键说话（无检测，最可靠但反自然）→ 固定静音阈值（简单但两难：快了切句慢了拖沓）→ 迟滞+最短话长（**本节的物理层方案**）→ **语义端点模型（Smart Turn v3.2：音频原生、8MB、65ms，见 §9.2 缺口 10）**。与离线 VAD 的关系：同一个 Silero，**离线吃整段（管切得准），在线吃环形缓冲（还要管切得快）**——§1.1 的 vad_segments 是前者，端点检测是后者的在线版。

> **§9 修订补充说明**：本节的"LLM 判断语义完整否"已被**专用小模型**取代——用 LLM 判端点要付出 300ms+ 的首 token 代价，而 Smart Turn v3.2 只有 65ms 且**直接吃 PCM**（能捕捉句末降调，这是文本层的 LLM 拿不到的信号）。本节的数字对拍更新为：TTS 首音 300 → **97ms**（Qwen3-TTS-0.6B），端到端目标由 1.25s 收紧到 **<1.0s**。§9.7 有完整的修订后预算表。

**⑤ 易错点**：
- 把 partial 当 final 用：部分结果被推翻后 LLM 已答错——**预热可以，落史/执行工具必须等 final**（铁律）
- 环形缓冲覆盖说话中的帧 → 转写开头丢字；缓冲至少 = 端点阈值 + 200ms pad（同 §1.1 的 speech_pad）
- 说话中"嗯…"犹豫 800ms 被切句——迟滞窗口内 partial 持续变化（有新词）时不判端点
- **以为语义端点能替代 VAD**：不能。Smart Turn 的输入是"一个 turn 的音频"，它需要先知道 turn 从哪开始——这个仍靠 Silero。**两者是串联，不是替换**（§9.2 缺口 10）

### 1.6 全双工状态机与打断（barge-in · AEC）

**① 严格定义**：**全双工** = 麦克风采集与扬声器播放同时进行。**打断**（barge-in）= Agent 说话中检测到用户开口 → 立即停止播放、丢弃已合成未播音频、把用户的话作为新输入。**回声消除**（AEC，Acoustic Echo Cancellation）= 从麦克风信号中减去扬声器正在播的内容，防 Agent 被自己的声音触发 VAD（自我打断死循环）。实现载体是三态状态机：`LISTENING`（收音+端点检测）→ `THINKING`（LLM 推理+工具执行）→ `SPEAKING`（TTS 播放+持续监听打断）→ 回 LISTENING。

**② 大白话**：**对讲机 vs 打电话**。对讲机半双工（一方说完另一方才能说）；电话全双工（能同时说、能抢话）。打断是"抢话筒"：Agent 说到一半你喊"等等"——它必须 300ms 内闭嘴（不然你说的它听不见）、扔掉没播的话（过时的回答比沉默更糟）。AEC 是**隔音墙**：不隔的话麦克风把自己的声音录进去，VAD 以为有人在说话——Agent 自己打断自己，无限套娃。

**③ 举例**：打断时序走一遍数字：Agent SPEAKING（已合成 12 块、播到第 5 块）→ 你开口"改成三段跳" → 浏览器 AEC 消回声后 VAD 仍确认人声（200ms）→ cancel 播放任务 + 丢弃第 6~12 块（连同播放器缓冲 flush）+ 上轮发言**截断记录到实际播到处** + 状态回 LISTENING → 新一轮以"（用户打断：改成三段跳）"衔接。全链路静音 <300ms。哲学：**掐断不做淡出**——淡出的 200ms 会盖住用户开口的第一个字。

**④ 演进**：按键打断（物理最可靠）→ 半双工轮替（电话客服时代，无打断）→ AEC+VAD 软件打断（当前：浏览器 getUserMedia 自带 echoCancellation，服务端无需自研 AEC）→ 端到端全双工模型（Realtime API 类：模型原生边听边说）。**自研状态机而非直接托管 Realtime API 的理由**：工具调用必须走自家 Agent Loop（M03 的 ReAct + M09 权限门）——托管语音 Agent 拿不到我们的工具体系与权限确认流，这个取舍与 M02"自建网关而非直连厂商 SDK"同源。

**⑤ 易错点**：
- THINKING 态也在收音：工具执行的几十秒里用户说的话——进**预输入队列**，本轮结束先处理（不丢话）
- 打断后上轮发言不截断：LLM 以为自己说完了完整答案，下一轮上下文错位
- 丢弃 TTS 块要连播放器缓冲一起清（前后端约定 flush 协议），否则漏半句
- AEC 依赖浏览器/硬件实现，移动端兼容性参差——首次连接播 1s 测试音做回声自检（VAD 误触发则提示换耳机）

---

## 2. 接口设计（完整签名）

> ⚠️ 本节的 `TranscriptionResult` 是**全模块的数据契约**——下游 features/diagnose/UI 全部挂在它上面。§9 换 ASR 引擎时**只改 `ASRBackend` 实现，不改这个契约**。

```python
# voice/stt.py
@dataclass
class WordInfo:
    text: str; start: float; end: float
    prob: float = 1.0            # ★ 词置信度（低置信要标注而非静默）

@dataclass
class Seg:
    start: float; end: float; text: str; words: list[WordInfo]
    speaker: str | None = None   # ★ SPEAKER_00（diarize 可选，绝不含真实姓名）
    emotion: str | None = None   # ★ SenseVoice 白送：HAPPY/SAD/ANGRY/NEUTRAL...
    events: list[str] = field(default_factory=list)   # ★ BGM/Laughter/Applause/Cough
    avg_logprob: float = 0.0
    compression_ratio: float = 0.0    # >2.4 判疑似幻觉
    no_speech_prob: float = 0.0

@dataclass
class Provenance:                # ★ 溯源：报告里的数字要能复核（§1.3 同原则）
    engine: str; engine_version: str
    language: str; language_prob: float
    aligned: bool; normalized: bool

@dataclass
class TranscriptionResult:
    language: str; duration: float; segments: list[Seg]
    provenance: Provenance

# ★ 多引擎抽象（§9.4.1）：声学模型是可替换件
class ASRBackend(ABC):
    name: str; supports_streaming: bool; supports_emotion: bool
    @abstractmethod
    async def transcribe(self, wav: Path, *, lang: str | None,
                         hotwords: list[str]) -> TranscriptionResult: ...
class WhisperBackend(ASRBackend): ...      # 英文/多语主力（faster-whisper）
class QwenAsrBackend(ASRBackend): ...      # 中文主力（原生流式）
class FireRedBackend(ASRBackend): ...      # 中文高精（离线诊断）
class SenseVoiceBackend(ASRBackend): ...   # 中文 + 情感 + 事件

class Transcriber:
    """编排：preprocess → backend → align → normalize（见 §9.4.1）。"""
    def pick(self, lang_hint: str | None, *, high_accuracy: bool) -> ASRBackend: ...
    async def transcribe(self, audio: Path, *, lang: str | None = None,
                         high_accuracy: bool = False) -> TranscriptionResult: ...

# voice/vad.py
def vad_segments(audio: np.ndarray, sr: int, threshold: float = 0.5,
                 min_speech_ms: int = 250,
                 min_silence_ms: int = 500) -> list[tuple[float, float]]: ...

# voice/features.py
@dataclass
class Pause: start: float; duration: float; kind: Literal["思考", "卡壳"]
@dataclass
class SpeechFeatures:
    speech_rate: float; pauses: list[Pause]; fillers: dict[str, int]
    rhythm_variance: float; total_speech_s: float
    rate_unit: str                # ★ "字/分" | "词/分"——由 language 决定（§1.1 ⑤b）
    low_confidence: bool          # ★ 该份转写是否含低置信段
def extract_features(tr: TranscriptionResult, filler_words: list[str]) -> SpeechFeatures: ...
def to_diagnosis_input(f: SpeechFeatures) -> dict: ...

# 工具注册（M04）
@register_tool(readonly=True, risk="low")
class TranscribeTool(BaseTool): ...     # 音频路径 → 转写+时间戳
@register_tool(readonly=True, risk="low")
class AnalyzeSpeechTool(BaseTool): ...  # 音频路径 → 特征+诊断报告
```

实时对话三件套（stream_asr / tts / realtime，§1.4~1.6 的落点）：

```python
# voice/stream_asr.py —— 流式转写（增量文本：partial 可推翻 / final 定稿）
@dataclass
class AsrDelta: text: str; is_final: bool; end: float

class StreamingPolicy(ABC):
    """同时策略：决定"什么时候敢吐字"（§9.2 缺口 9）。"""
    @abstractmethod
    def update(self, tokens: list[str]) -> tuple[str, str]: ...   # (confirmed, partial)
class AlignAttPolicy(StreamingPolicy): ...
#  首选：交叉注意力判断解码进度，触及音频缓冲区末尾的"危险区"即停，等下一块
class LocalAgreementPolicy(StreamingPolicy): ...
#  次选：取相邻两次更新输出的最长公共前缀作为 confirmed（易实现，质量略次）

class StreamingTranscriber(ABC):
    """音频块进、增量文本出。partial 只供热身，final 才可入史（§1.5 铁律）。"""
    async def feed(self, pcm: bytes) -> AsyncIterator[AsrDelta]: ...
class LocalStreamTranscriber(StreamingTranscriber): ...   # 本地：Qwen3-ASR 原生流式
class RemoteStreamTranscriber(StreamingTranscriber): ...  # 生产版：厂商 WS 流式 ASR

# voice/turn.py 的端点检测（§9.2 缺口 10）—— 两层串联，非替换
class TurnDetector(ABC):
    async def is_complete(self, turn_pcm: bytes) -> bool: ...
class SmartTurnV3(TurnDetector): ...
#  Whisper-Tiny backbone + 线性分类头，8M 参数 / int8 仅 8MB / CPU 10ms、云端 65ms
#  音频原生（吃 PCM 不吃文本）→ 能捕捉句末降调等文本里没有的韵律线索
#  ★ 前置仍是 Silero VAD：它需要先知道 turn 从哪开始

# voice/tts.py —— 流式合成（分句进、音频块出）
@dataclass
class TtsChunk: seq: int; audio: bytes; is_final: bool

class Synthesizer(ABC):
    """逐句吃文本、逐块吐音频。首音时延是唯一 KPI（§1.4）。"""
    async def synthesize_stream(self, sentences: AsyncIterator[str],
                                voice: str = "zh-CN-XiaoxiaoNeural") -> AsyncIterator[TtsChunk]: ...
class RemoteSynthesizer(Synthesizer): ...    # edge-tts / OpenAI TTS（复用 M02 韧性管道）
class LocalSynthesizer(Synthesizer): ...     # CosyVoice 本地服务（隐私优先场景）

# voice/realtime.py —— 全双工会话状态机（§1.6 的落点）
class RealtimeState(str, Enum):
    LISTENING = "listening"    # 收音 + 端点检测
    THINKING = "thinking"      # LLM 推理 + 工具执行（收音进预输入队列）
    SPEAKING = "speaking"      # TTS 播放 + 持续监听打断

class RealtimeSession:
    """一条 WS 连接一个会话：全双工收放音，端点检测/重叠流水线/打断都在这。"""
    async def run(self, mic: AsyncIterator[bytes],
                  sink: Callable[[TtsChunk], Awaitable[None]]) -> None: ...
    async def _endpoint(self, vad_prob: float, partial_changed: bool,
                        turn_pcm: bytes) -> bool: ...
    # ★ 两层（§9.2 缺口 10）：① 物理迟滞：静音≥500ms 且 partial 无新词 且 话长≥250ms
    #                        ② 语义层：Smart Turn v3.2 判"这句语义完不完整"（需 padded 到 8s）
    async def _pipeline(self, final_text: str) -> AsyncIterator[TtsChunk]: ...
    # partial 预热 LLM → M03 loop 流式 → 分句器（§1.4 双阈值）→ TTS 逐句
    async def _barge_in(self, mic_frame: bytes) -> bool: ...
    # SPEAKING 态监听：AEC 后仍检测到人声 ≥200ms → cancel 播放 + 丢弃未播块
    async def _truncate_last_turn(self, played_upto: int) -> None: ...
    # 打断善后：上轮 Assistant 记录截断到实际播到处（防上下文错位）
```

---

## 3. 关键难点参考片段

### 3.1 ASR 服务化（GPU 复用）

> ⚠️ 标题原为"whisper 服务化"。§9 对标后 whisper 退居**英文/多语路径**，但**"声学模型服务化"这一节的全部工程结论完全不变**（GPU 复用、长任务超时、重试语义）——它描述的是**接入层模式**，与具体引擎无关。下面的 `RemoteTranscriber` 保留作 whisper 后端的参考实现，中文后端（`QwenAsrBackend` / `FireRedBackend`）套用同一模式。

模型冷加载 3~10s，每请求冷启不可接受——ASR 常驻服务（deploy/compose），接入层走 HTTP：

```python
class RemoteASRBackend(ASRBackend):
    """ASR 常驻服务（TEI 同款部署模式）——引擎无关，换 base_url 即可换后端。"""
    name = "whisper-remote"                         # 或 "qwen3-asr-remote" / "firered-remote"

    @with_retry(max_retries=2)                      # M02 韧性管道第四次复用
    async def transcribe(self, wav: Path, *, lang: str | None,
                         hotwords: list[str]) -> TranscriptionResult:
        async with httpx.AsyncClient(timeout=600) as c:   # 长音频耗时
            r = await c.post(f"{self.base}/transcribe",
                files={"file": wav.open("rb")},
                data={"language": lang or "", "word_timestamps": "true",
                      "hotwords": ",".join(hotwords)})
            r.raise_for_status()
            return TranscriptionResult.from_api(r.json())
```

为什么难：转写是**分钟级长任务**——超时、重试语义（重试会不会重复计费/重复转写）、大文件上传三件事都与 LLM 调用不同；韧性管道复用但参数要按语音特性重调。

**服务化的选型判据（2026 更新）**：

| 维度 | 进程内加载 | 常驻服务 |
|---|---|---|
| 显存 | 每进程一份（large-v3 约 3GB，多进程浪费） | **单份共享**（RAG 嵌入 / 多 ASR 引擎共用 GPU 机） |
| 故障隔离 | 崩溃连累主服务 | **独立容器，可独立扩缩容** |
| 延迟 | 零网络 | 一次 HTTP 往返（几十 ms，相对分钟级转写可忽略） |
| **多引擎** | 每加一个引擎多一份常驻显存 ❌ | **加引擎只需多一个容器** ✅ |

本项目选服务化——**且 §9 引入多引擎后这个决策的收益翻倍**：三个 ASR 引擎 + embedding 服务可以共享同一张 GPU，靠容器编排调度，进程内加载的方案在多引擎下会直接爆显存。

### 3.2 实时会话主循环（三路异步 + 打断传播）

```python
async def run(self, mic, sink):
    state = RealtimeState.LISTENING
    async for frame in mic:                       # 20ms 帧（浏览器 AEC 后的 PCM）
        async for delta in self.asr.feed(frame):  # 流式 ASR
            if not delta.is_final:
                await self.llm.warmup(delta.text)         # ① 重叠：partial 只预热不提交
                continue
            state = RealtimeState.THINKING
            async for chunk in self._pipeline(delta.text):  # ② LLM 流式→分句→TTS 逐块
                state = RealtimeState.SPEAKING
                if await self._barge_in(frame):            # ③ 播放中持续监听
                    await self._truncate_last_turn(chunk.seq)
                    break                                  # 丢弃未播块，回 LISTENING
                await sink(chunk)
            state = RealtimeState.LISTENING
```

为什么难：三个"同时"——**同时收音/播放**（全双工）、**同时跑 ASR 与 LLM**（重叠预热）、**同时播 TTS 与监听打断**（barge-in）——每一路都是独立异步流，任何一路 await 卡住，另两路的实时性立刻破功。更难的是打断的**传播深度**：cancel 不能只停生成，还要穿透"已进播放器缓冲"的音频（前后端约定 flush 协议），否则用户听到漏出的半句。warmup 只构造请求不提交（partial 可被推翻），是这个循环里"激进"与"正确"的分界线。

---

## 4. 手敲指引（函数级伪代码）

> ⚠️ 步骤 2/6/7/8 已按 §9 修订；步骤 **9~14 为对标后新增**。

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 0 | `lab/m16/` | 自录 1 分钟样本，人工标注词数/停顿位置 + **逐字转写（CER 标注集）** | 标注表 + CER 基线 |
| **9** | **`voice/preprocess.py`** | `to_16k_mono：ffmpeg 统一转码；loudnorm：EBU R128 归一到 -16 LUFS；highpass 去 DC/低频` | 不同音量录音的 VAD 段边界一致 |
| 1 | `voice/vad.py` | `vad_segments：silero 逐帧概率→阈值判定→min_speech/min_silence 合并→返回 (start,end) 列表` | 段边界与人工听感一致 |
| 2 | `voice/stt.py` | **多引擎**：`ASRBackend` 协议 + 按语言路由（§9.4.1）；`Transcriber.transcribe：preprocess→backend→align→normalize`；含 `hotwords`/`condition_on_previous_text=False`/`hallucination_silence_threshold` | **CER < 8%**（非目测）；whisper 基线对照 |
| **10** | **`voice/align.py`** | `align_words：wav2vec2 + CTC trellis→beam backtrack→字符级→词级；中文逐字符对齐；失败回退原时间戳` | 词边界与人工标注偏差中位数 <120ms |
| **11** | **`voice/normalize.py`** | `itn（中文数字→阿拉伯数字）；restore_punc；redact_pii（落库前脱敏）` | ITN 前后词数变化可解释；手机号已脱敏 |
| 3 | `voice/features.py` | `pauses/speech_rate：§1.2 ③（首尾词跨度法）；count_units：按语言选单位；fillers 归一化；rhythm_variance：10 词滑窗；to_diagnosis_input：特征+**情感**→JSON（附参考区间）` | 与人工标注对拍误差 <10% |
| **12** | **`voice/diarize.py`**（可选） | `diarize：cam++（中文）/pyannote-3.1（英文）→ 段打 SPEAKER_xx 标签` | 标签**只有编号无真实姓名** |
| 4 | 诊断流+工具 | `AnalyzeSpeechTool.run：transcribe→extract_features→DIAGNOSE_PROMPT 调 LLM→报告+特征 JSON 一起返回；TranscribeTool 只转写` | 模型在对话中可调两工具 |
| **14** | **`voice/export.py`** | `to_srt/to_vtt/to_json：词级时间戳→字幕（含说话人标签）` | 字幕可导入剪辑软件 |
| 5 | 入口集成 | `preprocess 统一转码；隐私开关（录音不落库）；M19 挂 POST /voice/transcribe` | 聊天发语音→自动转文字响应 |
| 6 | `voice/stream_asr.py` | **`AlignAttPolicy`（交叉注意力触及危险区即停）/ `LocalAgreementPolicy`（相邻两次最长公共前缀）；VAC 前置跳静音** | final 与整段转写一致；partial 推翻率 <30% |
| 7 | `voice/tts.py` | `分句器：韵律边界优先+18 字兜底；词表预替换代码符号读法；**QwenTtsSynthesizer**（97ms 流式）逐句请求→TtsChunk（seq 递增）` | 首句首音 <300ms；seq 连续无洞 |
| 8 | `voice/realtime.py` + M19 WS | `RealtimeSession：§3.2 主循环；_endpoint **物理迟滞 + Smart Turn v3.2 语义层**；_barge_in 人声 200ms 确认→cancel+flush；_truncate_last_turn 截断；M19 挂 WS /voice/realtime` | 端到端首音 **<1.0s**；打断静音 <300ms |
| **13** | **`voice/metrics.py`** | `RtfRecorder/TtfaRecorder/VadTrimRate/HallucinationRate/CerReporter → 接 M21 observability` | 每次调用均产出埋点 |

---

## 5. 测试与验收

```python
# ── 原六条（第 1、4 条已按 §1.2 ③ / §1.1 ⑤ 修正）────────────────────
def test_speech_rate_excludes_pauses():
    # ★ 修正：静音在"中间"→ 100 字 / (60-10)s = 120 字/分
    #   静音在"开头/结尾"→ 同样 100 字 / 50s = 120 字/分（旧公式会算成 100，错 30%）
    #   两种位置必须得到同一个数——这是选"首尾词跨度法"而非"总长减法"的理由

def test_filler_count_normalization():
    # "就是就是"计 2 次；引述"那个"不计（豁免标注）

def test_pause_kinds_threshold():
    # 0.8~2s→思考；>2s→卡壳；边界 0.8s 恰好不计

def test_barge_in_discards_pending_tts():
    # SPEAKING 态（已合成 12 块、播到第 5 块）注入人声帧 → 第 6~12 块未送 sink 即被丢弃
    # 且状态回 LISTENING、上轮 Assistant 记录截断到第 5 块

def test_pipeline_overlap_saves_latency():
    # partial 预热生效：LLM 首 token 时刻早于 final 到达时刻 ≥300ms（假时钟对拍）

def test_endpointing_hysteresis():
    # 200ms 换气不切句；500ms 静音切句；partial 持续出新词时静音再长也不切

# ── §9 新增（对标补全后必测）────────────────────────────────────────
def test_vad_offset_restores_pauses():
    # 造 25s 音频：静音3s / 语音9s / 静音6s / 语音7s，speech_pad_ms=200
    # 断言压缩轴间隙 ≈0.6s（<0.8 阈值）；还原后 ≈6.2s → 判"卡壳"（防 §1.1 ⑤a 回归）

def test_no_double_pad():
    # pad=200 时首词 start ≈ 真实语音起点 ±0.25s，不得整体前移 0.2s（防重复减 pad）

def test_timestamps_sane():
    # 单调不重叠 & last.end <= duration & 0.4 <= 有声占比 <= 0.98
    # 有声占比飙到 0.95+ → 说明中间静音被抽干，时间戳多半没对齐

def test_units_follow_detected_language():
    # 同一段中文：按 zh 口径得 150~260 字/分；误按 en 口径会掉到 50~80 → 断言单位切换生效

def test_cer_regression():
    # lab/m16 标注集上 zh 路由引擎 CER < 8%，并跑 whisper-large-v3 作对照基线记录差值

def test_align_fallback_never_raises():
    # 强制对齐抛异常 → 回退原始时间戳，words 非空且单调性成立（管线不断）

def test_hotwords_injected_every_window():
    # 热词在每个窗口的 prompt 中出现，而非只首窗（对照 faster-whisper get_prompt 源码）

def test_semantic_endpoint_beats_silence_only():
    # "我用的是……"（悬停）判未完；"我用的是 Godot。"（句末）判已完
    # 两者静音时长相同 → 只有语义层能分开（Smart Turn v3.2）

def test_diarize_labels_are_anonymous():
    # 分离结果只有 SPEAKER_00/01，绝不含真实姓名（隐私红线）

def test_pii_redacted_before_persist():
    # 转写含手机号 → 落库前已脱敏，且脱敏不改变时间戳与词数

def test_rtf_and_ttfa_recorded():
    # 每次转写/合成都产出 RTF 与 TTFA 埋点，非空（可观测性）
```

**验收 Demo（MI-5，按 §9.7 升级）**：录 3 分钟"自我介绍+项目讲解" → `voice analyze` 出报告（语速/填充词/停顿分布 + **情感倾向** + LLM 建议，数值带参考区间）→ 导出 **SRT 字幕 + 特征 JSON + CER 报告** → 聊天发同段语音，Agent 转写后正确执行语音里的请求（"帮我加双跳"）→ `voice chat` 实时对话：说"给玩家加双跳"，**1.0s** 内听到语音应答且工具卡照常执行；Agent 回答中插话"改成三段跳"→ 300ms 内闭嘴、听懂并改执行。

---

## 6. 踩坑记录（留白自填）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

---

## 7. 面试拷打（附详细参考答案）

**1. 为什么要 VAD 前置？whisper 静音幻觉是什么？**
答：VAD 三收益：①省算力——静音段不跑模型（会议录音 40% 是静音，直接省 40% GPU）；②提质量——whisper 训练数据来自 YouTube 字幕（68 万小时弱监督），静音段常对应"片尾无字幕"，模型会幻觉出"谢谢观看/订阅频道"等高频片尾语——著名工程坑；③控段长——30s 窗口适配（超长段切分）。静音幻觉的根因是**训练分布的偏见**（片尾字幕与静音强相关），VAD 从输入侧消灭静音段=消灭幻觉触发条件，另配 `condition_on_previous_text=False` 防前文污染传播（前一段的幻觉种子不会传给下一段）。

**2. whisper 时间戳有什么用？哪些特征完全依赖它？**
答：时间戳把转写从"一串字"升级为"带坐标的事件序列"——全部流利度特征依赖它：语速（词数÷有效时长）、停顿（相邻词间隙分布与分类）、节奏方差（滑窗语速波动）、填充词定位（不只计数还能给"出现在哪个语境"）。没有时间戳这些指标全部退化为人工掐表。它是 whisper 相对传统 ASR 的关键产品化优势（传统 ASR 只给文本，对齐要另跑 forced alignment 模型）——**选型 whisper 的决定性理由之一就是免费自带词级时间戳**。

**3. 语速的分子分母分别是什么？为什么剔除停顿？**
答：分子=语言相关的"字/词数"（中文数字符、英文数词——先语言检测选单位）；分母=**有效语音时长**=总时长−停顿时长（≥0.8s 的间隙全剔除）。剔除的理由：语速衡量"说话时的输出密度"，思考停顿是另一个维度（有独立的停顿指标）——含静音的语速把两个维度搅在一起（30s 沉默把 200 字/分的快嘴算成 95 字/分的慢条斯理），诊断价值归零。指标设计原则：**一个指标测一件事**（正交分解），语速与停顿分开测才有可解释的诊断。

**4. 停顿 0.8s/2s 两档的依据？魔法数怎么管理？**
答：依据是口语研究经验值：0.8s 以下的间隙是正常换气/词间过渡（不算"停"）；0.8~2s 是思考型（组织语言，适度出现健康甚至加分——显深思熟虑）；>2s 是卡壳型（忘词/紧张/大脑空白，需要干预）。魔法数管理：进 `config/voice.yaml`，每条注释数据来源（"0.8s: 中文口语研究常用阈值"），并注明"可按场景调整"——面试场景 2s 卡壳正常些，直播场景 1.5s 就致命。原则：**魔法数不是罪，无出处的魔法数才是罪**——可追溯+可配置+有理由。

**5. 特征工程三步法怎么迁移到游戏领域（玩家行为分析）？**
答：三步法=原始信号→可计算指标→业务解释。迁移示例：玩家行为分析——信号=游戏内事件流（点击/移动/死亡日志，对应词时间戳）；指标=会话时长/死亡间隔分布/操作 APM/关卡放弃率拐点（对应语速/停顿）；业务解释="新手关死亡间隔中位数 15s 且集中于第二波敌人→难度曲线陡，建议该波血量-20%"（对应"语速 182 偏快，建议 150~170"）。通用性来源：任何领域都是**从带时间戳的事件序列提取统计特征，再翻译成领域行动建议**—— whisper 只是这个框架在语音域的实例。

**6. 诊断报告为什么要"数值引用可回链"？**
答：与 RAG 引用同构的**信任机制**：报告说"语速过快"如果不带实测值与参考区间，用户无法验证（与主观印象冲突时谁对？）；带 `[实测: 182, 参考: 150-170]` 后论断可复核，且实测值可回链到原始时间戳（点开看具体哪段快）。第二收益：**LLM 幻觉的检测接口**——提示要求"不得改写数值"，若报告里的数值与特征 JSON 不符，即幻觉实锤（自动校验一条正则）。可复核性是所有分析类产品的信任底线（体检报告/信用分/风控决策都如此）。

**7. condition_on_previous_text=False 防什么？**
答：防**跨段幻觉传播**。whisper 默认把上一段的转写作为下一段的条件上下文（提升连贯性），但副作用：上一段若幻觉出错误文本（或转错专有名词），这个错误成为下一段的"先验"——模型倾向沿用/延续它，错误像病毒一样在后续段落繁殖（长音频尤甚：一个错误的游戏名词污染整段转写）。关掉后每段独立转写（段间连贯性损失可接受——我们有 VAD 分段语义边界），配合后处理（专有名词词表强制纠正）双保险。这是**用少量连贯性换稳定性**的工程取舍。

**8. whisper 服务化与本地加载的取舍？**
答：本地（进程内 import）：零网络延迟、开发简单——但模型常驻显存（large-v3 约 3GB）、多进程各自加载浪费、GPU 调度不可控；服务化（独立容器）：模型单份常驻共享（RAG 嵌入/whisper 共用 GPU 机）、进程崩溃不连累主服务、可独立扩缩容——代价是一次 HTTP 往返（几十 ms，相对分钟级转写可忽略）。本项目选服务化，且接入层复用 M02 韧性管道（with_retry+CircuitBreaker——第三次复用：LLM/embedding/whisper）——**基础设施的投资在第三次复用时开始净赚**。

**9. 录音隐私的产品设计考量？**
答：五条：①**最小化采集**——录音默认"转写即弃"（只存转写+特征，原始音频不落库）；②**知情开关**——语音功能默认关闭，首次启用显式告知（"录音将发送至转写服务"）；③**本地优先选项**——LocalTranscriber 给"全部本地处理"选项（隐私敏感用户）；④**保留期限**——若用户选择留存录音，默认 7 天过期删除；⑤**第三方流转透明**——若转写走云端 API，隐私政策明示。设计哲学：语音是生物特征相邻数据（声纹可识别个人），**默认从紧、放开需用户主动**——与 M09 权限的 deny 优先同哲学。

**10. 实时语音对话的延迟预算怎么拆？哪段最难压？**
答：目标体感"对话级"= 首音 <1.5s（人类对话轮换间隔约 200~500ms，AI 到 1s 内已可接受）。预算拆解：VAD 断句 100~300ms（**最难压**——它的原料是"未来的静音"，只能等它发生：太快切断语义，等太久加延迟）；流式 ASR 尾块 200~400ms（非整段转写，边说边转只等最后一块）；LLM 首 token 300~800ms（TTFT：短提示+流式+就近推理，M02 网关的延迟指标直接复用）；TTS 首音 200~400ms（分句流式，句级返回）；播放启动 50ms。合计 0.85~1.95s，本项目重叠后目标 1.25s（§1.5 的对拍数字）。优化顺序铁律：**先调度后模型**——流水线重叠（partial 预热省 ~300ms）零模型成本；三板斧：端点迟滞自适应、ASR/LLM 重叠预热（warmup）、TTS 分句流式。体验关键不是总时延而是**首音时延**——人听到声音，耐心计时器就重置。

**11. 打断（barge-in）为什么要"丢弃"而不是"暂停"已合成的音频？**
答：三个理由：①**语义已过时**——用户打断通常意味着"方向错了"，剩下的音频再播就是错误答案的延续，比沉默更糟；②**暂停无恢复语义**——"从断点继续"要求用户显式说"继续"，比让 Agent 重答更绕（全双工产品的交互自然性优先）；③**资源释放**——丢弃即 cancel 上游 TTS/LLM 生成任务，省 token 省合成费。配套动作三件套（本项目 `realtime.py` 实现）：cancel 播放+flush 播放器缓冲、上轮发言截断记录到实际播到处（防 LLM 以为自己说完了）、新输入以"（用户打断：…）"衔接。掐断不做淡出——淡出的 200ms 会盖住用户开口的第一个字。

**12. 回声消除（AEC）为什么放在浏览器端而不是服务端做？**
答：AEC 需要**知道扬声器正在播什么**（参考信号）才能从麦克风信号里减掉它。浏览器端做：getUserMedia 的 echoCancellation 由 WebRTC 栈自动完成，参考信号（本地播放流）就在同一进程里，延迟为零——**这是标准做法**。服务端做：要先把参考信号传到服务器（一次网络往返几十 ms），回声路径变了、延迟抖动了，减不干净还烧服务器算力——吃力不讨好。所以本项目的分工：**浏览器管 AEC（采集约束 echoCancellation: true），服务端只做 AEC 失效兑底**（首次连接播 1s 测试音，VAD 误触发则提示换耳机/换浏览器）。工程原则：**能放在离信号源最近处处理的，不跨网络**。

**13. 自研管线（ASR+LLM+TTS 串联）vs 直接接 OpenAI Realtime API，怎么选？**
答：判断维度三个：①**工具与权限**——我们的 Agent 核心价值在 M03 ReAct + M04 工具 + M09 权限门 + M06 Godot 闭环；Realtime API 是封闭的语音 Agent 循环，工具要走它的 function calling、拿不到我们的 PermissionGate 确认流与 Hooks——**核心资产在自家 Loop 就必须自研编排**；②**延迟与质量**——端到端全双工模型（原生打断、超低延迟）体验上限更高，自研三段串联的打断是工程模拟——**演示/边缘场景可接 Realtime API 作对照**；③**成本与锁定**——Realtime API 按音频 token 计价昂贵且锁定厂商；自研管线的 ASR/TTS 是可替换件（本地 CosyVoice 隐私优先、云端 edge-tts 质量优先）。本项目结论：主产品自研（`voice/realtime.py`），Realtime API 作为 M02 网关的一种新 Provider 保留接入位——**与"自建网关而非直连厂商 SDK"（M02）同一条决策链**。

**14.（§9 新增）中文场景为什么不能只用 whisper？怎么平滑替换？**
答：三个数字说明问题：whisper large-v3 在 AISHELL-1 上 CER **5.14%**，WenetSpeech 会议场景高达 **18.87%**；而 FireRedASR2-AED 是 **0.57%**（平均 3.05%）、Qwen3-ASR-1.7B 是 3.76%。根因是**训练数据分布**：whisper 的 68 万小时里中文只占一小部分，是"通才"；国产模型的中文训练数据是"专才"级别的量。**对本项目的致命性在于**：口语诊断的全部数值（语速/停顿/填充词）都是转写结果的函数，CER 从 4% 涨到 18%，"填充词 23 次"这个数字本身就不可信了，**§1.3"数据与内容矛盾以数据为准"的纪律直接失效**——数据错了对不上的是数据。平滑替换靠 `ASRBackend` 协议（§9.4.1）：**换引擎不改变 `TranscriptionResult` 契约，上层 features/diagnose 零改动**，只动 `config/voice.yaml` 的 `asr.routing`。这个抽象不是过度设计——中文 ASR 的 SOTA 在 2026 年**每半年换一次**，不做这层抽象等于每次换代都要重写特征层。另外要保留 whisper 的**英文路径**（英文仍是它的主场）与**工程生态**（faster-whisper 的 VAD/批处理/`hotwords` 仍是最好用的）。

**15.（§9 新增）端点检测为什么需要语义层？静音阈值的极限在哪？**
答：静音阈值+迟滞已经把**声学信号**能榨的都榨干了，但它永远分不开这两种情况——「我用的是……」（停顿 700ms 在想词，**没说完**）与「我用的是 Godot。」（句末停顿 700ms，**说完了**）。两者的声学特征几乎一样，区别在**语义完整性与韵律**（句末降调）。这是**物理层的极限，不是调参能解决的**。解法是叠语义层：**Smart Turn v3.2**（Whisper Tiny backbone + 线性分类头，8M 参数、int8 量化仅 8MB、CPU 10ms / 云端 65ms、23 语言含中文、BSD-2）。它**直接吃 PCM 而非文本**，所以能捕捉文本里没有的韵律线索。注意它是**串联不是替换**：Smart Turn 的输入是"一个 turn 的音频"，需要先知道 turn 从哪开始——这个仍靠 Silero VAD。**VAD 是它的前置，不是它的替代品。**

**16.（§9 新增）流式 ASR 的 AlignAtt 与 LocalAgreement 有什么区别？为什么比滑窗好？**
答：三者都是"用离线模型做流式"的**同时策略（simultaneous policy）**，区别在"什么时候敢吐字"：**AlignAtt**（IWSLT 2025 SOTA）用 encoder-decoder 的交叉注意力判断"当前解码到源音频哪一帧"，注意力一进入"危险区"（接近缓冲区末尾）就停止解码，等下一块音频——**像看着稿子念，念到稿子边缘就停**；**LocalAgreement** 取相邻两次更新输出的**最长公共前缀**作为 confirmed，其余可被推翻——**像两人各说一遍，重合部分才敢写进报告**；滑窗（本模块原方案）则是每来一块就重跑整窗再比对，**没有"解码到哪了"的概念，全靠窗口对齐猜**。滑窗三个问题：① 每窗重跑整窗 encoder，**算力浪费**；② "两窗一致"对长句**收敛慢**；③ 不做增量，延迟随窗长线性增长。SimulStreaming 实测比上一代 WhisperStreaming **快约 5 倍**。落地时保留 §1.5 铁律不变：**partial 只供热身，落史/执行工具必须等 final**——策略只是让 final 来得更早更可信，不改变"final 才可提交"这条红线。

**17.（§9 新增）整个模块最该抽象的是哪一层？为什么？**
答：不是 ASR、不是 TTS，而是**数据契约 `TranscriptionResult`**。理由：ASR/TTS/VAD/对齐/分离这五个部件在 2026 年**全部处于高速换代期**（中文 ASR 半年一换、TTS 三个月出新版、端点检测从规则到模型），但**下游的特征工程、诊断报告、工具注册、UI 是稳定的**。把稳定的东西挂在契约上、把易变的东西藏在协议后，是**依赖倒置**在这个模块的具体形态。配套两条纪律：① 契约里**必须携带置信度**（word probability / avg_logprob / compression_ratio），因为换引擎后精度会变，没有置信度就无法判断数值可不可信；② 契约里**必须携带溯源信息**（引擎名/版本/语言/语言置信度），否则报告里的数字无法复核——这与 §1.3"数值引用可回链"是同一条原则在数据层的延伸。

---

## 9. 业界对标与缺口补全（2026-08 修订）

> **本节性质**：§1~§7 是"教学版施工图纸"，本节是**开工前的选型复核**。起因是一次对标复核发现：原方案把整个模块押在 whisper 上，而**whisper 在中文场景已被国产方案拉开代差**——这会直接动摇 §1.2 全部诊断数值的可信度。本节给出差距清单、决策与新增代码落点，**§1/§2/§4/§5/§7 中被本节推翻的部分以本节为准**（各处已就地修订并标注）。

### 9.1 对标矩阵：M16 原方案 vs 2026 SOTA

| 能力 | M16 原方案 | 2026 业界最强 | 判定 |
|---|---|---|---|
| 中文 ASR | whisper large-v3 | **Qwen3-ASR / FireRedASR2 / SenseVoice** | ❌ **代差** |
| 英文 ASR | whisper large-v3 | whisper large-v3-turbo / Canary / Parakeet | ⚠️ 可优化 |
| 词级时间戳 | whisper 原生 cross-attention DTW | **WhisperX wav2vec2 CTC 强制对齐** | ❌ 精度不足 |
| 说话人分离 | 无 | pyannote 3.1 / NeMo Sortformer / cam++ | ❌ 缺失 |
| ITN / 标点 | 无 | FunASR ITN + ct-punc / FST ITN-ZH | ❌ 缺失 |
| 情感 / 音频事件 | 无 | SenseVoice-Small（一个模型全出） | ❌ 缺失 |
| 热词 / 术语 | 无 | faster-whisper `hotwords` / FunASR 热词 | ❌ 缺失 |
| 转写评估 | "目测 >95%" | jiwer + whisper_normalizer 的 CER/WER | ❌ 缺失 |
| 置信度输出 | 丢弃 | word prob / avg_logprob / compression_ratio | ❌ 缺失 |
| 流式 ASR | 环形缓冲滑窗 + 两窗一致 | **SimulStreaming：AlignAtt + LocalAgreement** | ❌ 落后一代 |
| 端点检测 | 静音阈值 + 迟滞 | **Smart Turn v3.2（音频原生语义判断）** | ❌ 落后一代 |
| TTS 本地 | CosyVoice | **Qwen3-TTS-0.6B（97ms 流式 / Apache 2.0）** | ⚠️ 可升级 |
| 音频预处理 | ffmpeg 转码 | + EBU R128 响度归一化 + 降噪 | ❌ 缺失 |
| 可观测性 | 无 | RTF / 首音时延 / VAD 裁剪率 / 幻觉率 / CER | ❌ 缺失 |
| 字幕导出 | 无 | SRT / VTT / JSON | ❌ 缺失 |
| PII 脱敏 | 无 | 正则 + LLM 复核 | ❌ 缺失 |
| 全双工编排 | 自研三环串联 | 自研 ✅ / Realtime API 对照 | ✅ 决策正确 |

### 9.2 差距清单与决策（逐项：数据 → 决策 → 落点）

#### ★ 缺口 1（致命）：中文 ASR 选型落后整整一代

**数据**（统一基准 CER%，越低越好；来源：FireRedASR2S 仓库对比表 + 各官方仓库）：

| 模型 | 参数 | AISHELL-1 | 普通话平均 | 会议场景 | 流式 | 时间戳 | 许可 |
|---|---|---|---|---|---|---|---|
| **whisper large-v3**（原方案） | 1.55B | **5.14** | — | **18.87** ❌ | ✗ | ✓ | MIT |
| **FireRedASR2-AED** | 1.1B | **0.57** | **3.05** | 4.53 | ✗ | ✓ | Apache 2.0 |
| FireRedASR2-LLM | 8.3B | 0.64 | **2.89** | 4.32 | ✗ | ✓ | Apache 2.0 |
| **Qwen3-ASR-1.7B** | 1.7B | 1.48 | 3.76 | 5.88 | **✓** | ✓ | Apache 2.0 |
| Qwen3-ASR-0.6B | 0.6B | — | ~4.5 | — | **✓** | ✓ | Apache 2.0 |
| SenseVoice-Small | 234M | ~3.0 | — | — | ✗ | ✓ | Apache 2.0 |
| Paraformer-zh | 220M | 1.95 | — | — | **✓** | ✓ | MIT |

**大白话**：whisper 是个"见过 99 种语言"的通才，中文只是它的 1/99；国产模型是"只练中文"的专才，训练数据里中文占绝对多数。通才的多语言能力和专才的单语精度，在 2026 年已经是**两个数量级的差距**（会议场景 18.87% vs 4.53%——**whisper 每五个字错一个**）。

**为什么这条对本项目是致命的**：M16 的三条产品线里，"口语诊断"的全部数值（语速/停顿/填充词）都建立在转写结果上。转写 CER 从 4% 涨到 18%，**"填充词 23 次"这个数字本身就不可信了**——要么漏检（转写丢了"就是"），要么误检（转写把"就"幻觉成"就是"）。§1.3 说"数据与内容矛盾以数据为准"，可数据本身错了，这条纪律就失效了。

**决策**：**采纳**。抽象出 `ASRBackend` 协议，按语言路由。**中文默认引擎定为
SenseVoice-Small**（2026-08 落地时选定）：

| 场景 | 引擎 | 理由 |
|---|---|---|
| **中文（默认）** | **SenseVoice-Small** | CER ~3.0%、10s 音频仅 70ms（比 Whisper-Large 快 15 倍）、Apache 2.0 可商用。**并且一个模型同时出文本 + 情感 + 音频事件**——后两项是白送的副语言层维度（见 §9.2 缺口 5），对口语诊断的价值超过再降 1 个点的 CER |
| 中文（极限精度·离线） | FireRedASR2-AED | AISHELL-1 CER 0.57%。需 GPU、无流式、**无情感**，只在纯转写精度优先时切 |
| 中文（原生流式） | Qwen3-ASR-0.6B | RTF 0.00923，实时对话产品线若要自研流式可切它 |
| 粤语 / 日 / 韩 | SenseVoice-Small | SenseVoice 明确支持 yue/ja/ko |
| 英文 / 多语 | **faster-whisper large-v3-turbo** | 英文仍是 whisper 主场，turbo 版速度与精度平衡 |
| 端侧 / 隐私 | Paraformer-zh via sherpa-onnx | 220M，可跑 RPi/手机 |

**为什么选 SenseVoice 而不是精度最高的 FireRedASR**：三条产品线里"口语诊断"是
本模块的核心价值，它要的不只是"转得准"，还要"能诊断出东西"。FireRedASR 多给
2.4 个点的 CER，但 SenseVoice 白送的情感 + 事件维度是**从 0 到 1**——原方案三个
维度（流利度/填充词/结构）全在文本层，面试官最在意的紧张度/自信度恰恰藏在韵律
里，文本看不出来。**在指标已经够用的前提下，补一个缺失的维度比再优化一个已有
维度更有价值。**

**落点**：`voice/stt.py` 的 `FunASRBackend` + `config/voice.yaml` 的 `asr.routing`。

> **保留 whisper 的什么**：whisper 的**工程生态**（faster-whisper 的 VAD/批处理/
> 词时间戳/`hotwords`）仍是最好用的，英文路径继续用它；且 `TranscriptionResult`
> 数据契约不变——**换引擎不改上层**，这正是要抽象的原因。

#### 缺口 2：词级时间戳精度不足 → 引入强制对齐层

**业界方案**：WhisperX 用 **wav2vec2 + CTC** 做强制对齐（forced alignment），把 utterance 级时间戳精修到词级甚至字符级。

**关键实现细节**（来自 WhisperX `alignment.py`）：
- CTC 建 trellis（`(帧数 × token数)` 的 2D 张量）→ beam search 回溯（默认 `beam_width=2`）→ `merge_repeats` 合并重复 token
- **非空格语言（中/日）特殊处理**：`per_word = text`（每个字符当作一个 word），不做空格→`|` 转换，聚合时用 `"".join()`
- 词表外字符用 `*` 占位，`get_wildcard_emission()` 赋最大非 blank 发射概率——**中文夹杂英文/数字也不会崩**
- 对齐失败（字符表空 / 超音频长度 / 回溯失败）→ **回退原始 ASR 时间戳**，管线不断

**为什么必须做**：§1.2 的全部指标都是时间戳的函数。whisper 原生的 cross-attention DTW 对齐是**副产品**（注意力权重是训练出来的，不是为对齐优化的），CTC 强制对齐是**专门任务**——对中文逐字对齐尤其明显。

**决策**：**采纳**，作为**可插拔中间层**（默认开，失败自动回退）。落点 `voice/align.py`（新增）。

#### 缺口 3：说话人分离（多人场景完全不可用）

> **2026-08 落地更新**：SenseVoice 白送的情感与事件已通过 `Seg.emotion` /
> `Seg.events` 进入契约，并在 `to_diagnosis_input()` 里以"情感分布"字段喂给
> LLM（见 `test_sensevoice.py::test_emotion_flows_into_diagnosis_input`）。
> 也就是说这项**不需要额外部署就已经拿到了**——因为中文默认引擎就是 SenseVoice。

**决策**：**采纳**，但作为**可选能力**（单人口语诊断不必付这份算力）。
- 中文管线：`FunASR cam++`（与 ASR 同工具包，零额外部署）
- 通用/英文：`pyannote 3.1`（纯 PyTorch 重写，去掉了 onnxruntime 依赖）
- **只做 diarization（SPEAKER_00/01 编号），不做 speaker identification（身份识别）**——声纹是生物特征，与 §1.3 的隐私从紧原则冲突。

**落点**：`voice/diarize.py`（新增）。

#### 缺口 4：ITN 与标点恢复（完全缺失，直接影响字数统计）

**问题**：中文 ASR 输出"百分之三十"还是"30%"、"二零二六年"还是"2026年"，会**直接改变 §1.2 语速的分子**（"2026" 是 4 个字符还是 1 个词？）。不归一化，语速指标在不同音频间不可比。

**决策**：**采纳（必做）**。落点 `voice/normalize.py`（新增）。

#### 缺口 5：情感与音频事件（SenseVoice 白送的维度）

SenseVoice-Small 一个模型同时输出 **ASR + 情感（HAPPY/SAD/ANGRY/NEUTRAL...）+ 音频事件（BGM/Applause/Laughter/Cough/Sneeze）**，234M 参数，10 秒音频 70ms。

**为什么对"口语诊断"是降维打击**：原方案的诊断维度只有"流利度/填充词/结构"三项，全是**文本层**的。情感标签直接给出**副语言层**的"紧张度/自信度"——面试官最在意的恰恰是文本看不出来的东西。而且这是**复用同一个 ASR 调用**的输出，边际成本几乎为零。

**决策**：**采纳**。在 `TranscriptionResult` 上扩展 `emotion` / `events` 字段，并在 `to_diagnosis_input()` 里进 prompt。

#### 缺口 6~8：热词 / 评估 / 置信度

| 缺口 | 决策 | 要点 |
|---|---|---|
| **热词** | 采纳 | `hotwords="Godot, GDScript, Tween, player.gd"`。本项目专有名词多（Godot 生态），不注入热词转写必错；faster-whisper 的 hotwords 是**每个窗口都注入**的，比"沿用前文"安全 |
| **评估** | 采纳 | 用 `jiwer` 算 CER/WER，**必须先过 `whisper_normalizer`（英文）/ ITN（中文）再算**，否则标点/数字/全半角差异会污染指标。复用 M22 GodotBench 的评估框架（L1 结构 / L2 运行 / L3 裁判三层已有） |
| **置信度** | 采纳 | `Word.probability`、`Segment.avg_logprob / compression_ratio / no_speech_prob` 全部落库。**低置信要标注而非静默**——诊断报告里"该段置信度低，数值仅供参考"比给个错数字强 |

#### 缺口 9：流式 ASR 升级为 AlignAtt / LocalAgreement

**原方案的问题**：`WindowedWhisperTranscriber` = 环形缓冲滑窗 + "连续两窗一致判 final"。这是**最朴素**的做法，三大问题：① 每个滑窗都要重跑整窗 encoder（算力浪费）；② "两窗一致"对长句收敛慢；③ 没有"解码到哪里了"的概念，完全靠窗口对齐猜。

**业界 SOTA**：SimulStreaming（UFAL，IWSLT 2025 同传赛道第一），两种策略：

| 策略 | 机制 | 大白话 |
|---|---|---|
| **AlignAtt**（首选） | 用 encoder-decoder 的交叉注意力判断"当前解码到源音频的哪一帧"；注意力一旦进入"危险区"（接近音频缓冲区末尾）就**停止解码**，等下一块音频来了再继续 | **看着稿子念**：念到稿子边缘就停，等新一页送来 |
| **LocalAgreement**（易实现） | 取**相邻两次更新输出的最长公共前缀**作为 confirmed，其余是 unconfirmed（可被推翻） | **两次说法一致才算数**：两人各说一遍，重合的部分才敢写进报告 |

**关键数据**：SimulStreaming 比上一代 WhisperStreaming **快约 5 倍**；支持 VAC（Silero VAD 控制器）、beam search、`init_prompt`、`static_init_prompt`（术语不随窗口滚动）、跨 30s 窗口的上下文。

**决策**：**采纳**。落点 `voice/stream_asr.py` 重写，双策略可配（`policy: alignatt | localagreement`）。

> 注意：Simul-Whisper 的 CIF 词尾检测模型**没有 large-v3 版本**。中文场景我们本来就不用 whisper，英文用 large-v3-turbo 时需设 `never_fire` 或接受"末词总是截断"（流式场景末词本来就会被 final 修正，可接受）。

#### 缺口 10：端点检测升级为语义判断

**原方案的物理层极限**：§1.5 的静音阈值 + 迟滞 + "partial 持续变化不判端点"，已经把**静音信号**能榨的信息榨干了。但它永远分不开这两种情况：

- "我用的是……"（停顿 700ms，在想词）→ **没说完**
- "我用的是 Godot。"（句末停顿 700ms）→ **说完了**

两者的**声学特征几乎一样**（都是静音 700ms），区别在**语义和韵律**（句末有降调、语义完整）。

**业界方案**：Smart Turn v3.2（pipecat-ai，BSD-2 开源）
- **Whisper Tiny backbone + 线性分类头**，约 8M 参数
- **音频原生**：直接吃 16kHz PCM，**不经过文本**——所以能捕捉"降调/拖长音/气口"这些文本里没有的韵律线索
- **体量**：int8 量化 **8MB**（CPU 版）/ fp32 32MB（GPU 版，精度 +1%）
- **延迟**：CPU 最低 10ms，云上普遍 <100ms（Pipecat Cloud 标准实例约 65ms）
- **23 种语言含中文**
- **用法**：VAD 检测到静音后，把**整个 turn 的音频**（上限 8 秒，不足则**在前面补零**）喂给它判"说完了没"

**决策**：**采纳，两层串联**（不是替换）：

```
Silero VAD（物理层：有没有人声）  ──检测到静音──▶  Smart Turn v3.2（语义层：说完了没）
        10ms 级                                        65ms 级
           └── 任一层判"还在说" → 不交棒；两层都说完了 → 才进 THINKING
```

**为什么不是替换**：Smart Turn 的输入是"一个 turn 的音频"，它需要先知道 turn 从哪开始——这个仍要靠 VAD。**VAD 是它的前置，不是它的替代品。**

**落点**：`voice/realtime.py` 的 `_endpoint()` 增加语义层。

#### 缺口 11~15：TTS / 预处理 / 可观测 / 导出 / PII

| 缺口 | 决策 | 关键数据 / 要点 |
|---|---|---|
| **TTS** | 采纳升级 | **Qwen3-TTS-0.6B**：10 语言、3 秒零样本克隆、**97ms 流式延迟**、Apache 2.0、约 4GB VRAM → 中文场景的本地默认。Kokoro（82M / 4090 上 **210x 实时** / 54 预设音色 / Apache 2.0）作轻量备选。edge-tts 保留作云端兜底 |
| **音频预处理** | 采纳 | ffmpeg 链加 `loudnorm`（EBU R128，目标 -16 LUFS）+ 高通滤波去 DC/低频噪声。**VAD 阈值和 ASR 都对音量敏感**，不归一化会出现"同一套参数在不同录音上表现迥异" |
| **可观测性** | 采纳 | 埋 5 个指标：**RTF**（转写实时率）、**首音时延**（TTFA）、**VAD 裁剪率**（`1 - duration_after_vad/duration`）、**幻觉率**（重复段 + 低置信段占比）、**CER**（有标注时）。接入 M21 的 observability |
| **字幕导出** | 采纳 | SRT / VTT / JSON 三格式。**性价比最高的一项**——几十行代码，直接让转写结果可交付（Godot 旁白/台词场景刚需） |
| **PII 脱敏** | 采纳 | 转写文本里的手机号/身份证/邮箱/API key 正则脱敏 + 落库前执行。与 §1.3"录音不落库"同哲学：**只存必要的，且存之前先脱敏** |

#### 明确不采纳（附理由）

| 方案 | 不采纳的理由 |
|---|---|
| **Fish Audio S2 Pro** | 评测第一（EmergentTTS-Eval 81.88% 胜率），但**自托管商用需付费许可**——与"可商用、可自托管"的选型红线冲突 |
| **Chatterbox / Dia2 / VibeVoice** | 仅英文（Chatterbox/Dia2）或 research license（VibeVoice）。本项目中文为主 |
| **端到端全双工模型替换自研编排** | §1.6 已论证：工具调用必须走自家 Agent Loop + M09 权限门。保留 Realtime API 作为 M02 网关的一个 Provider 对照位 |
| **声纹身份识别** | 只做 diarization 编号。声纹是生物特征，与隐私从紧原则冲突 |

### 9.3 施工文件清单（8 → 18，另有配置文件与测试）

实际落地时做了两处必要拆分，比原表多 2 个文件：
- `schema.py` 单独成文件——**数据契约是全模块最重要的抽象**（§2 与 §7 拷打第 17 题），
  值得拥有独立文件而不是塞在 stt.py 里；
- `config.py` 单独成文件——M23 三层层加载器与数据类默认值有 350 行，混进 stt.py 会掩盖主线。

| # | 文件 | 行数 | 职责一句话 | 关键类/函数 | 状态 |
|---|---|---|---|---|---|
| 1 | `voice/__init__.py` | 55 | 包出口 + **懒导出** | `__getattr__` 按需 import 子模块 | 原 |
| 2 | `voice/schema.py` | 250 | **★ 数据契约** | `TranscriptionResult`、`Seg`、`WordInfo`、`Provenance`、`AsrDelta`、`TtsChunk`、`axis_warning()` | **新增** |
| 3 | `voice/config.py` | 352 | 配置加载（M23 三层兜底） | `VoiceConfig`、`load_voice_config`、`_apply_env` | **新增** |
| 4 | `voice/preprocess.py` | 176 | 转码 + EBU R128 响度归一 | `to_16k_mono`、`load_audio`、`resample`、`peak_normalize` | **新增** |
| 5 | `voice/vad.py` | 272 | 静音切割（silero + 能量法） | `vad_segments`、`energy_vad_segments`、`TimelineMapper` | 原（**重写**） |
| 6 | `voice/stt.py` | 470 | **多引擎 ASR 抽象与编排** | `ASRBackend`、`WhisperBackend`、`FunASRBackend`、`RemoteASRBackend`、`MockBackend`、`Transcriber` | **改** |
| 7 | `voice/align.py` | 240 | wav2vec2 CTC 强制对齐 | `align_words`、`_get_trellis`、`_backtrack` | **新增** |
| 8 | `voice/normalize.py` | 286 | ITN / 标点 / PII / 匹配归一 | `itn`、`cn2num`、`apply_itn_to_segment`、`redact_result` | **新增** |
| 9 | `voice/diarize.py` | 112 | 说话人分离（可选） | `diarize`、`assign_speakers` | **新增** |
| 10 | `voice/features.py` | 335 | 时间戳 → 诊断指标 | `pauses`、`speech_rate`、`count_units`、`count_fillers`、`extract_features` | 原（**修正**） |
| 11 | `voice/diagnose.py` | 156 | LLM 诊断报告（长转写分段） | `diagnose`、`verify_no_number_drift` | **新增** |
| 12 | `voice/stream_asr.py` | 264 | 流式转写 + 双策略 | `LocalAgreementPolicy`、`AlignAttPolicy`、`LocalStreamTranscriber` | **改** |
| 13 | `voice/turn.py` | 163 | 端点检测（物理 + 语义） | `HysteresisTurnDetector`、`SmartTurnV3`、`TwoStageTurnDetector` | **新增** |
| 14 | `voice/tts.py` | 332 | 流式合成（分句 + 三后端） | `SentenceSplitter`、`pronounce_code`、`Edge/Qwen/Kokoro/Mock`Synthesizer | **改** |
| 15 | `voice/realtime.py` | 344 | 全双工会话状态机 | `RealtimeSession`、`RealtimeState`、`energy_vad_prob` | **改** |
| 16 | `voice/export.py` | 105 | SRT / VTT / JSON 导出 | `to_srt`、`to_vtt`、`to_json` | **新增** |
| 17 | `voice/metrics.py` | 153 | 可观测埋点 + CER | `record`、`ttfa_timer`、`cer` | **新增** |
| 18 | `voice/tools.py` | 185 | M04 工具注册 | `TranscribeTool`、`AnalyzeSpeechTool` | **新增** |
| 19 | `config/voice.yaml` | 115 | 配置面（引擎/阈值/热词/音色/隐私） | — | **新增** |
| 20 | `tests/test_voice/` | 1450 | 9 个测试文件 / **104 条用例** | 见 §9.8 | **新增** |
| — | `lab/m16/` | — | 自录样本 + 人工标注（**CER 标注集**） | — | **待补**（需要真人录音） |

### 9.4 关键新增设计（代码骨架）

#### 9.4.1 多引擎 ASR 抽象

```python
# voice/stt.py
class ASRBackend(ABC):
    """声学模型是可替换件——这是 M16 最重要的一个抽象。
    换引擎不得改变 TranscriptionResult 契约，上层 features/diagnose 零改动。"""
    name: str
    supports_streaming: bool
    supports_emotion: bool

    @abstractmethod
    async def transcribe(self, wav: Path, *, lang: str | None,
                         hotwords: list[str]) -> TranscriptionResult: ...

class WhisperBackend(ASRBackend):        # faster-whisper，英文/多语主力
    name, supports_streaming, supports_emotion = "whisper", False, False
class QwenAsrBackend(ASRBackend):        # 中文主力，原生流式
    name, supports_streaming, supports_emotion = "qwen3-asr", True, False
class FireRedBackend(ASRBackend):        # 中文高精（离线诊断用）
    name, supports_streaming, supports_emotion = "firered", False, False
class SenseVoiceBackend(ASRBackend):     # 中文 + 情感 + 音频事件
    name, supports_streaming, supports_emotion = "sensevoice", False, True

class Transcriber:
    """按语言路由 + M02 韧性管道（第四次复用：LLM/embedding/whisper/ASR）。"""
    def __init__(self, backends: dict[str, ASRBackend], routing: dict[str, str],
                 quality_routing: dict[str, str] | None = None):
        self.backends, self.routing = backends, routing
        self.quality_routing = quality_routing or {}

    def pick(self, lang_hint: str | None, *, high_accuracy: bool) -> ASRBackend:
        table = self.quality_routing if high_accuracy else self.routing
        return self.backends[table.get(lang_hint or "", table["default"])]

    @with_retry(max_retries=2)
    async def transcribe(self, audio: Path, *, lang: str | None = None,
                         high_accuracy: bool = False) -> TranscriptionResult:
        wav = await preprocess.to_16k_mono(audio)          # ⑨ 归一化前置
        backend = self.pick(lang, high_accuracy=high_accuracy)
        r = await backend.transcribe(wav, lang=lang, hotwords=self.hotwords)
        r = align.align_words(r, wav, lang=r.language)      # ⑩ 强制对齐
        r = normalize.apply(r, lang=r.language)             # ⑪ ITN + 标点 + PII
        return r
```

#### 9.4.2 强制对齐（可插拔，失败回退）

```python
# voice/align.py
def align_words(tr: TranscriptionResult, wav: Path, lang: str) -> TranscriptionResult:
    """wav2vec2 + CTC 强制对齐：把词级时间戳精修到字符级精度。
    中文按字符对齐（非空格语言）；对齐失败回退原始时间戳，管线不断。"""
    if not cfg.align.enabled:
        return tr
    try:
        model, meta = load_align_model(lang)     # torchaudio 优先，HF 兜底
        for seg in tr.segments:
            aligned = _ctc_align(seg, wav, model, meta)
            if aligned:
                seg.words = aligned
                seg.start, seg.end = aligned[0].start, aligned[-1].end
    except Exception as e:                        # ★ 回退而非抛出
        logger.warning("align failed, fallback to ASR timestamps: %s", e)
    return tr
```

> **与 §1.1 坑 1 的关系**：强制对齐必须在**时间戳已还原到原始音频轴之后**做。若先对齐再还原，或还原漏了 pad，CTC 会对着错误的音频区间硬对齐——**宁可不对齐，也不要错对齐**。

#### 9.4.3 语义端点检测（物理层 + 语义层串联）

```python
# voice/realtime.py
async def _endpoint(self, vad_prob: float, partial_changed: bool,
                    turn_pcm: bytes) -> bool:
    # ① 物理层：静音迟滞（原 §1.5 逻辑，保留）
    if not self._silence_hysteresis(vad_prob, partial_changed):
        return False
    # ② 语义层：Smart Turn v3.2 判断"这句语义完不完整"
    if self.turn_detector and cfg.realtime.semantic_endpoint:
        return await self.turn_detector.is_complete(
            _pad_to_8s(turn_pcm))       # 不足 8s 在**前面**补零（官方要求）
    return True
```

#### 9.4.4 流式 ASR：LocalAgreement（AlignAtt 的简化 fallback）

```python
# voice/stream_asr.py
class LocalAgreementPolicy:
    """取相邻两次更新输出的最长公共前缀作为 confirmed，其余可被推翻。
    SimulStreaming 论文结论：AlignAtt 质量最好，LocalAgreement 次优但易实现。"""
    def __init__(self):
        self.prev: list[str] = []
        self.confirmed: list[str] = []

    def update(self, tokens: list[str]) -> tuple[str, str]:
        n = 0
        for a, b in zip(self.prev, tokens):
            if a != b:
                break
            n += 1
        new = tokens[:n]
        self.confirmed.extend(new[len(self.confirmed):])
        self.prev = tokens
        return "".join(self.confirmed), "".join(tokens[len(self.confirmed):])  # (final, partial)
```

> **§1.5 铁律不变**：partial 只供热身，**落史 / 执行工具必须等 final**。LocalAgreement 只是让 final 来得更早、更可信。

### 9.5 `config/voice.yaml`（配置面，遵循 M23 三层兜底）

```yaml
asr:
  # 中文默认：SenseVoice-Small（~3.0% CER / 10s 音频 70ms / Apache 2.0）
  # 附带白送情感识别 + 音频事件检测，见 §9.2 缺口 5
  default: sensevoice
  routing:
    zh: sensevoice
    yue: sensevoice        # 粤语
    ja: sensevoice
    ko: sensevoice
    en: whisper            # 英文仍是 whisper 主场
    default: sensevoice
  quality_routing:         # high_accuracy=True 时
    zh: sensevoice         # 极限精度可改 firered2-aed（CER 0.57%，但无情感）
    default: whisper
  engines:
    sensevoice:
      model: iic/SenseVoiceSmall
      device: cuda:0
      vad_model: fsmn-vad          # ★ 单次只吃 ≤30s，长音频必须挂 VAD 切段
      punc_model: ct-punc          # ★ SenseVoice 输出不带标点
      output_timestamp: true       # 字级时间戳（毫秒）——§1.2 全部指标的地基
      ban_emo_unk: false
  hotwords: [Godot, GDScript, Tween, Node2D, player.gd]   # 每窗口注入，比"沿用前文"安全
  condition_on_previous_text: false # 防跨段幻觉传播（§1.1 坑 3）
  hallucination_silence_threshold: 2.0   # 需 word_timestamps=true
vad:
  min_silence_duration_ms: 500      # 默认 2000 太钝
  speech_pad_ms: 200                # 默认 400；段两侧各留，防切字
align:
  enabled: true                     # wav2vec2 CTC 强制对齐，失败自动回退
  beam_width: 2
normalize:
  itn: true                         # 中文数字→阿拉伯数字，直接影响力语速分子
  restore_punctuation: true
  redact_pii: true
diarize:
  enabled: false                    # 会议场景才开，单人口语诊断不必付这份算力
  backend: cam++                    # 中文 / pyannote-3.1（英文）
features:
  pause_thresholds: {thinking: 0.8, stuck: 2.0}   # 来源：中文口语研究常用阈值
  filler_words: [就是, 然后, 那个, 嗯, 呃, 这个, like, um, uh]
  filler_normal_rate: [0.05, 0.08]  # 中文口语正常区间
  speech_rate_ref:
    zh: {unit: 字/分, slow: 140, normal: [180, 240], fast: 300}   # 讲解 200~260 / 播音 240~280
    en: {unit: 词/分, slow: 110, normal: [130, 170], fast: 190}   # 对话 130~150 / 演讲 150~170
  en_word_to_zh_char: 1.5           # 混说折算系数（经验值：210/150≈1.4，取 1.5）
tts:
  default: qwen3-tts-0.6b           # 97ms 流式 / 10 语言 / Apache 2.0
  fallbacks: [kokoro-82m, edge-tts]
  voice: zh-CN-XiaoxiaoNeural
  sample_rate: 24000                # ★ 合成/播放/录音三处必须一致
realtime:
  semantic_endpoint: true           # Smart Turn v3.2（8MB / int8 / 65ms / BSD-2）
  endpoint_silence_ms: 500
  barge_in_ms: 200
  asr_policy: alignatt              # alignatt | localagreement
privacy:
  keep_audio: false                 # 录音不落库，只存转写与特征（§1.3）
  retain_days: 7
```

### 9.6 测试落点

新增的 11 条测试**已全部并入 §5**（保持"测试只在 §5 一处"的单一事实源），此处不重复列出。分组是：

| 组 | 覆盖 | 条数 |
|---|---|---|
| 时间戳正确性 | `test_vad_offset_restores_pauses` / `test_no_double_pad` / `test_timestamps_sane` | 3 |
| 指标正确性 | `test_units_follow_detected_language`（+ §5 原有的语速/填充词/停顿三条） | 1 + 3 |
| 引擎与质量 | `test_cer_regression` / `test_align_fallback_never_raises` / `test_hotwords_injected_every_window` | 3 |
| 实时链路 | `test_semantic_endpoint_beats_silence_only` | 1 |
| 合规与可观测 | `test_diarize_labels_are_anonymous` / `test_pii_redacted_before_persist` / `test_rtf_and_ttfa_recorded` | 3 |

**唯一需要额外补的**（§5 未列，属于数据侧而非代码侧）：`test_itn_changes_speech_rate_numerator`——「百分之三十」ITN 后变「30%」，字数从 5 变 3，断言分子变化**可解释且全文口径一致**。

### 9.7 验收标准升级（MI-5）

| 指标 | 原标准 | **修订后** |
|---|---|---|
| 中文转写质量 | "目测 >95% 准" ❌ | **CER < 8%（自录标注集）+ 对照 whisper 基线并记录差值** |
| 时间戳精度 | 无 | **词边界与人工标注偏差中位数 < 120ms** |
| 语速数值 | 出数即可 | **与人工掐表对拍误差 < 10%** |
| 首音时延 | < 1.5s | **< 1.0s**（见下方预算表） |
| 打断静音 | < 300ms | < 300ms（不变） |
| 交付物 | 转写 + 报告 | **+ SRT/VTT 字幕 + 特征 JSON + CER 报告** |

**修订后的延迟预算表**（§1.5 的对拍数字更新版）：

| 段 | 原预算 | **修订后** | 变化来源 |
|---|---|---|---|
| 端点检测 | 100~300ms | **100~200ms** | 物理迟滞 + Smart Turn 65ms 并行跑，取"两者都判完" |
| 流式 ASR 尾块 | 200~400ms | **150~300ms** | AlignAtt 策略（Qwen3-ASR 原生流式，非滑窗重跑） |
| LLM 首 token | 300~800ms | 300~800ms | 不变（M02 网关指标直接复用） |
| TTS 首音 | 200~400ms | **~97ms** | **Qwen3-TTS-0.6B**（原 edge-tts 有网络往返） |
| 播放启动 | 50ms | 50ms | 不变 |
| **合计** | 0.85~1.95s | **0.70~1.45s** | 重叠后目标 **< 1.0s** |

> **铁律不变（§1.5）**：**先调度后模型**——流水线重叠是零模型成本的优化，必须先把调度压到极致，再考虑换更快的模型。上表里 TTS 那一项是唯一靠"换模型"拿到的收益（-200ms），其余全是调度与策略的改进。

### 9.8 落地清单与验证（2026-08 完工）

#### 依赖策略：零重依赖可跑，重模型按需升级

本项目环境**没有** torch / faster-whisper / funasr / edge-tts / ffmpeg。所以实现上
定了一条纪律：**顶层导入零重依赖**（只用标准库 + numpy），所有声学依赖在函数内懒导入。

| 能力 | 无依赖时 | 装了依赖后 |
|---|---|---|
| VAD | `energy_vad_segments`（自适应能量法，纯 numpy） | 自动切 Silero（`backend=auto`） |
| ASR | `MockBackend`（确定性合成时间戳） | **SenseVoice**（中文默认）/ Whisper（英文）/ Qwen3-ASR / FireRed |
| 强制对齐 | 原样返回 + `provenance.aligned=False` | wav2vec2 CTC 精修 |
| TTS | `MockSynthesizer`（等长静音块） | edge-tts / Qwen3-TTS / Kokoro |
| 端点检测 | 纯迟滞（物理层） | + Smart Turn v3.2（语义层） |
| 音频解码 | 标准库 `wave`（仅 PCM wav） | ffmpeg（mp3/m4a/flac…）+ 响度归一 |

好处：**CI 上不需要 GPU 也能跑通全链路**，且降级路径本身也是生产环境的容错路径。

#### 测试分组（85 条，全部通过）

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `test_timestamp.py` | 8 | **§1.1 ⑤a**：VAD 偏移还原、pad 不多减、拼接轴抹平停顿、轴诊断 |
| `test_features.py` | 14 | **§1.2**：语速分母/分子、停顿分类、填充词、节奏、语言阈值 |
| `test_normalize.py` | 24 | ITN / 中文数字（参数化）/ 标点恢复 / PII（参数化）/ CER |
| `test_pipeline.py` | 17 | VAD 段、端到端转写、配置三层兜底、导出、工具注册 |
| `test_realtime.py` | 14 | 端点迟滞、语义层、打断三件套、TTFA、LocalAgreement |
| `test_cli.py` | 8 | CLI 三条产品线端到端 |
| `test_sensevoice.py` | 19 | 中文默认引擎：`<\|TAG\|>` 情感/事件抽取、毫秒时间戳、多段 VAD 输出、参数分流 |
| **合计** | **104** | 另有项目整体回归 **572 passed** |

跑法：`cd backend && python -m pytest tests/test_voice -q`（约 0.5 秒）。

#### 实现过程中发现并修掉的 6 个真 bug

这些是"写之前没想到、跑测试才暴露"的问题，值得记进 §6 踩坑记录：

| # | 问题 | 现象 | 根因 | 解法 |
|---|---|---|---|---|
| 1 | **ITN 对中文形同虚设** | 「百分之三十」永远不转 | 逐词调 `itn()`，而中文词级时间戳是**逐字符**的，单字长度 < 2 → 永不转换 | 改为在**段的字符序列**上做，按替换区间合并 token 并重算时间范围（`apply_itn_to_segment`） |
| 2 | **TTS 无法消费异步句子流** | `TypeError: async_generator not iterable` | 合成器里 `list(sentences)` 展开异步生成器 | 加 `_iter_sentences()` 惰性消费——**不能 `list()` 收集，否则流式退化成"等 LLM 说完才合成"** |
| 3 | **LLM 生产者异常 → 会话静默卡死** | `voice chat` 无响应、无超时 | 生产者抛异常后没投递结束哨兵，消费者在 `queue.get()` 上永久阻塞 | 哨兵放进 `finally` 无条件投递；异常存下来在 `await producer` 后重新抛出 |
| 4 | **流式转录算力 O(n²)** | 长音频越跑越慢 | `_drain()` 后没有把计时器归零 → 过了首个 `min_chunk_s` 后每帧重跑整段缓冲 | drain 后 `self._elapsed = 0` |
| 5 | **测试音频全是静音** | 端点检测怎么调都不触发 | float → int16 直接 `astype` 会把 0.4 **截断**成 0 | 必须先 `* 32767` 再转换 |
| 6 | **英文 CER 退化成字符错率** | `cer("hello world","hello")` 得 1.0 | 归一化把空格也删了，英文就没法按词切 | CER 归一化按语言分支：中文去空白，**英文保留空格**（只去标点 + 小写） |

#### 切到 SenseVoice 时新暴露的 3 个 bug

| # | 问题 | 现象 | 根因 | 解法 |
|---|---|---|---|---|
| 7 | **情感/事件标签漏抽** | `<\|HAPPY\|>` 抽到了，`<\|Speech\|><\|Applause\|>` 原样留在正文 | 标签正则写成 `[A-Z_]+`，而 SenseVoice 的事件标签是**大小写混合**（Speech / Applause / Laughter / Cough / Sneeze / Breath / Cry / BGM），只有全大写的情感标签能匹配 | 正则改 `[A-Za-z_]+` |
| 8 | **长音频只转前 30 秒** | 3 分钟录音输出寥寥数句，**不报错** | 挂了 `fsmn-vad` 后 FunASR 返回**多段列表**，而解析只取 `res[0]`，其余静默丢弃 | 遍历全部 item，每段各建一个 `Seg` |
| 9 | **FunASR 参数混传** | `output_timestamp` 完全不生效 | 构造参数（`vad_model`/`punc_model`/`device`）与推理参数（`output_timestamp`/`ban_emo_unk`/`merge_*`）混在一起传。塞进 `AutoModel()` **不报错但静默失效**——比报错更难查 | 按白名单分两拨：`_FUNASR_MODEL_KWARGS` / `_FUNASR_INFER_KWARGS` |

另外记一条**配置纪律**（不算 bug，但漏了会静默劣化）：SenseVoice 有两条硬约束
必须在配置里体现，否则不报错只是悄悄变差——
① 单次只吃 ≤30s 音频 → 必须挂 `vad_model: fsmn-vad`；
② 输出**不带标点** → 必须挂 `punc_model: ct-punc`，否则停顿判断与 LLM 读到的
转写都是一坨连读。这两条已写进 `config/voice.yaml` 并有测试守卫
（`test_sensevoice_config_has_vad_and_punc`）。

#### 尚需人工完成的两项（代码已留好接口）

1. **`lab/m16/` 自录样本与人工标注**——CER 回归目前用的是"Mock 引擎对拍"（确定性，无意义
   但能防劣化）。真实验收需要录 3 分钟中文并逐字标注，放进 `lab/m16/`，
   然后 `test_cer_regression` 的 `ref` 换成真实标注文本。
2. **安装 funasr**——路由已经指向 `sensevoice`，但环境还没装引擎，目前跑的是
   `MockBackend`。执行 `uv pip install funasr`（GPU 另需 torch）后**无需改任何
   代码**即可切到真实引擎——这正是 §9.4.1 抽象的目的。想要极限精度就把
   `quality_routing.zh` 改成 `firered2-aed`（代价：无情感、需 GPU、无流式）。

---

## 10. 教程映射与延伸

- 📘 zero2Agent 10 课（stt & speech）
- 必读：whisper 论文（数据规模化思想）；faster-whisper README（性能对照）
- **必读（§9 对标后新增）**：
  - **Simul-Whisper 论文（arXiv 2406.10052）**——AlignAtt 用交叉注意力决定"解码停在哪"，这是流式 ASR 的核心思想
  - **WhisperX `alignment.py`**——CTC trellis + beam backtrack，以及中文"逐字符对齐"的非空格语言分支
  - **Smart Turn v3.2**——音频原生的语义端点检测，理解"静音阈值"的物理层极限在哪
  - **FireRedASR2S 仓库对比表**——中文 ASR 统一基准 CER，看 whisper 在中文上落后多少
- 必读（实时对话）：OpenAI Realtime API 文档（对标形态与打断语义）；WebRTC echoCancellation（AEC 采集约束）
- 选读：Silero-VAD 文档；SenseVoice（中文替代对比，**含情感识别**）；Qwen3-ASR / Qwen3-TTS；Kokoro（82M 轻量 TTS）；pyannote 3.1（说话人分离）；Gemini Live API（端到端全双工对照）
