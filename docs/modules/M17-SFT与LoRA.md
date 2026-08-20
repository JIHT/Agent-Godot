# M17 SFT 与 LoRA（监督微调 · 低秩适配 · 数据构造）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 12 · 里程碑 MI-6a「SFT/LoRA」 |
| 代码落点 | `training/datasets/`（2 文件）+ `training/sft/`（3 文件）+ `lab/m17/`（2 文件），见 §0.5 |
| 前置模块 | M01（Chat Template 也是 token）· M06（Godot 领域知识来源）· M22 预埋（评估先行） |
| 手写比例 | 教学版（LoRA 层/loss mask/数据构造）100% 手写；生产训练用 LLaMA-Factory 配方 |
| 教程映射 | 📗 hello-agents 微调章 · 📝笔记 SFT/LoRA · LLaMA-Factory 文档 |

---

## 0. 本模块在项目中的位置

**大白话**：通用模型是**通识大学毕业的员工**——什么都懂一点，但不了解你们公司的 Godot 4.3 变更和代码风格。两条培养路线：**RAG（外挂知识，M10）=给他配一个随查随用的资料库**——快、可更新，但每次都要"去查"；**微调（内化能力）=送他去岗位培训班**——把领域技能练进肌肉记忆，稳定复现不用想。判据：**知识频繁更新→RAG；风格/格式/领域能力要稳定→微调**（两条路最终配合，见面试题 10）。本模块目标：让 qwen2.5-coder-7b 在 Godot 任务上贴近 deepseek-chat（本地免费、数据不出域）。

**交付后状态**：教学版 LoRA 在玩具任务收敛；LLaMA-Factory 跑通 Godot 语料 QLoRA 微调；合并导出模型注册进 models.yaml 被网关调用——M02 适配器模式兑现"微调模型与云端模型同权"。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 8 个文件**（lab 先行吃透原理，再上生产配方）：

| # | 新建文件（完整路径） | 职责一句话 | 关键类/函数 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | `lab/m17/loss_mask.py` | 手搓 SFT 样本张量+边界审计 | `build_sft_sample`、`audit_mask` | 40 | 步骤 1 | transformers |
| 2 | `lab/m17/lora_layer.py` | 教学版 LoRA 层 | `LoRALinear` | 60 | 步骤 2 | torch |
| 3 | `training/datasets/__init__.py` 等 | 空包 | — | 2 | 步骤 0 | — |
| 4 | `training/datasets/quality.py` | 去重/过滤/配比 | `QualityFilter` | 80 | 步骤 3 | 无 |
| 5 | `training/datasets/sft_builder.py` | 三源样本构造 | `SFTBuilder` | 100 | 步骤 4 | M10/M02 |
| 6 | `training/datasets/trajectory_builder.py` | 事件流→轨迹样本 | `trajectory_to_sample` | 50 | 步骤 5 | M03 events |
| 7 | `training/sft/train_sft.py` | LLaMA-Factory 封装 | `run/sweep` | 50 | 步骤 6 | llamafactory |
| 8 | `training/sft/merge_export.py` | 合并+GGUF+注册 | `merge_lora/export_gguf/register` | 60 | 步骤 7 | llama.cpp |
| — | `training/configs/qwen25coder_godot_qlora.yaml` | 生产配方 | — | 30 | 步骤 6 | — |

**完成后你拥有**：`models.yaml` 里多一行 `local/godot-coder-sft`，网关即调。

---

## 1. 知识点详解（每节五段：定义 → 大白话 · 举例 · 演进 · 易错点）

### 1.1 SFT 的本质：条件概率的再校准

**① 严格定义**：预训练学了"通用语言的下一 token 分布"；SFT 用 `(指令, 期望回答)` 对把分布**校准到指令跟随格式**。损失仍是 next-token 交叉熵，差别全在数据形态与 **loss mask**——prompt 部分 label=-100（不进损失），回答部分正常计损：模型只学"怎么答"。

**② 大白话**：**师傅带徒弟的示范教学**。徒弟（模型）已经大学毕业（预训练：世界知识都在），SFT 是师傅做一遍给他看：这个任务该这么接、这么答。关键纪律：**只考答案不考提问**——考试卷上徒弟该背的是"师傅怎么答的"，不是"客户怎么问的"（学提问会让模型模仿用户口吻，复读机化；且问句千变万化，学它是浪费容量）。loss mask 就是试卷上的**划重点线**：线之前的（问题）不考，线之后的（回答）才考——同一份教材，有效训练密度翻倍。

