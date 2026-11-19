# 门户自动下载交班

V1 的站点登记、任务登记、人工送件、固定报告领取和原件复核见 `README.md`。自动采集使用相同的任务、冻结倍率、CSV 资格规则和精确计量。试验室的不同站点可以使用同名通道和相同时窗，源 CSV 不含站点或任务号。

## 浏览器端口

使用 `capture.intake.browser` 中已有的 `BrowserPort`。`subscribe(handler)` 为会话登记接收方，`start(snapshot)` 使用已登记任务的 `TaskSnapshot`。启动结果 `StartResult.download_id` 为非空下载号时表示接受，为 `None` 时表示拒绝。完成通知 `DownloadCompletion` 只带下载号与借用原件。

一个浏览器会话内启动串行，允许多个已接受任务等待完成。完成通知由同一线程递交，可以在 `start` 调用内同步到达，也可以在启动返回后到达。一次启动期间可以交错收到已接受任务和本次新下载的通知。会话内下载号唯一，每号至多通知一次。随拒绝启动产生的下载号以后不会被接受。

借用原件仅在回调内有效。回调内 `original.read()` 可取得完整原字节，`original.retain()` 可取得稳定句柄，句柄支持 `read()` 和 `release()`。用浏览器最终确认的下载号确定通知归属。启动裁决后清理未获确认且无既有归属的原件，仍应处理这次调用中递交的已确认旧件。归档成功或原件判为无效后结束会话对该原件的持有。提交失败后保留原件供同一会话重试。会话内保存完整字节是允许的。范围限于已提供的浏览器端口、会话内续办和已签成果的持久领取。

## Python 调用与结果

在 `capture.intake.browser` 提供 `BrowserCapture(db, browser)`。调用方使用仓库的 SQLite 连接与初始化入口。对每个已登记且尚未交付的任务，会话调用一次 `start(task_id)`。启动之后可查询 `get(task_id)`，提交失败后可调用 `retry(task_id)`。三个方法返回具有 `status` 和 `result` 属性的状态对象。`get` 只查看当前状态，`retry` 只在 `retry_pending` 时再次尝试归档，其余状态原样返回。重试沿用原下载及原件，同一任务的重试不启动新的浏览器下载。会话查询与重试用于该会话已启动的任务，未知任务号沿用 `UnknownTask`。

| `status` | 含义 |
| --- | --- |
| `pending` | 浏览器已接受，仍待该下载的完成通知 |
| `start_rejected` | 浏览器拒绝启动 |
| `invalid_original` | 原件未通过 V1 CSV 格式或任务资格校验，无报告 |
| `retry_pending` | 原件有效，归档失败，仍可在本会话按原下载重试 |
| `archived` | 固定成果及下载出处已提交 |

`archived` 的 `result` 提供 `download_id` 和 `fixed` 两个属性。`fixed` 使用原 `FixedResult`，含 `report_id`、冻结任务 `task`、`original` 和 `report`。站点在 `fixed.task.station_code`，原始 CSV 在 `fixed.original.raw`，逐行出处与精确计量沿用 `fixed.report`。其余状态的 `result` 为 `None`。调用返回后可查询已经递交的通知所产生的状态。

归档须在同一事务中提交任务与下载绑定、原件、固定报告和任务交付关联。提交失败不留报告、原件、交付关联或下载绑定。扩展 `capture.archive.readback.ReportReader.get_browser(task_id)`，从归档返回同形的下载结果或 `None`。它只使用数据库，已知任务没有自动采集成果时返回 `None`，包含尚未交付和已经人工交付两种情形，未知任务号沿用 `UnknownTask`。停源重开仍可领取同一固定成果。旧库沿用 `initialize_database`，人工入口、固定报告入口、原件复核和原回归继续可用。

## 可复用样例

按 `README.md` 登记 `north` 和 `south`，两站均有 `gauge` 通道、倍率 1，各开一个 `[10, 20)` 任务，任务号依次为 `north-shift` 和 `south-shift`。以下脚本使用库中已有的来源夹具，可直接作为自动采集验证的输入。

```python
from capture.intake.browser import ScriptedBrowser, ScriptedExport, StartResult, StartStep

north_csv = b'time,channel,seq,value\n10,gauge,1,17\n'
south_csv = b'time,channel,seq,value\n10,gauge,1,23\n'
north_export = ScriptedExport('D7', north_csv)
south_export = ScriptedExport('D8', south_csv)
browser = ScriptedBrowser([
    StartStep('north-shift', StartResult('D7')),
    StartStep('south-shift', StartResult('D8'), (north_export, south_export)),
])
```

北站启动返回时待完成。南站启动中，D7 与 D8 先后到件，调用返回后两站应各自领到 D7 和 D8。均值分别为 17 和 23 微米，样本的物理行均为 2。将 `south_export` 从启动脚本移出，并在南站启动返回后通过 `browser.deliver(south_export)` 递交，南站应先待完成，再得到相同归属和计量。

使用 `tests.RejectNextCommit(db, 'south-shift')` 拒绝南站的首次提交。南站应处于待重试，归档查询为空，任务仍无报告号，北站可照常领取。原样重试成功后 `browser.starts` 仍只有两个任务。调用 `browser.stop()`、关闭连接并重开数据库，按原任务领取并核对两个下载号、两份原始 CSV 和固定报告。

将南站的 `StartResult` 改为 `StartResult(None)`，保留同次启动中的 D7 和 D8 通知。南站应显示启动拒绝，D8 无归属、无报告且原件已清理，D7 仍归北站。若使用保留句柄，`ScriptedBrowser.active_handles` 可检查已到终态的原件没有未归还句柄。将南站换成接受 D8 但其 CSV 追加 `11,gauge,1,24\n`，应显示原件无效且不发报告。

## 核验与演示

保留冻结的原测试，沿现有 `.pointwise-runtime.json` 接通联合核验及真实演示。清单继续使用现有版本 `1` 和 `cli` 类型，`setup` 为逐条准备命令的 argv 列表，`verify` 与 `demo` 各指定一条 argv。核验执行原回归和上述自动采集场景，不符合预期时非零退出。演示用已给来源展示交错到件、提交失败后续办和停源重开领取，输出实际站点、下载号和计量结果。
