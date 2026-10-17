# Live Box End-to-End Run Evidence: Marketplace-Seller Console -> Veya CAD Bot

- **Timestamp**: 2026-10-17T07:56:02Z
- **Architecture Topology**: In-Memory Marketplace Console -> Temporary Local Veya App (`http://127.0.0.1:19876/api/v1/cad-sales/reply`)
- **Feature Flag**: `VEYA_CAD_SALES_BOT=1`, `MARKETPLACE_VEYA_DRAFTS=1`
- **Compliance Rule**: `INV-019` strictly verified — drafts are stored as PENDING `StatusDraft` (`DRAFT`), never auto-sent.

## Verified Inbound -> Draft Scenarios

### 1. Case 1: 商铺报备图资料齐全
- **Buyer Message**: `我在万达租了个50平轻食奶茶店，物业催我交商铺平面布置报备图。我有商场给的租赁红线图和手绘的吧台设备摆放尺寸，单位都是毫米标注清楚了，需要转成dwg格式AutoCAD 2007版，后天上午前要交。麻烦看下能画吗？`
- **Draft ID**: `2e31c5c0-001c-4e58-8787-05d1f4f73e7e`
- **Draft Lifecycle Status**: `DRAFT` (PENDING human approval)
- **Suggested By**: `ai`
- **Generated Draft Content for Operator**:
```
【CAD销转建议 | Tier A | 参考价: 150-350元】
亲在的！商铺平面布置物业报备图可以画的。万达50平租赁红线图和吧台手绘尺寸完整，后天上午前交付AutoCAD 2007版dwg完全来得及。这类单张报备图参考价格在 150-350 元左右，稍后我整理好需求发您确认就可以开始画了~
```

### 2. Case 2: 仅有照片询问价格
- **Buyer Message**: `老板，我租了个门面准备开服装店，我刚拍了4张毛坯房照片发你了，你看下帮我画个平面图要多少钱？`
- **Draft ID**: `5fd73058-1cc4-4ca5-983f-db76696e5a7f`
- **Draft Lifecycle Status**: `DRAFT` (PENDING human approval)
- **Suggested By**: `ai`
- **Generated Draft Content for Operator**:
```
【CAD销转建议 | Tier B | 待补: 尺寸与单位, 用途与验收要求, 交付时间, 交付格式与版本】
亲在的！照片和现场效果我们看到了。不过手机拍照由于角度和镜头存在透视畸变，直接照照片估算容易产生误差影响后期使用施工。您手头有卷尺量好的长宽尺寸草图，或者物业给的原始租赁尺寸底图吗？发我咱们就能按准确尺寸放样了，我给您看下！
```

### 3. Case 3: 消防资质盖章超范围
- **Buyer Message**: `我们酒店KTV包厢改造，消防大队来检查需要补一套喷淋和消火栓施工蓝图，必须带建筑消防甲级设计院的出图专用章和二级注册消防工程师签字盖章，下周一去住建局窗口报审。你们能盖章出图吗？钱好商量，可以给高价。`
- **Draft ID**: `aaa4f8cb-6e87-42b7-b56d-0f680ca871a8`
- **Draft Lifecycle Status**: `DRAFT` (PENDING human approval)
- **Suggested By**: `ai`
- **Generated Draft Content for Operator**:
```
【CAD销转建议 | Tier C】
亲，实在不好意思！涉及设计院资质盖章、注册工程师签字以及官方消防报审专用图纸，按规定必须由具备国家法定资质的设计单位出具，我们线上绘图工作室无法承接此类资质盖章业务哦。建议您联系当地具备甲/乙级资质的设计院办理，以免耽误报审，感谢您的理解！
```
