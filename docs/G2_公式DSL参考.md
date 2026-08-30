# G2-4 公式 DSL 参考（G2-4 DSL 语法手册）

> 交付物：`backend/app/indicators/dsl.py`。把「类通达信公式」字符串解析为
> G7 选股条件树 JSON，供 G8 自然语言与人工输入共用。

## 语法 BNF（简化）

```
expr      := or_expr
or_expr   := and_expr ( 'OR' and_expr )*
and_expr  := not_expr ( 'AND' not_expr )*
not_expr  := 'NOT' not_expr | '(' or_expr ')' | leaf
leaf      := operand op number
operand   := FIELD | INDICATOR '(' args ')' | INDICATOR.OUTPUT '(' args ')'
FIELD     := C | O | H | L | V | VOLUME          # 收盘/开/高/低/量
op        := > | >= | < | <= | = | == | != | <>
number    := 整数 | 小数 | 负数
args      := number (',' number)*                  # 位置参数，映射指标 spec.params
```

## 支持示例

| 公式 | 语义 | 产出的条件树 |
|---|---|---|
| `C > MA(20)` | 收盘站上 20 日均线 | compare 双序列叶子 |
| `MACD.DIF(12,26,9) > 0` | MACD DIF 大于 0 | indicator {name:macd, output:dif} |
| `KDJ.K(9) > 80` | KDJ K 值超买 | indicator {name:kdj, output:k, params:{n:9}} |
| `C > 5 AND VOLUME > 100000` | 价格与量双条件 | and 树 |
| `NOT WR(14) < -80` | 威廉不超卖 | NOT 节点取反 |
| `5 < C` | 数值在左 | 自动翻转 → C > 5 |

## 约束（v1）

- 仅支持「字段/指标 vs 数值」比较；指标对指标比较（`MA(5) > MA(10)`）暂不支持，
  请用金叉/死叉自然语言（G8）生成 compare 叶子。
- 指标名大小写不敏感（解析后转小写匹配注册表）；输出列名大小写不敏感。
- 参数为位置参数，顺序对应注册表 `spec.params`（如 kdj 的 `n`、boll 的 `n,m`）；
  名为 `period` 的参数在条件树中映射为 `win`（与 /market/screen 契约一致）。
- 错误（未知指标/缺右操作数/非法 token）→ ValueError，带行列位置。

## 调用方式

```python
from app.indicators.dsl import parse
cond = parse("C > MA(20) AND RSI(14) < 30")
# cond 直接可作为 /market/screen 的 conditions 参数
```

后端未暴露独立 DSL 端点——DSL 是 G8 的底层实现（`POST /market/screen/nl`），
以及未来前端公式输入框的解析入口。