**③ 举例**：一条样本的 token 流（Chat Template 展开）：

```text
<|im_start|>user\n 给敌人加碰撞伤害 <|im_end|>\n<|im_start|>assistant\n [改 enemy.gd...] <|im_end|>
└─── mask=-100（不学提问）───┘└──── 损失全在这段（学回答+EOS）────┘
```

```python
def build_sft_sample(instruction, answer):
    prompt_ids = tok.apply_chat_template(
        [{"role": "user", "content": instruction}], tokenize=True, add_generation_prompt=True)
    answer_ids = tok(answer, add_special_tokens=False)["input_ids"] + [tok.eos_token_id]
    return prompt_ids + answer_ids, [-100]*len(prompt_ids) + answer_ids   # ★mask 现场
```

**④ 演进**：全参数微调（7B 要 8×A100，个人无缘）→ Prompt/Prefix Tuning（只训提示向量，容量小）→ **LoRA**（2021 低秩旁路，性价比革命）→ QLoRA（2023：4bit 基座+LoRA，单卡 24G 可训）→ M18 的 RL（从"模仿"到"择优"）。

**⑤ 易错点**：
- **Chat Template 每家不同**（`<|im_start|>` vs `<s>[INST]`），用错模板=训出只会输出乱码的模型——必须 `apply_chat_template` 不许手拼
- EOS 必须作为 label 学到（不学 EOS 推理时停不下来，车轱辘话无限生成）
- 样本超长从中间截=回答砍两半——整条丢弃或保头截尾
- 拒答样本（"抱歉我不能"）会教坏模型——质量过滤先于数量（1 万精品 > 10 万带毒）

### 1.2 LoRA：低秩假设与 ΔW=BA

**① 严格定义**：微调的本质是找权重增量 ΔW。**低秩假设**：微调引起的变化是低秩的（任务只需很少自由度），故 ΔW = BA 可分解：

$$
W' = W_0 + BA,\quad B \in \mathbb{R}^{d \times r},\ A \in \mathbb{R}^{r \times k},\ r \ll \min(d,k)
$$

W₀ 冻结（不进优化器）；A 高斯初始化、**B 零初始化**（起点 ΔW=0 不破坏基座）；可训参数仅 2·d·r（d=4096,r=16 时约 0.1%）；推理可合并回原矩阵**零额外延迟**；缩放 α/r 控制影响强度。挂载点惯例：注意力 Wq/Wv 起步，现代常 qkvo+FFN 全挂。

**② 大白话**：**岗位培训，不上重读大学**。全参微调=把通识教育重学一遍（4 年学费+全部课本重买）；LoRA=大学知识冻结原样（W₀ 不动），只上三个月岗位课（A/B 两个瘦矩阵）——花 0.1% 的学费，学到岗位所需的全部增量。B 零初始化的巧思：培训第一天**什么都没改变**（ΔW=0，行为=基座），从零开始逐步加技能——不会一进培训班就把大学知识打乱。训练完"结业合并"（merge：W₀+=BA），岗位技能融进本体，推理时**不多一层计算、零延迟**——这是它击败 Adapter（串行小层，推理加延迟）的关键。理解锚点：**LoRA 之于微调 ≈ 残差连接之于深网——不动原结构，加一条可学习的捷径**。

**③ 举例**：教学版 60 行（可直抄）：

```python
class LoRALinear(nn.Module):
    def __init__(self, base: nn.Linear, r=8, alpha=16):
        super().__init__()
        self.base = base
        for p in self.base.parameters(): p.requires_grad_(False)      # 冻结
        self.A = nn.Parameter(torch.randn(r, base.in_features) * 0.01)  # 高斯
        self.B = nn.Parameter(torch.zeros(base.out_features, r))       # ★零init
        self.scale = alpha / r
    def forward(self, x):
        return self.base(x) + self.scale * (x @ self.A.T @ self.B.T)
    def merge(self):   # 结业合并
        self.base.weight.data += self.scale * (self.B @ self.A)
```

实验：套两层 MLP 玩具任务——观察 A/B 逐渐非零、loss 下降；merge 前后输出 allclose。

