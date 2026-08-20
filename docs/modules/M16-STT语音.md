# M16 STT 与语音分析（whisper · VAD · 口语诊断）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 11 · 里程碑 MI-5「语音」 |
| 代码落点 | `backend/agent_godot/voice/`（3 个文件，见 §0.5） |
| 前置模块 | M02（whisper 服务接入复用网关韧性管道）· M04（transcribe/analyze 注册为工具） |
| 手写比例 | 接入层与应用层 100% 手写；声学模型（whisper）只用不写 |
| 教程映射 | 📘 zero2Agent 10 课 · 📝笔记 STT/语音 · faster-whisper 文档 |

---

## 0. 本模块在项目中的位置

**大白话**：给 Agent 装**耳朵**。两条产品线：①**语音输入**——对麦克风说"给玩家加双跳"，转文字进 Query Engine（M12）走原管线（语音只是另一个入口，后面全部复用）；②**口语诊断**——分析录音的语速/停顿/填充词产出报告（面试练习场景移植；Godot 侧用于录制旁白/台词质检）。核心管线一句话：**先切后转再算**——VAD 切出"有人说话的段"（省算力+防幻觉）→ whisper 转写（附带词级时间戳）→ 从时间戳**算特征**（语速/停顿/填充词，零额外模型）→ LLM 综合诊断。

**交付后状态**：`voice transcribe interview.wav` 出带时间戳转写；`voice analyze interview.wav` 出诊断报告（语速 182 字/分、填充词"就是"×23、最长停顿 4.2s@08:31…）；聊天里语音消息自动转文字。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 5 个文件**：

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `voice/__init__.py` | 空包 | — | 1 | 步骤 0 | — |
| 2 | `voice/vad.py` | 静音切割 | `vad_segments` | 40 | 步骤 1 | silero |
| 3 | `voice/stt.py` | whisper 接入（本地+远程双实现） | `Transcriber`、`LocalTranscriber`、`RemoteTranscriber` | 90 | 步骤 2 | faster-whisper |
| 4 | `voice/features.py` | 时间戳→特征指标 | `extract_features`、`pauses`、`speech_rate` | 80 | 步骤 3 | stt |
| 5 | `lab/m16/` | 自录样本+人工标注 | — | — | 步骤 1 前置 | 麦克风 |

**完成后你拥有**：两个工具注册（TranscribeTool/AnalyzeSpeechTool）；诊断报告数值可回链。

---

## 1. 知识点详解（每节五段：定义 → 大白话 · 举例 · 演进 · 易错点）

### 1.1 语音转文字管线（VAD → ASR）

**① 严格定义**：**VAD**（Voice Activity Detection）先切"有人说话的段"再送 ASR——Silero-VAD 逐帧输出语音概率，按阈值+最短段长+最短间隔合并成段。**whisper 三合一输出**：转写文本＋**词/段级时间戳**（word_timestamps=True）＋语言检测——时间戳是特征工程的全部数据源。

**② 大白话**：**先分诊后化验**。把整段录音（含一半静音）直接扔给 whisper，就像把全血连血浆一起化验——又贵又容易被污染：whisper 对长静音会**幻觉出内容**（训练数据来自 YouTube 字幕，静音段它爱"脑补"片尾的"谢谢观看"——著名工程坑）。VAD 是分诊护士：把"确实在说话"的段挑出来，段前后各补 200ms 防切字，化验（ASR）又快又准。

**③ 举例**：

```python
model = WhisperModel("large-v3", device="cuda", compute_type="float16")
segments, info = model.transcribe(
    path, language="zh", vad_filter=True, vad_parameters=dict(
        min_silence_duration_ms=500,     # 静音>500ms 才切段
        speech_pad_ms=200),              # 段前后各补 200ms
    word_timestamps=True, condition_on_previous_text=False)  # 防前文幻觉传播
```

chunk 策略：VAD 段 >30s 再切（whisper 窗口 30s）；段间 0.5s 重叠防切词。

**④ 演进**：HMM-GMM（需发音词典，逐字建模）→ E2E 神经 ASR（DeepSpeech/Conformer）→ **whisper**（2022：68 万小时弱监督，多语言+零样本鲁棒性革命）→ faster-whisper（CTranslate2 重写：4 倍速+显存减半）→ SenseVoice/Paraformer（中文更优可替换）。工程锚点：**声学模型是可替换件，我们只写管线与后处理**。

