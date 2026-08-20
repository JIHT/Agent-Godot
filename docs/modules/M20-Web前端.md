# M20 Web 前端（React 工作台 · SSE 客户端 · Diff 审查 · 状态管理）

| 项 | 值 |
|---|---|
| 生产进度 | Sprint 15 · 里程碑 **MI-7「Web 工作台可用」** |
| 代码落点 | `frontend/src/`（api/stores/features/chat/editor/knowledge/training/admin，见 §0.5） |
| 前置模块 | M19（API 与 SSE 协议即合同）· M06（Diff/artifact 结构）· M09（确认门事件） |
| 手写比例 | 100% 手写（openapi-typescript 生成客户端属于工具链） |
| 教程映射 | 📘 zero2Agent 12 课（web-ui）· React 19/Vite 官方文档 · 📝笔记前端架构 |

---

## 0. 本模块在项目中的位置

**大白话**：Agent 能力再强，用户感知的全部就是这块屏幕——**流式打字、工具卡片、确认弹层、Diff 审查**。前端的工程本质一句话：**把 M19 的 SSE 事件流忠实且优雅地渲染成界面，把用户的每个决策可靠地回传**。它是**同声传译的译员**：后端说事件流（text_delta/tool_call/confirm），译员实时转成用户看得懂的界面；用户的每次按键（批准/拒绝/勾选 hunk）也要一字不差地传回去。CLI 与 Web 双前端并存——M00"三消费者复用同一 Runtime"的最终验证。

**交付后状态**：完整工作台——会话列表/流式对话/工具卡折叠展开/确认弹层/Diff 逐块审批/文件树+Monaco/知识库管理页。

---

## 0.5 ★ 施工文件清单（开工前必看的一页表）

**本模块你一共要新建 15 个文件**（按依赖顺序）：

| # | 新建文件（完整路径） | 职责一句话 | 关键内容 | 预估行数 | 手敲步骤(§4) | 依赖 |
|---|---|---|---|---|---|---|
| 1 | 脚手架（Vite+TS+Router+Tailwind） | 项目底座 | — | 配置 | 步骤 1 | — |
| 2 | `src/api/types.ts` | OpenAPI 生成类型 | `pnpm openapi-typescript` | 生成 | 步骤 2 | M19 |
| 3 | `src/api/client.ts` | REST 薄客户端+token 刷新 | `authedFetch`（单飞） | 80 | 步骤 2 | types |
| 4 | `src/api/sse.ts` | ★手写 SSE 客户端 | `streamSSE/parseFrame` | 80 | 步骤 3 | — |
| 5 | `src/stores/session.ts` | 会话 store+纯 reduce | `reduceSession` | 90 | 步骤 4 | sse |
| 6 | `src/stores/{ui,auth}.ts` | UI/认证切片 | — | 40 | 步骤 4 | — |
| 7 | `src/features/chat/MessageStream.tsx` | 异构消息流 | 三类渲染 | 100 | 步骤 5 | session |
| 8 | `src/features/chat/ToolCallCard.tsx` | 工具卡三态 | running/ok/failed | 60 | 步骤 5 | — |
| 9 | `src/features/chat/ConfirmGate.tsx` | 确认门队列弹层 | 队列化 | 70 | 步骤 5 | M09 协议 |
| 10 | `src/features/editor/FileTree.tsx` | 文件树（懒加载） | — | 60 | 步骤 6 | api |
| 11 | `src/features/editor/MonacoPane.tsx` | 编辑器+gdscript 高亮 | language 注册 | 100 | 步骤 6 | monaco |
| 12 | `src/features/editor/DiffReview.tsx` | Diff 审查+hunk 审批 | `HunkLayer` | 100 | 步骤 7 | monaco |
| 13 | `src/features/knowledge/` 等 | 三管理页 | upload/status | 80 | 步骤 8 | api |
| 14 | `src/App.tsx` + 路由 | 惰性加载+错误边界 | — | 40 | 步骤 8 | — |
| 15 | 测试 | sse/reduce/单飞 | vitest | 60 | 随写随跑 | — |

**完成后你拥有**：MI-7 全流程验收（§5）。

---

## 1. 知识点详解（每节五段：定义 → 大白话 · 举例 · 演进 · 易错点）

### 1.1 SSE 客户端：为什么手写而不用 EventSource