**④ 演进**：Adapter（串行小层，**推理加延迟**——LoRA 论文的靶子）→ LoRA（并行旁路可合并）→ QLoRA（NF4 量化冻结基座：7B 显存 80G→10G）→ DoRA/rsLoRA（改良变体）。

**⑤ 易错点**：
- **B 零初始化（不是 A）**：A 零+B 高斯时起点 ΔW≠0，冷启动破坏基座
- r 不是越大越好（r=64+ 易过拟合小数据）；α 常取 r 的 1~2 倍
- merge 后删 LoRA 模块再 save，否则加载结构对不上
- 多 LoRA 热切换（godot-lora/code-lora 运行时叠加）与 merge 部署是两种模式，别混

### 1.3 训练数据构造（决定上限的脏活）

**① 严格定义**：SFT 数据三源——A **文档指令化**（Godot 文档→LLM 生成 QA 对，self-instruct 路线；A 必须基于原文生成——RAG 约束，否则学会编造）；B **真实轨迹**（成功 craft 会话事件流→(任务, tool_calls+代码) 样本——**Agent SFT 的精华：学的不是答案而是怎么用工具的过程**）；C **风格样本**（M08 画像约定→少量高质）。配比 A:B:C≈5:4:1，流水线：MinHash 去重→规则过滤→配比。

**② 大白话**：**教材编写决定学生上限**。A 类=从官方手册改编习题（答案必须忠于原文——习题答案乱编，学生学会瞎说）；B 类=**师傅工作实录**（最珍贵：不只记"最终交了什么"，还记"中间查了哪份图纸、先做了什么后做了什么"——徒弟学的是完整工作流）；C 类=公司风格规范（"我们用 tabs"）。B 类是 Agent 微调与普通 Chat 微调的分水岭：assistant 部分是结构化 tool_calls+代码，模型学"何时调什么工具、怎么写代码"的**联合分布**。M17 阶段先手打 50 条示范轨迹顶上，M22 后自动化。

**③ 举例**：轨迹样本构造：

```python
def trajectory_to_sample(task, events):
    turns = []
    for ev in events:
        if ev.type == "tool_call_start":
            turns.append(("tool_calls", ev.payload))     # 结构化保留
        elif ev.type == "message_end":
            turns.append(("text", ev.payload["final_text"]))
    return render_chat_messages(task, turns)             # 再走 1.1 的 loss_mask
```

**④ 演进**：人工标注（贵）→ Self-Instruct（2022 LLM 自产指令）→ Evol-Instruct（复杂度进化）→ **轨迹蒸馏**（强模型跑任务采集轨迹微调小模型——DeepSeek-R1 蒸馏同范式）。

**⑤ 易错点**：
- 失败轨迹别全扔：改写成"错误纠正对"少量掺入（<10%）有益
- 合成数据同质化：一个模板生成五千条"怎么用 move_and_slide"，去重后只剩五十条有效
- 学习率配数据量：1 万条用 2e-4 会过拟合，先看 loss 曲线

### 1.4 训练工程与部署回接

**① 严格定义**：生产配方=LLaMA-Factory（写配置不写训练循环）：QLoRA（4bit 基座）+lora_rank 16+all 挂点+qwen 模板。训练后三部曲：**评估（M22 先于一切）→ 合并导出（GGUF 给 LM Studio / vLLM 起 OpenAI 兼容服务）→ models.yaml 注册**。

**② 大白话**：**培训班结业三步**：先考试（GodotBench，不是看培训出勤率 eval_loss）、再发证（merge 导出可部署格式）、再上岗（注册进网关轮岗表——与 DeepSeek 云端模型同权排队）。训练侧写配置、教学侧写代码——两条腿：手写层懂原理，配置文件跑生产。

**③ 举例**（配方节选）：

```yaml
model_name_or_path: Qwen/Qwen2.5-Coder-7B-Instruct
stage: sft; finetuning_type: lora
lora_rank: 16; lora_alpha: 32; lora_target: all
quantization_bit: 4            # QLoRA
template: qwen                 # ★必须与基座匹配
cutoff_len: 4096; learning_rate: 1.0e-4; num_train_epochs: 3.0
```

**④ 演进**：自写训练循环（调试地狱）→ HuggingFace Trainer → LLaMA-Factory/LLaMA-Factory 级配置化框架（超参实验标准化）。