**⑤ 易错点**：
- word_timestamps 与 vad_filter 同开时要重对齐（段时间戳是 VAD 后的，要加回 pad）
- 中文语速"字/分"、英文"词/分"——先语言检测再选单位，混算出鬼数
- 幻觉双防：VAD 前置 + `condition_on_previous_text=False`（长音频防前文污染传播）

### 1.2 语音特征工程（时间戳 → 诊断指标）

**① 严格定义**：转写只是数据，特征才是洞见。三类指标全部从词级时间戳推导（零额外模型）：**流利度**（语速=有效语音时长内字数/分钟；停顿=相邻词间隙>0.8s 思考型 / >2s 卡壳型；节奏方差=语速滑动窗标准差）；**填充词**（词表匹配："就是/然后/那个/嗯/呃/like"，密度=填充词/总词数，中文口语正常 5~8%，>12% 显著）；**结构**（STAR/要点覆盖——LLM 读转写判，规则判不了）。

**② 大白话**：**从录像带出统计表**。教练回看训练录像（转写+时间戳）不是看热闹，而是掐表统计：有效说话时间多长（剔除发呆）、哪里停了 4 秒（卡壳）、"就是"说了多少次（口头禅密度）——**把主观印象变成客观数字**。方法论三步（可迁移到任何领域）：**原始信号 → 可计算指标 → 业务化解释**——"182 字/分"要翻译成"语速偏快，听众易疲劳，建议 150~170"（阈值标定来自领域知识，进配置可调）。

**③ 举例**：

```python
def pauses(words, threshold=0.8):
    return [Pause(start=a.end, duration=b.start - a.end,
                  kind="卡壳" if b.start - a.end > 2 else "思考")
            for a, b in zip(words, words[1:]) if b.start - a.end >= threshold]

def speech_rate(words, total_s, pause_s, lang):
    effective = total_s - pause_s              # ★分母剔除停顿的"纯说话时间"
    unit = len(words) if lang.startswith("en") else sum(len(w.text) for w in words)
    return unit / (effective / 60)
```

**④ 演进**：人工听录音评估（主观不可复现）→ 规则指标（客观但零散）→ 指标+LLM 综合诊断（结构化输入，报告有据）。与 RAG 引用同理：**LLM 的判断要挂在可复核的数值上**。

**⑤ 易错点**：
- 语速分母是"纯说话时间"不是音频总长——含 30s 静音的录音直接腰斩语速
- 填充词匹配要归一化（"就是就是"连说、繁简、大小写）且引述语境豁免（"他说'那个'一词"不算）
- 阈值（0.8s/2s/5~8%）进配置文件并注明数据来源——魔法数可追溯

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

---

## 2. 接口设计（完整签名）

```python
# voice/stt.py
@dataclass
class WordInfo: text: str; start: float; end: float
@dataclass
class Seg: start: float; end: float; text: str; words: list[WordInfo]
@dataclass
class TranscriptionResult: language: str; duration: float; segments: list[Seg]

class Transcriber(ABC):
    async def transcribe(self, audio_path: Path) -> TranscriptionResult: ...
class LocalTranscriber(Transcriber): ...    # faster-whisper 进程内
class RemoteTranscriber(Transcriber): ...   # whisper 服务（HTTP，复用 M02 韧性管道）

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
def extract_features(tr: TranscriptionResult, filler_words: list[str]) -> SpeechFeatures: ...
def to_diagnosis_input(f: SpeechFeatures) -> dict: ...

# 工具注册（M04）
@register_tool(readonly=True, risk="low")
class TranscribeTool(BaseTool): ...     # 音频路径 → 转写+时间戳
@register_tool(readonly=True, risk="low")
class AnalyzeSpeechTool(BaseTool): ...  # 音频路径 → 特征+诊断报告
```

---

## 3. 关键难点参考片段：whisper 服务化（GPU 复用）

模型冷加载 3~10s，每请求冷启不可接受——whisper 常驻服务（deploy/compose），接入层走 HTTP：