**① 严格定义**：原生 `EventSource` 三不满足：**不能 POST**（对话要发消息体）、**不能带 Authorization 头**（JWT）、**不能设自定义 content-type**——所以用 `fetch`+`ReadableStream` 手写。关键机制：**外层重连环**（断线后带 `Last-Event-ID` 重连，指数退避 1s→15s）+**内层读帧环**（缓冲区按 `\n\n` 攒帧——帧可能跨 chunk 劈开）+**幂等渲染**（重复帧按 id 去重）。

**② 大白话**：**自装对讲机而不是买收音机**。EventSource 是收音机——只能听电台（GET 请求），不能喊话（POST 消息体）、不能出示证件（Authorization 头）。手写 fetch 流=自己装对讲机：想说什么说什么（POST body）、带工牌进场（JWT 头）、断线自动重拨并接上刚才的话题（Last-Event-ID 补发）。攒帧缓冲的道理：电报按字到达，一句话没念完不能开始翻译——**按空行分帧，半句话攒着**。

**③ 举例**：核心 60 行（可直抄）：

```typescript
export async function streamSSE(url, body, token, onEvent, signal) {
  let lastId = 0;
  while (true) {                                          // 外层重连环
    const resp = await fetch(url, { method: "POST",
      headers: { "Content-Type": "application/json",
                 Authorization: `Bearer ${token}`,
                 ...(lastId ? { "Last-Event-ID": String(lastId) } : {}) },
      body: JSON.stringify(body), signal });
    const reader = resp.body!.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    while (true) {                                        // 内层读帧环
      const { done, value } = await reader.read();
      if (done) break;
      buf += value;
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {          // ★帧以空行分隔
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const ev = parseFrame(frame);                     // data:/id:/心跳
        if (ev?.id) lastId = ev.id;
        if (ev?.data) onEvent(ev);
      }
    }
    await sleep(Math.min(1000 * 2 ** retries++, 15000));  // 指数退避
  }
}
```

与 M19 hub 的契约：`id` 单调递增→重连带 Last-Event-ID→环形缓冲补发→**前端幂等渲染去重**。

**④ 演进**：轮询→EventSource（2015 标准化，只够订阅场景）→ fetch 流手写（POST/头自由）→ RSC 流（另一范式）。**手写一遍 SSE 解析器，WebSocket/HTTP2 流触类旁通**。

**⑤ 易错点**：
- 帧分隔 `\n\n` 但跨 chunk 劈开——必须 buf 攒帧，逐 chunk parse 会丢帧
- React StrictMode 双执行 effect——SSE 订阅要幂等（AbortController 清理+store 去重）
- 401 静默刷新 token 重试，429 退避提示——两种"失败"体验完全不同

### 1.2 会话状态：zustand 最小切片

**① 严格定义**：三类状态按**变化频率与共享范围**分家：`useSessionStore`（服务端数据镜像——SSE 事件驱动，高频）、`useUIStore`（纯前端态——用户驱动，中频）、`useAuthStore`（token/配额——低频）。流式渲染性能命门：text_delta 每秒几十次——**按 message_id 分片订阅**（MessageBubble 只订阅自己的消息，selector 粒度），文本合并 rAF 节流。

**② 大白话**：**按部门分办公室，别全公司一个大厅**。所有状态放一个 Context=全员大会：任何人打个喷嚏（任一 delta 到达）全公司陪会（全树重渲染）。切片 store=按部门开会：流式更新只惊动"当前正在打字的那个气泡"（部门里的一个人）。选 zustand 不选 Redux 的理由：样板少、selector 自由、**高频更新不连坐**——流式场景这是决定性的。

**③ 举例**：事件到 store 的纯函数归并（React 之外可单测）：

```typescript
export function reduceSession(state: SessionState, ev: AgentEvent): SessionState {
  switch (ev.type) {
    case "text_delta":   return patchMessage(state, ev.message_id, m => ({...m, text: m.text + ev.delta}));
    case "tool_call_start":   return addToolCall(state, {id: ev.call_id, name: ev.tool, status: "running"});
    case "tool_call_result": return patchToolCall(state, ev.call_id, {status: ev.ok?"ok":"failed", summary: ev.summary});
    case "confirm_required": return {...state, confirmQueue: [...state.confirmQueue, ev]};
    case "message_end":  return {...state, streaming: false, usage: ev.usage};
  }
}
```

**④ 演进**：useState 提升（prop drilling）→ Context（全树重渲染）→ Redux（样板重）→ zustand/jotai（原子订阅）。主线：**订阅粒度越来越细，渲染范围越来越小**。

