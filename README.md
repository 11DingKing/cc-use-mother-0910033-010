# 主诊医师授权排班

本项目维护主诊医师授权排班的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖机构合规员、执业人员、监管人员、复核专家，并明确责任覆盖约束、资格排班快照、跨院支援授权、并发预约锁定等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/scheduling/`：排班领域服务与 HTTP 接口，维护医师资质、机构注册、项目授权、可用时段与连续工作限制；排班确认时冻结资格快照并锁定责任覆盖，支持临时替班、跨院支援、授权暂停与并发预约原子更新。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动排班 HTTP 服务。
- `tests/`：契约完整性与排班服务回归测试。

## 服务接口

启动服务：`python3 tools/run_server.py --port 8080`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/institutions` | 注册机构及其高风险项目范围 |
| POST | `/projects` | 登记高风险项目与所需资质 |
| POST | `/physicians` | 登记医师与资质 |
| POST | `/physicians/{id}/qualifications` | 维护医师资质（增删），不影响已冻结快照 |
| POST | `/authorizations` | 授予项目授权（`support=true` 表示跨院支援授权） |
| POST | `/authorizations/{id}/suspend` `/resume` `/revoke` | 暂停 / 恢复 / 撤销授权，返回受影响的已确认班次 |
| POST | `/availability` | 申报医师在某机构的可用时段 |
| POST | `/shifts` | 创建高风险项目班次 |
| POST | `/shifts/{id}/confirm` | 确认排班：冻结资格快照、锁定责任覆盖 |
| POST | `/shifts/{id}/substitute` | 临时替班：替换责任人并保留完整指派历史 |
| POST | `/shifts/{id}/complete` `/cancel` | 完成（原责任人永久保留）/ 取消 |
| GET | `/shifts/{id}` `/coverage` | 班次详情 / 覆盖解释：缺口、风险、候选替代与被拒原因 |
| POST | `/shifts/{id}/bookings` | 并发预约：容量原子扣减、按预约编号幂等 |

行为要点：

- 确认或替班资格不足时返回 `409 coverage_insufficient`，`details.report` 内含覆盖缺口、候选替代与每位医师的被拒原因（资质、授权、可用时段、连续工作限制）。
- 授权暂停后，相关已确认班次标记为 `at_risk` 并暂停接受预约，需替班或恢复授权；已完成服务不受影响，原责任人永久保留。
- 确认、替班、完成、取消支持 `expected_version` 乐观并发控制；预约在锁内原子检查并扣减容量。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