**⑤ 易错点**：
- cutoff_len 截断轨迹的 tool 序列——样本构造时就控长在 4k 内，别靠训练时硬截
- eval_loss 与下游分数脱节——必须跑 GodotBench（loss 不代表任务能力）
- 显存不够：先降 cutoff_len 再降 batch（梯度累积补吞吐）

---

## 2. 接口设计（完整签名）

```python
# training/datasets/
@dataclass
class SFTSample:
    messages: list[dict]                    # openai 格式（含 tool_calls 轮）
    source: Literal["doc_qa", "trajectory", "style"]
    weight: float = 1.0
class SFTBuilder:
    def from_docs(self, parsed: list[ParsedDoc], generator: LLM) -> list[SFTSample]: ...
    def from_trajectory(self, task: str, events: list[AgentEvent]) -> SFTSample | None: ...
    def to_sharegpt(self, samples, path: Path) -> None: ...    # LLaMA-Factory 格式
class QualityFilter:
    def dedup(self, samples, threshold=0.9) -> list[SFTSample]: ...
    def rule_filter(self, samples) -> list[SFTSample]: ...
    def balance(self, samples, ratios: dict) -> list[SFTSample]: ...

# training/sft/
class LoRALinear(nn.Module): ...            # 教学版（1.2）
def run(config_path: Path) -> TrainReport: ...
def merge_lora(base_model: str, adapter_dir: Path, out_dir: Path) -> None: ...
def export_gguf(model_dir: Path, quant: str = "q4_k_m") -> Path: ...
def register_to_models_yaml(model_dir: Path, alias: str) -> None: ...
```

---

## 3. 关键难点参考片段：loss mask 边界审计器

模板 token 数错一个，训练全歪且**不报错**（loss 照样下降）——训练前必跑的防沉默错位器：

```python
def audit_mask(sample_tokens, labels, tok):
    first_learn = next(i for i, l in enumerate(labels) if l != -100)
    decoded_prefix = tok.decode(sample_tokens[:first_learn])
    assert decoded_prefix.endswith("assistant\n"), \
        f"边界异常: mask 结束于 {decoded_prefix[-20:]!r}"
    assert labels[-1] == tok.eos_token_id, "最后一位必须是 EOS 标签"
    print(f"prompt {first_learn} tok / answer {len(labels)-first_learn} tok ✓")
```

为什么难：它防的是"沉默的错位"——template 改版/tokenizer 升级/数据重建任何一个动作都可能让整批 mask 错位，loss 曲线一切正常，只有 GodotBench 分数雪崩才暴露。**审计器是把"事后爆炸"提前到"事前断言"**。

---

## 4. 手敲指引（函数级伪代码）

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 1 | `lab/m17/loss_mask.py` | `build_sft_sample：§1.1 ③；audit_mask：§3 代码` | 边界对齐人工数 token |
| 2 | `lab/m17/lora_layer.py` | `LoRALinear：§1.2 ③ 全量；merge 后 allclose 断言` | 玩具任务收敛+零起点恒等 |
| 3 | `datasets/quality.py` | `dedup：MinHash 签名→近重复簇→留代表；rule_filter：拒答词表/长度/格式三规则；balance：按 source 分组采样配比` | 5000 同质样本去重后 <500 |
| 4 | `datasets/sft_builder.py` | `from_docs：文档 chunk→RAG 约束生成 QA（检索原文进 prompt："基于以下内容回答"）→SFTSample；to_sharegpt：序列化 LLaMA-Factory 格式` | 抽检 50 条答案忠于原文 |
| 5 | `trajectory_builder.py` | `§1.3 ③ 代码：事件流→多轮 messages（tool_calls 轮保留结构）` | 手打 50 条示范轨迹全部转换正确 |
| 6 | `train_sft.py`+配置 | `run：subprocess 封装 llamafactory-cli train + 配置；sweep：多配置并行（不同 lr/rank 的实验矩阵）` | 首跑 loss 平滑下降 |
| 7 | `merge_export.py` | `merge_lora：加载 base+adapter→merge→save；export_gguf：convert_hf_to_gguf.py 调用+量化；register：models.yaml 追加 local 别名` | LM Studio 加载对话可用 |

---

## 5. 测试与验收

```python
def test_lora_zero_init_identity():
    layer = LoRALinear(base, r=8)
    assert torch.allclose(layer(x), base(x))        # ΔW 起点=0

def test_mask_boundary():
    ids, labels = build_sft_sample("hi", "hello")
    audit_mask(ids, labels, tok)                    # 不抛即通过

def test_dedup_collapses_near_duplicates(): ...
```