**⑤ 易错点**：
- reducer 必须纯且不可变——它是"事件溯源"的前端投影（M09 同款思想），不纯无法去重回放调试
- 工具卡折叠态别进 store（UI 局部态）——存了会话恢复时出现"折叠了没展开过"的荒谬状态

### 1.3 三类渲染单元：消息流 · 工具卡片 · 确认门

**① 严格定义**：消息流是**异构列表**（user 气泡/流式 markdown/工具卡/引用角标/系统条）。渲染策略：流式 markdown 用 marked+DOMPurify（**禁止 dangerouslySetInnerHTML 裸用——XSS**）+rAF 节流 re-parse；工具卡三态（running 转圈/ok 折叠摘要/failed 展开错误+重试建议），复杂结果默认折叠——**工具是"过程"，用户要"结论+可展开细节"**；确认门全局唯一队列化弹层，动作三选（允许一次/本会话总是/拒绝+理由）。

**② 大白话**：**展厅三种展柜**。文字流=滚动画卷（边画边展开）；工具卡=折叠说明书（封面一行结论，感兴趣的翻开细看）；确认门=柜台按铃（ Agent 需要签字时铃响，柜员弹出文件+笔）。"拒绝必须可填理由"不是客套——理由会回填为 Observation，**模型靠它改道**（M09 设计在前端的兑现）；"本会话总是"直连 M09 的 `remember: session` 规则授予——一次点击免掉本会话后续所有同类弹窗。

**③ 举例**：确认门队列化（防多个确认叠弹层）：

```tsx
function ConfirmGate() {
  const queue = useSessionStore(s => s.confirmQueue);
  const [current, ...rest] = queue;
  if (!current) return null;
  return <Modal title={`${current.tool} 需要确认`} risk={current.risk}>
    <pre>{current.preview}</pre>
    <ReasonInput placeholder="拒绝时建议填写理由（模型会参考）" />
    <Actions onAllow={...} onAlways={...} onDeny={...} />
  </Modal>;
}
```

**④ 演进**：文本框（terminal）→ 气泡流（IM）→ **过程可见的工作流**（Cursor 式：工具卡+Diff 内联+确认门）。Agent 产品 UI 的方向：**把执行过程从黑箱变成可审计的时间线**。

**⑤ 易错点**：
- 流式中半截代码块（``` 未闭合）——parser 容忍渲染为"进行中代码块"，别崩
- 工具卡 summary 是 2k 截断版——完整数据要拉 `/artifacts/{id}`
- 确认门期间切换会话——弹层必须跟随 pendingConfirm 的会话归属，不能串台

### 1.4 Diff 审查与编辑器工作台

**① 严格定义**：M06 的 FileDiff+hunks→Monaco DiffEditor（original=快照, modified=生成, inline 模式）→**逐 hunk 审批**（勾选框与 Monaco diff 区块按行号对齐）→`POST /diffs/{id}/apply {approved_hunks}`。GDScript 高亮：Monaco 无内置——自定义 language registration（关键字/内置类型/注解三组 token 规则，从 Godot 官方 tmLanguage 转换，约 200 行）。

**② 大白话**：Diff 审查是**画作的裱框审查台**：左边原作（快照）、右边 AI 的仿作（生成），评审员逐段贴标签——这段临摹得好（勾选）、这段走样了（拒绝）——最后只把贴了绿标的段落装裱（apply 部分应用）。Monaco 提供"放大镜"（专业编辑器体验：高亮/折叠/跳转），自己只需要补一块"Godot 方言词典"（gdscript 高亮规则）。

**③ 举例**：hunk 审批交互层：

```tsx
const HunkLayer = ({diff, approved, onToggle}) => (
  <div className="hunk-sidebar">
    {diff.hunks.map((h, i) => (
      <label key={i} className={approved.has(i) ? "on" : "off"}>
        <input type="checkbox" checked={approved.has(i)} onChange={() => onToggle(i)} />
        Hunk {i+1} · {h.old_start}-{h.old_start + h.old_len}
      </label>))}
    <button disabled={!approved.size}
            onClick={() => api.applyDiff(diff.id, [...approved])}>
      应用选中的 {approved.size} 块
    </button>
  </div>);
