# 主诊医师授权排班

本项目维护主诊医师授权排班的领域约定、角色边界与样例数据，并提供 Python 服务端实现，供后端服务、接口和自动化验证统一使用。覆盖机构合规员、执业人员、监管人员、复核专家，落实四项关键约束：

1. **责任覆盖约束**：高风险项目的每个服务时段必须有一名资格、机构注册、项目授权三项齐备的主诊医师在岗，否则无法确认排班或接受预约。
2. **资格排班快照**：排班确认（及已确认时段的替班）时冻结资质/注册/授权视图为不可变快照，责任判定以快照为准；事后资质变化不改变历史。
3. **跨院支援授权**：区分院内授权（`institution`，凭执业机构注册）与跨院支援授权（`cross_support`，凭来源机构注册 + 授权窗口），简单换班超出注册范围将被拒绝并给出原因。
4. **并发预约锁定**：所有用例在单事务锁内完成"校验 + 写入"，预约容量扣减、重复预约、乐观版本号（`expected_version`）均为原子操作。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/scheduling/`：排班服务端（仅标准库）。
  - `models.py`：状态常量与文档构造器；
  - `repository.py`：线程安全内存仓库与事务边界；
  - `service.py`：领域规则——资格/注册/授权评估、覆盖解释、快照冻结、替班、工时限制、原子预约；
  - `httpapi.py` / `server.py`：JSON HTTP 接口与启动入口。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tools/smoke_http.py`：HTTP 端到端冒烟（暂停→覆盖不足→跨院替班→并发抢占→完成留痕）。
- `tests/`：契约与排班服务回归测试。

## 运行

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

契约检查：`python3 tools/check_contract.py domain/contract.json`

HTTP 冒烟：`python3 tools/smoke_http.py`

启动服务（内存存储，重启清空）：

```bash
PYTHONPATH=src python3 -m scheduling.server --host 0.0.0.0 --port 8080
```

## 接口一览（JSON）

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /organizations`、`POST /physicians` | 机构 / 医师登记 |
| `POST /physicians/{id}/qualifications` | 维护执业资格（含到期时间） |
| `POST /physicians/{id}/work-rule` | 连续工作天数、班间休息限制 |
| `POST /registrations` | 机构执业注册与执业范围 |
| `POST /projects` | 高风险项目及其资格要求 |
| `POST /authorizations` | 院内 / 跨院支援项目授权 |
| `POST /authorizations/{id}/suspend` `/resume` | 授权暂停 / 恢复；暂停返回受影响时段与候选替代 |
| `POST /sessions`、`GET /sessions/{id}/coverage` | 开时段；查看覆盖解释（不合格原因 + 全部候选医师） |
| `POST /sessions/{id}/assignments` | 派班提议（实时三维复验 + 工时/冲突校验） |
| `POST /sessions/{id}/confirm` | 确认排班：全部复验通过后原子冻结资格快照、锁定责任覆盖 |
| `POST /sessions/{id}/substitute` | 临时替班 / 跨院支援；已确认时段即时复验并冻结替班快照 |
| `POST /sessions/{id}/appointments` | 原子预约（容量、重复、版本、覆盖复验） |
| `POST /sessions/{id}/complete` | 登记完成：按当时快照生成服务记录，原责任人永久保留 |
| `GET /snapshots/{id}`、`GET /service-records` | 快照与责任留痕查询 |

冲突类错误统一为 `409`，响应体形如：

```json
{
  "error": {
    "code": "coverage_insufficient",
    "message": "责任覆盖不足，不能接受预约；请等待替班或改约其他时段",
    "reasons": [{"code": "authorization_suspended", "message": "授权 auth_000001 已暂停：飞行检查"}],
    "details": {"coverage": {"covered": false, "candidates": []}}
  }
}
```
