# 榆川材料试验室门户采集

值班员在仪器门户选择站点、通道和当班时窗，人工下载测量 CSV，将原件送入本地 SQLite 归档。质检员凭任务号找到固定报告，可在停用门户后重开数据库，领取当时的文件、冻结倍率及计量结果，并核对原行。

## 运行

需要 Python 3.11 和标准库。在仓库根目录运行控制台提供的完整回归入口。

```sh
python3 -B check_project.py
```

独立源码导出的完整 unittest 入口为 `python3 -B -m unittest discover -s tests -t . -v`。用 `python3 -B -m capture.intake.manual` 演示登记、送件、修改口径、另一班送件、重开及复核。演示读取 `samples/shift.csv`，使用自动清理的临时数据库。

## 值班员和质检员接口

```python
from contextlib import closing
from pathlib import Path

from capture.archive.audit import ReportAuditor
from capture.archive.database import initialize_database, open_database
from capture.archive.readback import ReportReader
from capture.intake.manual import ManualCapture
from capture.stations.catalog import StationCatalog
from capture.tasks.registry import TaskRegistry
from capture.tasks.request import OpenTaskRequest
from capture.tasks.window import TimeWindow

with closing(open_database('shifts.sqlite')) as db:
    initialize_database(db)
    stations = StationCatalog(db)
    tasks = TaskRegistry(db)
    stations.upsert('north', [('gauge', 1)], active=True)
    tasks.open(OpenTaskRequest('north-day', 'north', 'gauge', TimeWindow(10, 20)))
    ManualCapture(db).submit('north-day', Path('samples/shift.csv').read_bytes())

with closing(open_database('shifts.sqlite')) as db:
    report_id = TaskRegistry(db).get('north-day').progress.report_id
    fixed = ReportReader(db).get(report_id)
    print(fixed.report.statistics.mean_um, fixed.original.raw)
    print(ReportAuditor(db).audit(report_id).passed)
```

| 接口 | 输入和结果 | 拒绝条件 |
| --- | --- | --- |
| `StationCatalog.upsert` | 站点代码、通道名与正整数微米倍率的成对清单、布尔启用状态 | `InvalidStationDefinition`，整份更新回滚或在写入前拒绝 |
| `TaskRegistry.open` | 文本任务号、站点代码、通道、整数半开时间窗。同参重投返回原快照及当前进度 | `InvalidWindow`、`UnknownStation`、`StationInactive`、`UnknownChannel`、`TaskRequestConflict` |
| `TaskRegistry.get` | 任务号，返回冻结范围及 `pending` 或 `delivered` 状态和可用报告号 | `UnknownTask` |
| `ManualCapture.submit` | 任务号和完整 `bytes`，返回签发的 `FixedResult` | `UnknownTask`、`ExportFormatError`、`ObservationEligibilityError`、`SubmissionConflict`、`PublicationError` |
| `ReportReader.get` | 整数报告号，返回固定任务、原件字节、十六进制 SHA-256、计量及出处 | `UnknownReport` 或 `ArchiveReadError` |
| `ReportAuditor.audit` | 整数报告号，返回 `passed` 以及失败时的 `reason` | 未知号抛出 `UnknownReport`。单件损坏或不一致返回失败结论 |

站点代码、任务号和通道名使用非空文本。替换站点定义只影响后续任务。停用时照常查询和送交原任务，重新启用后的新任务采用新定义。同一采样时刻的不同序号允许共同入档。

CSV 必须使用 `time,channel,seq,value` 表头，数据记录按 CSV 引用规则读取。三列整数允许符号和前导零，通道精确匹配，时窗左闭右开，序号全件唯一。原件保留 BOM、换行、引号及数字拼写。样本注明一基物理起始行，跨行字段占用的行会计入后续位置。样例用倍率 1 将 17 和 24 原单位换成 17 和 24 微米，最小值 17，最大值 24，均值为 `Fraction(41, 2)`。覆盖从时标 10 到 19。只有表头的原件生成零样本报告，最值、均值和覆盖为空。

签发使用一个事务同时写入报告、原件和任务完成关联。失败后可重交原件，同任务同字节重投返回原号，等值异字节会冲突。`open_database` 启用关联约束。`initialize_database` 可重复调用，保留已录入的站点、任务和报告。并发工作者各自打开连接。