```

**④ 演进**：textarea→CodeMirror/Monaco（LSP 级体验）→ **AI 审查流**（Diff 是对话的一等公民）→ 结构化编辑（场景树直改）。

**⑤ 易错点**：
- 部分应用后行号漂移——apply 响应返回新 hash，前端失效缓存强制刷新
- 大文件 Diff 卡顿——`renderSideBySide=false`+computeDiff 异步
- 用户 Monaco 未保存修改 vs Agent 写冲突——dirty 状态提示（乐观锁前端版）

### 1.5 工程化：类型链路与构建

**① 严格定义**：**端到端类型安全**——FastAPI 自动生成 OpenAPI→`openapi-typescript` 生成 `apiTypes.ts`→手写薄客户端→业务代码零 any；SSE 事件类型单独维护（与后端 `agent/events.py` 常量同源评审）。Vite 分包：monaco 动态 import（首屏不含编辑器）；feature 级路由惰性加载。

**②③④ 合并**：错误边界兜底 SSE 解析异常；openapi 再生成后 any 冒头 CI 拦（tsc strict 底线）。演进：手写接口文档→Swagger 手动同步→**Schema 单一事实源自动生成**（类型漂移在编译期暴露）。

---

## 2. 接口设计（前端骨架签名）

```typescript
// api/client.ts
export const api = {
  auth: { login(f), refresh(), me() },
  sessions: { list(), create(p), messages(id, body, onEvent, signal), confirm(id, cid, a) },
  projects: { list(), files(id, path), bind(p) },
  diffs: { get(id), apply(id, hunks) },
  knowledge: { upload(f, onProgress), status(docId) },
};

// stores/session.ts
interface SessionStore {
  sessions: SessionMeta[]; messages: Record<string, Message[]>;
  streaming: boolean; confirmQueue: PendingConfirm[]; usage: Usage | null;
  dispatch(ev: AgentEvent): void;      // reduceSession 的出口
  send(content: string): Promise<void>;
}
```

---

## 3. 关键难点参考片段：token 刷新的并发单飞

多个并发请求同时 401，各自触发 refresh 会互相作废（旋转式下第二次 refresh 已失效）：

```typescript
let refreshPromise: Promise<string> | null = null;
export async function authedFetch(input: RequestInfo, init: RequestInit) {
  let resp = await fetch(input, withAuth(init));
  if (resp.status === 401) {
    refreshPromise ??= refreshToken().finally(() => (refreshPromise = null));  // ★单飞
    const token = await refreshPromise;
    resp = await fetch(input, withAuth(init, token));     // 新 token 重放一次
  }
  return resp;
}
```

为什么难：竞态在"多个 401 共享一次刷新"与"刷新失败全员登出"的异常传播——finally 清锁保证后续 401 能再发起，共享 promise 让同批请求等同一结果。**分布式系统的"合并并发请求"模式（single-flight）在前端的缩影**。

---

## 4. 手敲指引（函数级伪代码）

| 步骤 | 文件 | 函数级作用（伪代码） | 验证 |
|---|---|---|---|
| 1 | 脚手架 | `Vite react-ts 模板+Router+Tailwind+vitest` | 空壳跑起 |
| 2 | `api/` | `openapi-typescript 生成类型；authedFetch：§3 单飞；api 对象按资源分组薄封装` | 登录流程通 |
| 3 | `api/sse.ts` | `streamSSE：§1.1 ③ 双环结构；parseFrame：:注释跳过/data:拼接/id:解析` | 假事件流回放单测（含跨 chunk 劈帧用例） |
| 4 | `stores/` | `reduceSession：§1.2 ③ 纯函数；send：api.messages+dispatch 事件流` | 事件序列回放快照测试 |
| 5 | `chat/` | `MessageStream：异构列表+vDOM memo；ToolCallCard 三态；ConfirmGate 队列化+三动作回传` | curl 造事件全渲染 |
| 6 | `editor/` | `FileTree 懒加载+虚拟滚动；MonacoPane：动态 import+gdscript language 注册；` | 打开项目文件高亮正确 |
| 7 | `DiffReview` | `DiffEditor inline+HunkLayer 勾选→apply→按响应刷新` | 部分应用回写成功 |
| 8 | 管理页+App | `knowledge 上传进度/training 任务/admin 审计；路由惰性+错误边界` | 全功能走查 |

---

## 5. 测试与验收

```typescript
test("sse parser handles frames split across chunks", () => {
  const p = new SSEParser();
  p.feed('data: {"type":"text_de');
  const evs = p.feed('lta"}\n\n: ping\n\n');
  expect(evs).toEqual([{ data: { type: "text_delta" } }]);
});

