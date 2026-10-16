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

## 测试

```
python tests/test_e2e_stub.py    # 13 断言，桩模型端到端，无需网络/在线 provider
```

覆盖：bootstrap 工具真注入子进程、`note()` 从子进程回传、`final_answer` 停循环、
命名空间跨块保留、死路由→活路由 failover 且失败计入台账、全部路由挂了要干净报错
不挂起、事件落盘可 resume。**这套测试本身就是开发时抓出 4 个真 bug 的东西**：
`close()` 遗留子进程、`out=""` 谎报 0 步、`Events.__init__` resume 必崩、kernel
`_start` 缺失。没有它这些都会以"看着对"的样子发出去。

## 与插件的关系

同目录旁 `../mathmodel` 是 ZCode 插件（主循环交 ZCode 驱动、吃它自带额度）。本
`harness` 是**独立** agent（自带循环、吃你配的端点），二者共享 `tools_modelling`
引擎所以数值一致。选哪个看你要哪种"额度来源"，不是新旧之分。

## License

MIT
