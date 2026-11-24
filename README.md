# 全渠道会员复购口径与营销触达名单核验

合成会员身份关系、同意状态和跨渠道订单样例。所有用户均为虚构标识。

All example identifiers and values are synthetic. CSV files use UTF-8. The first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `identity.csv` contains 带版本和有效日期的会员号关联声明。 Columns are `member`, `source`, `person`, `from`, `to`, `version`, `state`.
- `consent.csv` contains 渠道营销授权及撤回记录。 Columns are `person`, `channel`, `state`, `at`.
- `orders.csv` contains 会员交易及订单终态。 Columns are `order_id`, `member`, `channel`, `paid`, `state`, `at`.

Python 3 standard library is sufficient to inspect these inputs. Run `python3 -m unittest discover -s tests -v` to check the delivered file structures and example references.