**验收 Demo（MI-6a）**：教学版 LoRA 收敛曲线 + LLaMA-Factory 训练完成 + `godot-agent ask --model local/godot-coder-sft "Area2D 检测碰撞的信号"`——回答风格贴合训练数据（GodotBench 量化留 M22）。

---

## 6. 踩坑记录（留白自填）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

---

## 7. 面试拷打（附详细参考答案）

**1. SFT 与预训练的损失函数相同，差别在哪？**
答：三个差别：①数据形态——预训练用海量无标注文本（自回归即可），SFT 用精选的（指令,回答）对；②**loss mask**——SFT 把 prompt 部分置 -100 只对回答计损（学"怎么答"不学"怎么问"）；③数据量级与超参——预训练万亿 token/小学习率/长周期，SFT 万级样本/大学习率（LoRA 1e-4 级）/2~3 epoch。一句话：**SFT = 在冻结的世界知识上，重练"对齐格式的条件概率"**——同一把尺子（交叉熵），量的是不同的东西。

**2. 为什么 prompt 部分 mask -100？不 mask 会怎样？**
答：mask 的理由：①容量效率——问句分布千变万化且不是训练目标，学它浪费模型容量；②行为安全——学提问会让模型倾向模仿用户口吻（生成时爱"扮演用户"复读机化）；③梯度密度——同一条数据有效梯度全集中在回答段，数据利用率翻倍。不 mask 的实际后果（实验可验证）：模型开始生成"用户视角"的文本（如回答开头出现"请问…"）、指令跟随变弱。特例：多轮对话中**中间轮的 assistant 回答也计损**（它是"回答"的一部分），只有 user/tool 轮 mask。

**3. Chat Template 用错会发生什么？为什么必须 apply_chat_template？**
答：推理时服务端用模板把 messages 渲染成 token 流——训练与推理的模板必须逐 token 一致。用错模板（如拿 qwen 模板训、llama 模板部署）的后果：模型看到的"开场白"全是训练时没见过的 token 序列——输出乱码或复读特殊符号；或半错位（模型勉强回答但行为怪异——格式 token 的条件概率全错）。必须用 apply_chat_template 的理由：模板不是简单字符串拼接（含特殊 token 的 token 化规则/角色顺序/生成提示符），各家实现有细节差异——手拼字符串几乎必然错一两个 token，而 mask 边界审计器（§3）就是为抓住这种错位而生。

**4. LoRA 的低秩假设是什么？A/B 谁零初始化、为什么？**
答：低秩假设：微调引起的权重变化 ΔW 的"内在维度"很低——任务适配只需调整特征空间中很少的方向（实验支持：多数任务 8~16 秩足够），因此 ΔW∈R^{d×k} 可用 BA（d×r 与 r×k，r≪min）近似，参数量从 d·k 降到 2·d·r。**B 零初始化**（A 高斯）：ΔW=BA，B=0 时 ΔW=0——训练起点模型行为与基座完全一致，技能从零逐步加上；若反过来 A=0：数学上起点也是零，但梯度上 B 的更新依赖 A 非零（∂L/∂B ∝ A），A=0 时两者互相锁死在零点附近收敛极慢——不对称的根源在梯度结构。

**5. LoRA 为什么推理零延迟？Adapter 方案差在哪？**
答：LoRA 训练时是并行旁路（y=W₀x+scale·BAx），部署时可**代数合并**：W'=W₀+scale·BA 烘焙成一个矩阵——推理时就是一个普通 Linear，与原模型逐 FLOP 相同，零额外延迟零额外显存。Adapter（Houlsby 2019）是**串行**小层插在层间（y=f(W₀x)+adapter(x)）——无法合并（非线性结构），每层永远多两次矩阵乘+激活，推理延迟随深度累积。这是 LoRA 论文的直接靶子：**"效果相当，但我不加推理成本"**——也是它迅速成为工业标准的原因。

