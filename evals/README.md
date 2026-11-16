# 预定义案例与评分口径

CP03 在检索实现和调参前生成并冻结 8 个开发 incident 与 12 个预定义验收 incident。
`data/fixtures/` 只有运行观测，`data/knowledge/` 只有排障规范。
`evals/dev/`、`evals/acceptance/` 为输入，`evals/expected/` 与 `rubric.json` 是工具不可访问的标准答案和评分规则。

开发与验收共用故障机制类别，但每个 incident 独立编号、独立事件组，未将同一 incident 变体拆到两个集合。
这些材料由参与开发的代理制作，不能声称严格独立盲测。CP04–CP09 只使用开发集。
CP10 前冻结代码、提示词、配置和语料，确认 manifest 未变后再用验收集，两方案各跑两次，全部保留原始结果和人工核对。

冻结完整性检查：`uv run --frozen python scripts/freeze_dataset.py --check`。
既有 manifest 不允许脚本静默覆盖；数据确需更新时必须另立版本并披露旧评价用途变化。
