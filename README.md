# 全渠道会员复购口径与营销触达名单核验

一个仅依赖 Python 3.10+ 标准库的应用，面向会员运营分析师、身份治理人员和活动负责人。它从带版本的身份关系、同意状态、跨渠道订单/退款中生成按**入会期、地区、首单品类**划分的复购分析，并支持固定时点冻结营销名单、发送留痕、更正后提醒负责人以及未发送名单重新核验。

> 样例 ID 均为虚构。CSV 使用 UTF-8；空单元格表示来源未提供值。

## 1. 快速开始

无需安装第三方依赖：

```bash
python3 -m unittest discover -s tests -v

# 分析随仓库提供的最小样例
python3 -m repurchase --data samples analyze \
  --as-of 2026-11-25 \
  --out output/sample-analysis
```

也可以使用等价入口：

```bash
python3 app.py --data samples analyze --as-of 2026-11-25
```

打开 `output/sample-analysis/analysis_report.md` 可查看中文报告；同目录还包含机器可读的 JSON/CSV。

### 完整业务演示数据

`examples/retail_demo/input/` 提供了跨渠道合并、取消、全额退款、身份争议、缺失身份、未成熟会员、地区/品类以及营销名单数据。

```bash
DATA=examples/retail_demo/input
STATE=output/retail-demo-state

# 当前分析
python3 -m repurchase --data "$DATA" analyze \
  --as-of 2026-11-25 \
  --out output/retail-demo-analysis

# 活动当天冻结并发送 L1
python3 -m repurchase --data "$DATA" freeze-list \
  --list L1 --at 2026-11-20T10:00 --state "$STATE"
python3 -m repurchase --data "$DATA" send-list \
  --list L1 --at 2026-11-20T18:00 --state "$STATE"

# 11 月 22 日 P2 撤回授权后，用当前数据核验
python3 -m repurchase --data "$DATA" review-lists \
  --at 2026-11-25T09:00 --state "$STATE" \
  --write-analysis output/retail-demo-review-analysis
```

输出包括：

- `$STATE/sent/L1.sent.json`：只写一次的已发送名单，后续更正不会改写。
- `$STATE/alerts/list_review.json`、`list_alerts.csv`：机器可读提醒。
- `$STATE/alerts/owner_<负责人>.md`：按负责人拆分的提醒。
- 未发送的 `L2` 会生成新的 `*__reverification.json` 快照并更新当前指针；旧快照保留用于审计。

## 2. 输入文件

### 必需文件

| 文件 | 列 | 说明 |
|---|---|---|
| `identity.csv` | `member,source,person,from,to,version,state` | 带版本和有效区间的会员号—自然人关系。`to` 为空表示长期有效；日期区间按日历日含端点处理。 |
| `consent.csv` | `person,channel,state,at` | 授权/撤回事件流。固定时点取该时点之前（含）最后一条事件。 |
| `orders.csv` | `order_id,member,channel,paid,state,at` | 订单终态。支持 `completed`、`cancelled/canceled`、`refunded/fully_refunded`、`partially_refunded` 等状态。 |

身份状态中，只有规范化为 `confirmed/active/approved/merged/valid` 的关系会用于合并；`disputed/pending/review/uncertain` 不会静默合并；`revoked/unlinked/rejected` 不作为当前有效关系。同一 `(member, person)` 关系选择覆盖业务时点的最高 `version`。

### 可选治理与分析文件

- `members.csv`：`member,joined_at,region`，会员账号的入会日期和地区。
- `persons.csv`：`person,joined_at,region`，自然人级入会日期/地区，优先级高于会员账号。
- `order_items.csv`：`order_id,category,amount`，用于确定首单主营品类（金额最大的品类；无金额时按明细行数，再按品类名稳定排序）。
- `refunds.csv`：`refund_id,order_id,amount,at[,recorded_at]`，后续退款/退款到达事件。
- `exclusions.csv`：`person,member,code,reason,from,to`，员工、风控、测试号等业务排除条件及有效期。
- `campaign_lists.csv`：`list_id,name,channel,owner,status[,frozen_at,sent_at,exclude_recent_days]`。
- `campaign_recipients.csv`：`list_id,person,member`，候选接收人；至少提供 `person` 或 `member`。

`orders.csv` 也允许直接提供额外的 `region`、`category`、`refunded_amount` 列。若没有地区或品类，会分别进入 `未提供地区`、`未分类`，不会伪造维度。

## 3. 复购与观察期口径

复购定义严格实现为：

> 从第一笔当前有效的已完成正金额订单开始，在其后 90 个日历日内出现另一笔不同 `order_id` 的当前有效正金额订单；第 90 天当天计入，第 91 天不计入。

处理要点：

1. `cancelled/canceled/void` 订单永远不计入。
2. 全额退款订单不计入；`refunds.csv` 累计退款达到订单金额，或订单终态为全额退款，均判定为全额退款。
3. 部分退款后净额仍为正的订单仍为有效订单。
4. 后续退款会重新计算有效订单集合：
   - 第二笔复购单被全额退款时，会继续寻找窗口内其他有效订单；
   - 第一笔单被全额退款时，以次笔有效订单作为新首单并整体移动观察窗。
5. 分析截止点之后的订单/退款不进入该时点口径。

### 身份不确定时不做“未复购”结论

系统区分事件发生时身份状态和截止日当前身份状态：