```python
class RemoteTranscriber(Transcriber):
    """whisper 服务（TEI 同款部署模式）。"""
    @with_retry(max_retries=2)                      # M02 韧性管道第三次复用
    async def transcribe(self, path: Path) -> TranscriptionResult:
        async with httpx.AsyncClient(timeout=600) as c:   # 长音频耗时
            r = await c.post(f"{self.base}/transcribe",
                files={"file": path.open("rb")},
                data={"language": "zh", "word_timestamps": "true"})
            r.raise_for_status()
            return TranscriptionResult.from_api(r.json())
```

为什么难：转写是**分钟级长任务**——超时、重试语义（重试会不会重复计费/重复转写）、大文件上传三件事都与 LLM 调用不同；韧性管道复用但参数要按语音特性重调。

---

## 4. 手敲指引（函数级伪代码）

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 0 | `lab/m16/` | 自录 1 分钟样本，人工标注词数/停顿位置（对拍基准） | 标注表 |
| 1 | `voice/vad.py` | `vad_segments：silero 逐帧概率→阈值判定→min_speech/min_silence 合并→返回 (start,end) 列表` | 段边界与人工听感一致 |
| 2 | `voice/stt.py` | `LocalTranscriber：§1.1 ③ 参数直跑；RemoteTranscriber：§3 代码；TranscriptionResult.from_api：JSON→dataclass（重对齐 pad）` | 转写目测 >95% 准 |
| 3 | `voice/features.py` | `pauses/speech_rate：§1.2 ③；fillers：词表归一化匹配（连续重复分开计+引述豁免）；rhythm_variance：10 词滑窗语速标准差；to_diagnosis_input：特征→JSON（附参考区间）` | 与人工标注对拍 |
| 4 | 诊断流+工具 | `AnalyzeSpeechTool.run：transcribe→extract_features→DIAGNOSE_PROMPT 调 LLM→报告+特征 JSON 一起返回；TranscribeTool 只转写` | 模型在对话中可调两工具 |
| 5 | 入口集成 | `ffmpeg 统一转码 16k mono wav；隐私开关（录音不落库）；M19 挂 POST /voice/transcribe` | 聊天发语音→自动转文字响应 |

---

## 5. 测试与验收

```python
def test_speech_rate_excludes_pauses():
    # 60s 音频含 10s 静音、100 字 → 语速 = 100/(50/60) = 120 字/分（非 100）

def test_filler_count_normalization():
    # "就是就是"计 2 次；引述"那个"不计（豁免标注）

def test_pause_kinds_threshold():
    # 0.8~2s→思考；>2s→卡壳；边界 0.8s 恰好不计
```

**验收 Demo（MI-5）**：录 3 分钟"自我介绍+项目讲解" → `voice analyze` 出报告（语速/填充词/停顿分布+LLM 建议，数值带参考区间）→ 聊天发同段语音，Agent 转写后正确执行语音里的请求（"帮我加双跳"）。

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

**10. 开放题：实时语音对话（STT→LLM→TTS 全双工）的延迟预算怎么拆？**
答：目标体感"对话级"= 首响应 <1.5s（人类对话轮换间隔约 200~500ms，AI 到 1s 内已可接受）。预算拆解（打断点优化）：VAD 断句 100~300ms（判定"说完了"：静音 400~500ms 窗口，这是最难压的——太快切断语义，等太久加延迟）；流式 ASR 尾块 200~400ms（非整段转写，边说边转只等最后一块）；LLM 首 token 300~800ms（TTFT：短提示+流式+就近推理，M02 网关的延迟指标直接复用）；TTS 首音 200~400ms（流式合成，句级返回）；播放启动 50ms。合计 0.85~1.95s——优化三板斧：VAD 自适应阈值（语义特征辅助断句）、ASR/LLM 流水线重叠（LLM 在 ASR 尾块前用增量文本预热首 token）、TTS 分句流式。体验关键不是总时延而是**首音时延**——人听到声音就开始耐心计时重置。

---

## 8. 教程映射与延伸

- 📘 zero2Agent 10 课（stt & speech）
- 必读：whisper 论文（数据规模化思想）；faster-whisper README（性能对照）
- 选读：Silero-VAD 文档；SenseVoice（中文替代对比）
