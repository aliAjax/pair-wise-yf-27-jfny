# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`reviewer2`、`claimant1`、`public`。

## 双人复核规则

权利主张实行双人审查：

- `submitted → under_review`（受理立案）可由一名审查员通过 `POST /api/claims/{id}/transition` 完成。
- 进入协商或给出返还/驳回结论（`under_review/negotiating` 阶段）必须两名审查员通过 `POST /api/claims/{id}/reviews` 分别登记**互不相同的审查员意见**（`vote` 为 `negotiating`、`resolved_return` 或 `rejected`，附不少于 5 字说明）；同一审查员同一轮只能投一次。
- 两张赞成票意见一致才会推进；出现反对票（意见相左）时保留该轮分歧记录（`claim_review_rounds.outcome='split'` 及全部选票），主张继续停在待复核并开启新一轮投票。
- 审查员不能给自己的主张投票；工作人员不能投票，但可通过 `GET /api/reviews` 查看每张主张的票数、待补票名单，以及在藏品详情中查看各轮意见与最终意见。
- 复核轮次、选票和最终意见仅对 `staff`/`reviewer` 可见；公众与主张人的可见范围不扩大（主张人只看到自己主张的阶段状态，看不到审查意见）。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：仅用于受理立案（`submitted → under_review`）；其余阶段会返回 `dual_review_required`。
- `POST /api/claims/{id}/reviews`：审查员登记复核票，两张一致票推进阶段，分歧票保留记录并开启新轮次。
- `GET /api/reviews`：工作人员/审查员待复核看板（票数、待补票、轮次）。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