- 订单日存在有效 `confirmed` 关系时，跨渠道订单可以合并；关系在订单日之后到期/撤销，不会改写历史交易事实，但会产生当前名单影响。
- 争议、冲突、缺失或在订单日无效的关系不会被强制合并。
- 已满观察期但身份不能确认的顾客进入 `indeterminate`，而不是 `no_repeat`。
- 系统会沿争议关系计算“可能加入哪些额外订单”，并在影响登记中给出 `possible_extra_orders`。
- 对满期群组同时输出：
  - `repurchase_rate_decided`：所有已出现复购的顾客 /（已复购 + 身份确定的未复购）；提前复购但窗口尚未自然走完的顾客，阳性结果已经确定，因此进入此口径；
  - `repurchase_rate_decided_90d`：仅以已满 90 天的顾客为分母，严格对应完整 90 天队列；
  - `repurchase_rate_lower_bound_90d`：所有满期顾客为分母，身份待定者按未复购处理（仅作下界）；
  - `repurchase_rate_upper_bound_90d`：身份待定者按可能复购处理（上界）。

这避免了把“无法关联”错误解释成“确定没有复购”。

### 成熟度和共同观察期

顾客级结果在 `customer_metrics.csv` 中提供：

- `observed_days`：自首笔有效订单至截止日的日历天数；
- `window_end`：90 天窗口结束日；
- `mature`：是否已满 90 天；
- `days_remaining`：距窗口成熟还剩多少天；
- `repeat_status`：`repeat`、`no_repeat`、`observing`、`indeterminate`、`no_valid_order`。

群组级结果在 `segment_metrics.csv` 中提供未成熟顾客数、窗口未满数、待继续观察数、最短/最长观察天数和 `complete_observation_days`（群组中所有首单顾客均已完整观察的最大天数，封顶 90）。

`common_period_comparison.csv` 对每个 H（默认 30/60/90）只纳入首单后已满 H 天的顾客，并计算 H 天内复购。这样老会员和新会员按相同观察长度比较，而不是让新队列天然显得复购率低。

## 4. 名单冻结、发送和更正治理

冻结某个名单时，系统按冻结时点评估：

1. 渠道同意事件的最新状态必须是 `active`；无记录、未知状态或 `withdrawn` 均不可发送。
2. `member` 必须能在冻结时点解析到唯一确认的 `person`；身份争议、冲突或缺失会保守排除。
3. `exclusions.csv` 中在冻结日有效的业务排除码必须为空。
4. 若名单配置了 `exclude_recent_days`，冻结前 N 天内不能有当前有效订单。

冻结快照保存每个接收人的资格、原因、同意状态、身份状态、评估时间和输入数据指纹。

治理语义：

- **已发送**：`sent/<list_id>.sent.json` 是只写一次的官方记录。之后授权撤回、身份争议或退款更正只生成提醒，不修改该文件。
- **未发送**：`review-lists` 会用当前花名册和当前数据重新核验，写入新的 `reverification` 快照并更新状态指针；旧快照保留审计。
- **负责人提醒**：`alerts/owner_<owner>.md` 和 `list_alerts.csv` 指出名单、接收人、冻结时资格、当前资格、变化原因和关联的数据更正影响。
- 当前分析可通过 `analyze` 或 `review-lists --write-analysis` 重新生成；若输出目录已有 `analysis.json`，`analyze` 还会写出 `analysis_diff.json/csv`，显示哪些顾客状态和群组指标被更正。

## 5. Python API

```python
from repurchase import load_input, run_analysis
from repurchase.reporting import write_analysis_outputs
from repurchase.campaigns import CampaignService

data = load_input("examples/retail_demo/input")
result = run_analysis(data, as_of="2026-11-25", horizons=(30, 60, 90))
write_analysis_outputs(result, "output/analysis")

service = CampaignService(data, "output/campaign-state")
snapshot = service.freeze_list("L1", at="2026-11-20T10:00")
service.mark_sent("L1", at="2026-11-20T18:00")

new_data = load_input("examples/retail_demo/input")  # 读取更正后的 CSV
review = CampaignService(new_data, "output/campaign-state").review_lists(
    at="2026-11-25T09:00"
)
```

## 6. 输出文件

一次分析会生成：

- `analysis.json`：完整类型化结果和影响登记。
- `customer_metrics.csv`：每个身份组/顾客的首单、观察天数、成熟度、身份、同意和复购状态。
- `segment_metrics.csv`：入会期 × 地区 × 品类指标。
- `common_period_comparison.csv`：30/60/90 天共同观察期比较及不确定区间。
- `impact_register.csv`：身份争议/到期、授权撤回、取消/退款影响的统计群组。
- `analysis_report.md`：中文运营报告。
- `analysis_diff.json/csv`：当输出目录已有旧结果时，额外生成前后差异。

## 7. 项目结构

```text
repurchase/
  dates.py       # 日期、队列期间、90 天边界
  io.py          # CSV schema 校验和加载
  identity.py    # 版本化、有效期身份图与可能关系闭包
  consent.py     # 时点同意状态
  orders.py      # 取消、退款、净额和有效订单
  analysis.py    # 复购、成熟度、群组/共同窗口/影响/diff
  campaigns.py   # 冻结、发送留痕、重新核验、负责人提醒
  reporting.py   # JSON/CSV/Markdown 输出
  cli.py         # 命令行接口
samples/                      # 题目交付的最小结构样例
examples/retail_demo/input/   # 覆盖完整业务场景的合成数据
tests/                        # 样例结构测试和应用行为测试
```

Python 3 标准库足以检查输入并运行应用：

```bash
python3 -m unittest discover -s tests -v
```
