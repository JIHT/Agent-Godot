# M16 STT 与语音分析（whisper · VAD · 口语诊断 · 实时对话）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 11 · 里程碑 MI-5「语音」 |
| 代码落点 | `backend/agent_godot/voice/`（6 个文件，见 §0.5） |
| 前置模块 | M02（whisper 服务接入复用网关韧性管道）· M04（transcribe/analyze 注册为工具）· M03（实时对话的推理环复用 Agent Loop）· M19（WS 实时端点） |
| 手写比例 | 接入层与应用层 100% 手写；声学/声码模型（whisper/TTS）只用不写 |
| 教程映射 | 📘 zero2Agent 10 课 · 📝笔记 STT/语音 · faster-whisper 文档 · OpenAI Realtime API（对标形态） |

---

## 0. 本模块在项目中的位置

**大白话**：给 Agent 装**耳朵和嘴巴**。三条产品线：①**语音输入**——对麦克风说"给玩家加双跳"，转文字进 Query Engine（M12）走原管线（语音只是另一个入口，后面全部复用）；②**口语诊断**——分析录音的语速/停顿/填充词产出报告（面试练习场景移植；Godot 侧用于录制旁白/台词质检）；③**实时语音对话**——全双工：边听你说、边用语音答你、可随时打断（对标 OpenAI Realtime/Gemini Live 的产品形态；自研编排、声学模型只用不写——工具调用必须走自家 Agent Loop，这是不直接托管 Realtime API 的原因，§1.6 详解）。①②的核心管线一句话：**先切后转再算**——VAD 切出"有人说话的段"（省算力+防幻觉）→ whisper 转写（附带词级时间戳）→ 从时间戳**算特征**（语速/停顿/填充词，零额外模型）→ LLM 综合诊断。③的管线一句话：**三环重叠 + 随时清场**——流式 ASR 没说完 LLM 先热身、LLM 没说完 TTS 先开口（重叠压延迟），用户一开口立即掐断播放（打断保对话权）。

**交付后状态**：`voice transcribe interview.wav` 出带时间戳转写；`voice analyze interview.wav` 出诊断报告（语速 182 字/分、填充词"就是"×23、最长停顿 4.2s@08:31…）；聊天里语音消息自动转文字；`voice chat` 进实时对话——说"给玩家加双跳"，1.5 秒内听到语音应答且工具卡照常执行；Agent 说话中插话"改成三段跳"，300ms 内闭嘴并听懂新指令。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 8 个文件**：

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `voice/__init__.py` | 空包 | — | 1 | 步骤 0 | — |
| 2 | `voice/vad.py` | 静音切割 | `vad_segments` | 40 | 步骤 1 | silero |
| 3 | `voice/stt.py` | whisper 接入（本地+远程双实现） | `Transcriber`、`LocalTranscriber`、`RemoteTranscriber` | 90 | 步骤 2 | faster-whisper |
| 4 | `voice/features.py` | 时间戳→特征指标 | `extract_features`、`pauses`、`speech_rate` | 80 | 步骤 3 | stt |
| 5 | `lab/m16/` | 自录样本+人工标注 | — | — | 步骤 1 前置 | 麦克风 |
| 6 | `voice/stream_asr.py` | 流式转写（partial/final 增量） | `StreamingTranscriber`、`WindowedWhisperTranscriber`、`RemoteStreamTranscriber` | 80 | 步骤 6 | stt |
| 7 | `voice/tts.py` | 流式合成（分句进、音频块出） | `Synthesizer`、`RemoteSynthesizer`、`LocalSynthesizer` | 90 | 步骤 7 | edge-tts |
| 8 | `voice/realtime.py` | 全双工会话状态机（重叠流水线+打断） | `RealtimeSession`、`RealtimeState` | 130 | 步骤 8 | stream_asr+tts+M03 |

**完成后你拥有**：两个工具注册（TranscribeTool/AnalyzeSpeechTool）；诊断报告数值可回链；`voice chat` 实时对话（首音 <1.5s、打断静音 <300ms）。

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

**④ 演进**：按键说话（无检测，最可靠但反自然）→ 固定静音阈值（简单但两难：快了切句慢了拖沓）→ 迟滞+最短话长（当前）→ 语义端点（LLM 判断"这句语义完整否"，前沿）。与离线 VAD 的关系：同一个 Silero，**离线吃整段（管切得准），在线吃环形缓冲（还要管切得快）**——§1.1 的 vad_segments 是前者，端点检测是后者的在线版。

**⑤ 易错点**：
- 把 partial 当 final 用：部分结果被推翻后 LLM 已答错——**预热可以，落史/执行工具必须等 final**（铁律）
- 环形缓冲覆盖说话中的帧 → 转写开头丢字；缓冲至少 = 端点阈值 + 200ms pad（同 §1.1 的 speech_pad）
- 说话中"嗯…"犹豫 800ms 被切句——迟滞窗口内 partial 持续变化（有新词）时不判端点

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

实时对话三件套（stream_asr / tts / realtime，§1.4~1.6 的落点）：