test("reduce is idempotent under frame replay", () => { ... });
test("refresh single-flight under concurrent 401s", () => { ... });
```

**验收 Demo（MI-7 里程碑）**：浏览器全流程：登录→绑定 lab/m06 项目→`craft "给玩家加双跳"`→流式打字+工具卡片→写文件确认门（预览 Diff、选"本会话总是"）→DiffReview 逐块审批应用→Monaco 打开改后文件→**刷新页面会话完整恢复（事件重放）**。

---

## 6. 踩坑记录（留白自填）

| 日期 | 坑 | 现象 | 根因 | 解法 | 关联知识点 |
|---|---|---|---|---|---|
|     |    |     |     |    |          |

---

## 7. 面试拷打（附详细参考答案）

**1. EventSource 三不满足是什么？手写的关键点？**
答：三不满足：不能 POST（只支持 GET，对话要发消息体）、不能带自定义 Authorization 头（JWT 没法传）、不能设 content-type。手写关键四点：①fetch+ReadableStream+TextDecoderStream 逐 chunk 读；②**缓冲区攒帧**——SSE 帧以 `\n\n` 分隔，但 TCP chunk 边界与帧边界不对齐，半帧必须攒到下一个 chunk 拼完再 parse（丢帧 bug 的头号来源）；③外层 while 重连环（Last-Event-ID+指数退避）；④AbortSignal 贯穿（组件卸载/切换会话能真正中断 fetch）。手写收益：协议全可控+一次学会所有流式解析（WebSocket 消息帧、HTTP2 流同构）。

**2. 断线补发的前端配合？**
答：三层配合：①**记 id**——每个事件的 `id:` 单调递增，客户端维护 lastId（连 store 之外，重连后仍要用）；②**带票重连**——重连请求带 `Last-Event-ID` 头，服务端 hub 环形缓冲补发缺失段；③**幂等渲染**——补发段与已收段可能重叠（补发边界），reduceSession 必须幂等（同帧重放结果不变：delta 按 id 去重、状态覆盖式更新）。三者缺一：不记 id 无法补发；不带票服务端不知道从哪补；不幂等补发导致消息翻倍。这是**至少一次投递+消费端幂等**的经典分布式组合（Kafka consumer 同款语义）在 SSE 上的实例。

**3. 流式高频更新怎么防全树重渲染？**
答：三板斧：①**切片订阅**——zustand selector 把订阅粒度压到组件级：MessageBubble 只订阅自己的 message（`useStore(s => s.messages[id])`），其他消息的 delta 不触发本组件；②**rAF 节流**——text_delta 每秒几十次直接 set 会排队几十次渲染，用 requestAnimationFrame 合并（每帧最多一次 setState，多帧内 delta 先累积）；③**vDOM 层 memo**——异构列表的静态项（历史消息）React.memo 隔离。度量驱动：React DevTools Profiler 看 commit 次数——未优化时一个 delta 全树 commit，优化后只有目标气泡 commit。

**4. reduceSession 为什么必须纯函数？**
答：三个用途都依赖纯性：①**去重**——补发段重放要产生相同结果（非纯函数如累加计数器遇重放就翻倍）；②**回放**——刷新页面/调试时间旅行=从空状态重放全部事件（非纯无法复现）；③**测试**——快照测试"给定事件序列→断言最终状态"（非纯要 mock 环境）。它本质是**前端的事件溯源投影**（M09 Session 同款思想在浏览器端的复刻）：事件是事实（不可变），状态是事件的派生视图——这个架构让"会话恢复"变成免费的"重放到 message_end"。

**5. 流式 markdown 的 XSS 防线？半截语法怎么容错？**
答：XSS 防线：模型的输出是不可信输入（可能被提示注入诱导输出 `<script>` 或 onerror 属性）——三道：①marked 解析+**DOMPurify 消毒**（白名单标签/属性，script/事件处理器全灭）；②绝不 dangerouslySetInnerHTML 裸用；③链接协议白名单（javascript: 伪协议过滤）。半截语法容错：流式中代码块 ``` 未闭合——marked 容忍未闭合渲染到当前为止；表格/列表同理按"进行中"渲染——**每帧 re-parse 整段而不是增量拼接 HTML**（增量拼会把半截标签永久固化）。性能上 rAF 节流兜住 re-parse 成本。

