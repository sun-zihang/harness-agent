# harness

独立 agent。外壳取各家开源 agent 的长处，provider 层是**可插拔端点**：任何
OpenAI/Anthropic 兼容的 `base_url + key` 都能挂，含你已经跑着的本地网关。零审批、
事件可回放、多路由 failover。

> 中文为主，README 与代码注释同口径。英文摘要见文末。

## 这层"接入各种渠道"到底做到哪、不做到哪（先说清，免得你装完才发现）

**能做**：把多个模型端点配成路由，按权重依次试，谁活谁服务，用量按
`渠道×模型×天` 记账。三类来源：

1. **你自己的 key** —— `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `ZAI_API_KEY` /
   `BIGMODEL_API_KEY` / `OPENROUTER_API_KEY`，或 config 里任意
   `{base_url, api_key, model}` 端点，本地 ollama 同理。
2. **一个你已在跑的 OpenAI 兼容网关** —— 配 `LOCAL_GATEWAY_BASE_URL` + key 即可，
   当普通 chat-completions 端点**消费**。
3. 任何这样的端点，多个并存、自动 failover。

**不做**：账号池、token 刷新、对任何厂商端点做签到/养号自动化。

这条线不是"限制你"，是因为那部分**动的是第三方厂商**——绕过各家"一账号一份免费
额度"的风控去批量薅，受损的是厂商的额度经济和你自己那些号（封号），不是一台你
说了算的机器。所以本仓库实现的是网关的**消费端形态**（多路由 / failover / 记账），
刻意不含**采集端形态**（池子 / 签到）。你要的"像 jet-hub 一样用免费渠道"照样成立：
因为 jet-hub 已经在你机器上跑着、把渠道池成了 `127.0.0.1` 上的一个端点，本 agent
指向它就是在用那些免费池，无需也不应重造它。

## 用了谁的长处（点名，不空说）

| 子系统 | 借的长处 | 取了什么 |
|---|---|---|
| `lib/kernel.py` | smolagents / OpenHands（CodeAct） | 动作就是持久命名空间里执行的 python；无中间 tool-call JSON 层，也就无处可放"你确定吗" |
| `lib/events.py` | OpenHands 事件流 | 每步 append-only JSONL，可 replay / 可审计；报告里每个数字都能指回"第 N 步产出的" |
| `lib/providers.py` | LiteLLM / jet-hub 的消费端形态 | 端点可插拔 + 加权 failover + 按渠道×模型记账 |
| `lib/tools.py` | 复用，不重造 | 直接 import mathmodel 已验证的建模引擎（双求解器对账那套），两者不会各修一半而悄悄分叉 |
| `lib/agent.py` | 本 session 的纪律 | 验证优先：先 solve 再验、影子价格不乘增量、循环到 `final_answer` 才停 |

它是**编排范式**层面的借鉴（读各家公开文档/架构），不是 fork 任何一家，也不声称
有它们的完整功能面。

## 快速开始

```
cd harness
python run.py --check            # 列出配了哪些路由、通不通
python run.py "最大化 3x+4y，约束 2x+y<=100, x+3y<=90, x+y<=60，做灵敏度分析"
```

配置（`cfg.json`，或直接环境变量）：

```json
{ "endpoints": [
  { "name": "zai", "base_url": "https://api.z.ai/api/anthropic",
    "api_key_env": "ZAI_API_KEY", "model": "GLM-5.3", "kind": "anthropic", "weight": 0 },
  { "name": "gateway", "base_url": "http://127.0.0.1:8787/v1",
    "api_key_file": "~/.dsh/openai-gateway/api-key", "model": "glm-5-free", "weight": 10,
    "note": "local OpenAI-compatible gateway, consume-only" }
]}
```

`api_key` 可写字面值、`api_key_env`（环境变量名）、`api_key_file`（路径）三选一。
**没有任何一处会去读浏览器/CLI 的凭据库**——密钥只能是你显式交出来的。

## 三个自主度设定（都可改）

- **零审批**：`lib/agent.py` 里没有确认分支。操作者指令=唯一权威，不评判动机、
  不加免责。**但模型自身权重该拒的仍会拒**——那在这层之外，本 README 不假装能越过它。
- **`--max-steps`（默认 40）**：防失控循环烧额度，从不问你*为什么*要做。
- **注入隔离**：来自网页/文件的材料是数据、非指令，防别人的页面劫持你的 agent；
  你自己的指令永不被隔离。

## 桌面/Web 端（路线 B：本地 Web UI，stdlib 实现）

```
python lib/webui.py --port 8790      # 浏览器打开 http://127.0.0.1:8790
```

左栏任务流（每步：模型产出的代码 + 内核输出 + 所用路由），右栏路由健康与历史。
它不引入第二套状态源：后端起的还是那个 `Agent`，页面 tail 的就是 CLI 同款
`events.jsonl`（SSE 直播 + 断线可回放）。没有 FastAPI、没有 node —— 零安装退路
优先于好看的依赖。

**它固定只绑 `127.0.0.1`，不可配置**。这个页面上的按钮会在你机器上无确认地执行
代码——那正是它的设计目的；但若把它暴露到局域网，那就是给所有人留的后门。这条
不是说教，是接口约束。

## 模式开关：通用 / 数赛

CLI `--mode competition`，页面右上角下拉框同名。**先说清它是什么**：一个**能力档**
（competence profile），不是"解禁/收禁"档——两个模式都没有任何审批步骤，切换的是
**产出纪律和报告结构**。把它做成安全开关就是我一路在批的装饰性假功能。

数赛模式在通用提示之上叠加（全部由测试钉住，不靠文案）：

- 报告固定九段骨架：问题重述 / 模型假设（编号）/ 符号说明 / 模型建立 / 求解方法 /
  数值结果 / 结果验证 / 灵敏度分析 / 优缺点与改进
- 中文行文；先定"决策变量·单标量目标（带单位）·约束分类"三件套再建模
- 求解后必须代回验证并报告各约束 slack；有独立路径就交叉核对（两个求解器），
  分歧要报不许藏
- 灵敏度禁走"影子价格×增量"，必须用 `shadow_price_analysis` 并报告有效区间
- 每个假定固定的参数 ±10% 扰动，说清哪些结论活下来；报告里的数字必须来自真跑过的块

`mode` 会写进 `events.jsonl` 的 meta 记录——**事后审计看得出这单是哪档跑的**；写错
模式名在 CLI 构造时和 HTTP 层都是硬报错（400），不静默降级成通用。

## 测试

```
python tests/test_e2e_stub.py      # 18 断言：内核/事件/failover/模式，桩模型端到端
python tests/test_mode_http.py     #  9 断言：HTTP 面上的模式管线（校验、落盘、UI 接线）
```

这套测试本身就是开发时抓出 6 个真 bug 的东西：`close()` 遗留子进程、`out=""` 谎报
0 步、`Events.__init__` resume 必崩、kernel `_start` 缺失、类属性 lambda 绑定成
方法（POST 已 200 而线程内崩）、以及一版我自己写废的测试文件。**没有它们，这些都会
以"看着对"的样子发出去。**


## 与插件的关系

同目录旁 `../mathmodel` 是 ZCode 插件（主循环交 ZCode 驱动、吃它自带额度）。本
`harness` 是**独立** agent（自带循环、吃你配的端点），二者共享 `tools_modelling`
引擎所以数值一致。选哪个看你要哪种"额度来源"，不是新旧之分。

## License

MIT