```python
# voice/stream_asr.py —— 流式转写（增量文本：partial 可推翻 / final 定稿）
@dataclass
class AsrDelta: text: str; is_final: bool; end: float

class StreamingTranscriber(ABC):
    """音频块进、增量文本出。partial 只供热身，final 才可入史（§1.5 铁律）。"""
    async def feed(self, pcm: bytes) -> AsyncIterator[AsrDelta]: ...
class WindowedWhisperTranscriber(StreamingTranscriber): ...
# 教学版：环形缓冲滑窗喂 faster-whisper——尾窗文本仍在变=partial，连续两窗一致=final
class RemoteStreamTranscriber(StreamingTranscriber): ...   # 生产版：厂商 WS 流式 ASR

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
    async def _endpoint(self, vad_prob: float, partial_changed: bool) -> bool: ...
    # 迟滞端点检测：静音≥500ms 且 partial 无新词 且 话长≥250ms（§1.5）
    async def _pipeline(self, final_text: str) -> AsyncIterator[TtsChunk]: ...
    # partial 预热 LLM → M03 loop 流式 → 分句器（§1.4 双阈值）→ TTS 逐句
    async def _barge_in(self, mic_frame: bytes) -> bool: ...
    # SPEAKING 态监听：AEC 后仍检测到人声 ≥200ms → cancel 播放 + 丢弃未播块
    async def _truncate_last_turn(self, played_upto: int) -> None: ...
    # 打断善后：上轮 Assistant 记录截断到实际播到处（防上下文错位）
```

---

## 3. 关键难点参考片段

### 3.1 whisper 服务化（GPU 复用）

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

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 0 | `lab/m16/` | 自录 1 分钟样本，人工标注词数/停顿位置（对拍基准） | 标注表 |
| 1 | `voice/vad.py` | `vad_segments：silero 逐帧概率→阈值判定→min_speech/min_silence 合并→返回 (start,end) 列表` | 段边界与人工听感一致 |
| 2 | `voice/stt.py` | `LocalTranscriber：§1.1 ③ 参数直跑；RemoteTranscriber：§3 代码；TranscriptionResult.from_api：JSON→dataclass（重对齐 pad）` | 转写目测 >95% 准 |
| 3 | `voice/features.py` | `pauses/speech_rate：§1.2 ③；fillers：词表归一化匹配（连续重复分开计+引述豁免）；rhythm_variance：10 词滑窗语速标准差；to_diagnosis_input：特征→JSON（附参考区间）` | 与人工标注对拍 |
| 4 | 诊断流+工具 | `AnalyzeSpeechTool.run：transcribe→extract_features→DIAGNOSE_PROMPT 调 LLM→报告+特征 JSON 一起返回；TranscribeTool 只转写` | 模型在对话中可调两工具 |
| 5 | 入口集成 | `ffmpeg 统一转码 16k mono wav；隐私开关（录音不落库）；M19 挂 POST /voice/transcribe` | 聊天发语音→自动转文字响应 |
| 6 | `voice/stream_asr.py` | `WindowedWhisper：环形缓冲（端点阈值+200ms pad）→ 滑窗喂 LocalTranscriber → 尾窗两窗一致判 final；Remote 版：WS 连厂商、partial/final 事件解析` | 对拍：final 与整段转写一致；partial 推翻率 <30% |
| 7 | `voice/tts.py` | `分句器：韵律边界优先+18 字兜底；词表预替换代码符号读法；RemoteSynthesizer 逐句请求→TtsChunk（seq 递增）` | 首句首音 <400ms；seq 连续无洞 |
| 8 | `voice/realtime.py` + M19 WS | `RealtimeSession：§3.2 主循环；_endpoint 迟滞判定；_barge_in 人声 200ms 确认→cancel+flush；_truncate_last_turn 截断；M19 挂 WS /voice/realtime（M00 §12.4 说好的"必须换 WS"场景）` | 端到端首音 <1.5s；打断静音 <300ms；打断后新指令执行正确 |

---

## 5. 测试与验收

```python
def test_speech_rate_excludes_pauses():
    # 60s 音频含 10s 静音、100 字 → 语速 = 100/(50/60) = 120 字/分（非 100）

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
```

**验收 Demo（MI-5）**：录 3 分钟"自我介绍+项目讲解" → `voice analyze` 出报告（语速/填充词/停顿分布+LLM 建议，数值带参考区间）→ 聊天发同段语音，Agent 转写后正确执行语音里的请求（"帮我加双跳"）→ `voice chat` 实时对话：说"给玩家加双跳"，1.5s 内听到语音应答且工具卡照常执行；Agent 回答中插话"改成三段跳"→ 300ms 内闭嘴、听懂并改执行。

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

---

## 8. 教程映射与延伸

- 📘 zero2Agent 10 课（stt & speech）
- 必读：whisper 论文（数据规模化思想）；faster-whisper README（性能对照）
- 必读（实时对话）：OpenAI Realtime API 文档（对标形态与打断语义）；WebRTC echoCancellation（AEC 采集约束）
- 选读：Silero-VAD 文档；SenseVoice（中文替代对比）；edge-tts / CosyVoice README（TTS 双实现对照）；Gemini Live API（端到端全双工对照）
