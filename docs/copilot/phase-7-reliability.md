# Phase 7 — Reliability Enhancements

[总进度](README.md) · [共同设计](design.md) · [文档索引](../README.md)

状态：todo。依赖：Phase 6 验收完成。

## 进度

- [ ] P7-01 — todo：评估一图流可选 Box provider。
- [ ] P7-02 — todo：评估 MAA OperBox 本地 OCR provider。
- [ ] P7-03 — todo：验证新鲜缓存与 fallback 策略。
- [ ] P7-04 — todo：独立 fixture 和可靠性验收。

## 完成记录

尚未完成。每项 done 需记录实现文件、测试/验收证据和日期；整个阶段只有全部验收通过才标 done。

只有 Operational MVP 完成之后再做。

---

## 多 Box Provider

最终可以形成：

```text
BoxProvider
├── SklandBoxProvider
├── YituliuBoxProvider
└── MaaOperBoxProvider
```

但不要在 Phase 0 同时实现三个。

---

## 一图流

适合作为：

```text
optional provider
```

优点：

- 正式 read-only token；
- 已有完整 progression；
- 数据格式相对程序化。

但不应该成为核心 matcher 的硬 dependency。

---

## MAA OperBox

适合作为：

```text
local fallback
```

优点：

- 不需要第三方账号数据服务。

缺点：

- 需要模拟器；
- OCR；
- 扫页；
- progression 信息可能不如 Skland/API 完整。

---

## Provider fallback

未来可以讨论：

```text
Skland
  ↓ fail
Yituliu
  ↓ fail
MAA OperBox
  ↓ fail
fresh cached Box
  ↓
fail closed
```

但这个顺序必须根据实际稳定性验证决定。

不要现在提前固化。

---