**6. 确认门为什么队列化？"本会话总是"对应后端什么？**
答：队列化原因：一轮工具调用可能多个高危（5 并行调用 3 个待确认）——叠弹层用户无法区分顺序，且 z-index 战争；队列=一次一个、答完下一个，与后端逐个挂起恢复的节奏一致。前端只做展示队列，**决策状态在后端 session 的 pendingConfirm 队列**（刷新页面队列还在——M09 持久化）。"本会话总是"= 调 confirm 时带 `remember: session` → 后端 RuleEngine.grant_session 给该规则打会话级 allow——**前端一个按钮，后端一条规则**，下次同类调用直接放行不再弹窗。这个设计的价值：把"用户烦了"的信号转化为系统的授权知识。

**7. 逐 hunk 审批的数据对齐？部分应用后的缓存失效链？**
答：对齐问题：HunkLayer 的勾选框来自 API 的 hunks 数组（序号 i），Monaco 的 diff 高亮块来自编辑器自己算的 diff——**两者行号算法不同可能块数不同**。解法：不用 Monaco 的计算结果，把 API hunks 的行号区间映射成 Monaco 的 decorations（着色区间），保证"勾的第 3 块=高亮的第 3 块=提交的 approved_hunks[2]"。失效链：部分应用后文件变化→apply 响应返回新 hash 与新 hunks→前端失效该文件的本地缓存（FileTree 重拉、Monaco model 重新加载、会话内嵌"已应用"系统消息）——**不做失效链，用户看到的还是旧内容，下一次编辑基于幻影**。

**8. token 刷新单飞解决什么竞态？**
答：竞态场景：页面加载时 6 个请求并发，access 恰好过期→6 个都收 401→各自触发 refreshToken。在旋转式刷新（旧的用一次就作废）下：第 1 个刷新成功作废旧 refresh，第 2~6 个拿着同一旧 refresh 去刷——**全部失败→全员登出**，用户莫名其妙被踢。单飞：全局 `refreshPromise ??= refreshToken()`——第一个 401 发起刷新，其余共享同一个 promise 等结果；finally 清 null 让下一轮 401 能再发起。这是 **single-flight 模式**（合并并发相同请求）：用"共享一次飞行"消灭"重复起飞互相击落"。

**9. Monaco 大文件 Diff 的优化清单？**
答：五项：①`renderSideBySide=false`（inline 模式渲染量减半）；②computeDiff 异步选项（不阻塞主线程，大 diff 显示骨架）；③**虚拟滚动**（Monaco 内建，确保开启——只渲染可视行）；④大文件降级阈值（>5000 行 diff 时放弃 DiffEditor，改用"全文对比提示+分段查看"——工程上承认极限比硬撑流畅更专业）；⑤动态 import（monaco 几 MB，首屏不载入——用到编辑器才加载）。附带：diff 计算在 worker（Monaco 自带 web worker 配置要正确，Vite 下 worker 打包是经典坑）。

**10. 开放题：会话时间线做成"多人协作"（同时看同一会话），架构改什么？**
答：四层改动：①**事件流多播**——hub 从"单会话单订阅者"扩展为"会话级广播频道"（presence：谁在线/谁在看哪条）；②**读协作容易**（SSE 天然多订阅者，各自 replay 补发）；**写协作要仲裁**——确认门的批准权：方案 A 举手制（第一个点的人生效，其余人看到"已由 X 批准"）；方案 B 锁定制（审批权锁定持有者 30s）；③**评论/光标层**——异步批注（锚定 message_id 的评论，CRDT 列表）；实时光标（OT/CRDT——Yjs 文档级协作，但注意这是**查看型协作**不是编辑型，复杂度可大幅裁剪：光标只广播位置不合并文本）；④**权限细分**——viewer 能看会话但不能看 Diff？（企业场景：外包看过程不看代码）RBAC 加会话级 ACL。关键判断：**先明确协作是"共看"还是"共写"**——共看只需多播+presence（成本低），共写才需要 CRDT（成本高）——多数"团队围观 AI 干活"场景是共看。

---

## 8. 教程映射与延伸

- 📘 zero2Agent 12 课（web ui）
- 必读：MDN SSE；zustand docs（切片模式）；Monaco DiffEditor API
- 选读：React 19 useSyncExternalStore 深读；openapi-typescript 工作流