**6. r 和 α 的作用？rank 大就好吗？**
答：r（秩）=旁路的"自由度/容量"：r 越大 ΔW 能表达的变化越丰富（能学更复杂的任务偏移），参数与过拟合风险同步增加。α（配合 scale=α/r）控制 LoRA 输出的**影响强度**——α 大则同样学到的 BA 对前向的影响被放大。经验：简单风格任务 r=8、领域能力 r=16~32、复杂推理 r=64+；α 取 r 的 1~2 倍。rank 大不好的原因：小数据集（万级样本）下高秩旁路易过拟合（把训练集的噪声也学进去，泛化反降）；且参数与显存线性涨。正确姿势：从 r=16 起步，用 GodotBench val 分数做模型选择——**rank 是超参不是越大越好的容量崇拜**。

**7. QLoRA 怎么把 7B 塞进单卡？**
答：三板斧：①**NF4 量化冻结基座**——W₀ 用 4bit NormalFloat 存储（正态分布友好的量化格点，信息损失最小），相比 fp16 显存直接 ÷4；②**计算时反量化**——前向时按需把 NF4 块反量化回 bf16 参与计算（精度损失可控）；③**LoRA 旁路保持 fp16/bf16**——可训练部分（A/B）全精度，梯度只流经旁路。合计：7B 基座 ~3.5GB（4bit）+ LoRA 参数极少 + 优化器状态只覆盖 LoRA——24G 单卡可训（对比全参微调要 8×A100 80G）。附加技巧：双重量化（量化常数也量化）、分页优化器（显存峰值管理）。

**8. 轨迹样本与普通 QA 样本的区别？工具轮怎么进 loss mask？**
答：普通 QA：assistant=纯文本回答。轨迹样本：assistant 轮是**结构化 tool_calls**（name+arguments JSON）+ 后续 tool 轮（执行结果）+ 最终 text 轮——模型学的是"何时调什么工具+怎么写代码+怎么整合结果"的联合分布。mask 处理：**user 轮与 tool 轮 mask**（tool 结果是环境给的，不是模型该学的——学它会教模型"背诵执行结果"），**assistant 轮（含 tool_calls JSON 的 token）计损**——其中的 JSON 结构、参数写法、调用时机全是学习目标。实现上把 tool_calls 的 arguments 字符串模板化进 assistant 段即可套用同一个 mask 函数。这是 Agent 微调的核心技术点：**教的是决策与生成，不是环境反馈**。

**9. eval_loss 降但下游任务分不涨，怎么办？**
答：诊断三步：①**先怀疑数据**——loss 衡量"模仿训练分布"的拟合度，若训练数据与真实任务分布有偏（合成 QA 多、真实轨迹少），模型把"错误的教材"背得更好，分数当然不涨——查数据配比与真实任务的匹配度；②**再怀疑模板/部署错位**——训练用 A 模板、推理用 B（或 merge 没做、量化掉精度）：用 audit_mask 复检+部署链路逐步排查（合并前后对比输出）；③**最后怀疑过拟合**——eval_loss 若也在降但分数不涨，可能学到的是"像训练数据的风格"而非任务能力（风格学到头，能力没学到）。原则：**下游 benchmark 是唯一裁判，loss 只是训练过程的体温计**——这也解释了为什么 M22 评估要先于微调模块存在。

**10. 开放题：RAG 与微调怎么配合？Godot 5.0 API 大改，重训链路怎么排？**
答：分工本质：RAG=知识外挂（**知道什么**——事实、版本细节、文档），微调=能力内化（**怎么做**——格式、风格、工具使用模式）。配合形态（本项目）：日常问答靠 RAG（文档可更新），代码生成风格与工具决策靠 SFT（轨迹内化），RL 再优化多步任务成功率。Godot 5.0 场景的重训链路排序：①**RAG 先行**（小时级）——新文档入库，问答立刻覆盖新 API；②**风格不受影响**（SFT 学的缩进/命名不变）；③**工具轨迹需增量重训**（周级）——新 API 的示范轨迹（人工 50 条+强模型采集）增量 SFT；④**RL 奖励函数检查**——验证器升级（新版本的 check/test 语义变了要适配），然后在**新环境**重跑 GRPO（旧轨迹的奖励在新版本下失效）。关键洞察：**分层设计让"知识更新"（RAG 小时级）与"能力更新"（微调周级）解耦**——这正是当初选择双轨的回报。

---

## 8. 教程映射与延伸

- 📗 hello-agents 微调章（LoRA/SFT 基础与本项目同型）
- 必读：LoRA 论文（Hu 2021）；QLoRA 论文（Dettmers 2023，读 NF4 与双重量化节）
- 选读：Self-Instruct；LLaMA-Factory README（配置项字典）
