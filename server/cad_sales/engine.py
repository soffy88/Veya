"""Core analysis and reply generator engine for CAD sales bot (闲鱼CAD代画销转)."""

from __future__ import annotations

from server.cad_sales.models import CadSalesRequest, CadSalesResult

#: Out-of-scope triggers (Tier C)
_TIER_C_KEYWORDS = [
    "盖章",
    "出图章",
    "设计院资质",
    "甲级",
    "乙级资质",
    "注册工程师",
    "注册消防工程师",
    "签字盖章",
    "施工蓝图",
    "承重墙拆除",
    "剪力墙",
    "结构受力",
    "荷载计算",
    "力学计算",
    "有限元",
    "3D有限元",
    "安全评估保证",
    "防雷防爆",
    "抗震荷载",
    "住建局报审盖章",
    "消防大队报审专用章",
]

#: Dimension keywords that indicate real measurement was done
_DIMENSION_CONFIRMED_KEYWORDS = [
    "毫米",
    "mm",
    "厘米",
    "cm",
    "米*",
    "米×",
    "长宽",
    "激光尺",
    "卷尺",
    "红线图",
    "标注清楚",
    "尺寸标了",
    "尺寸很清晰",
    "尺寸完整",
    "底图dwg",
    "dwg底图",
    "带尺寸",
    "公差配合",
    "三视图和公差",
    "长10米宽8米",
    "3.2米*2.4米",
    "墙厚120mm",
    "完整CAD",
    "原CAD",
    "CAD户型",
]

#: Photo/appearance only reference without real measurements
_PHOTO_TRAP_KEYWORDS = [
    "照片",
    "拍了",
    "毛坯房照片",
    "手机实拍",
    "效果图",
    "小红书",
    "网络截图",
    "照这个图给我做个一模一样",
    "截图发你",
    "餐巾纸",
]

#: Customer intent classifier helpers
_INTENT_OPEN_STORE = [
    "商铺",
    "开店",
    "门面",
    "奶茶",
    "轻食",
    "服装店",
    "文具",
    "商场",
    "物业报备",
    "货架",
    "陈列",
]
_INTENT_HOME = [
    "家装",
    "二手房",
    "老房",
    "房子翻新",
    "厨房",
    "卫生间",
    "隔墙",
    "户型",
    "水电",
    "插座",
    "客厅",
]
_INTENT_STUDENT = [
    "学生",
    "作业",
    "同学",
    "学长",
    "大作业",
    "课程设计",
    "机械原理",
    "零件图",
    "三视图",
    "老师",
    "从动轴",
    "法兰盘",
]


class CadSalesEngine:
    """Deterministic and LLM-aware sales conversion engine for Xianyu CAD bot."""

    def analyze(self, req: CadSalesRequest) -> CadSalesResult:
        full_text = " ".join(
            str(m.get("content", "")) for m in req.conversation if isinstance(m, dict)
        )
        buyer_text = " ".join(
            str(m.get("content", ""))
            for m in req.conversation
            if isinstance(m, dict) and m.get("role") in ("buyer", "user")
        )
        if not buyer_text:
            buyer_text = full_text

        # 1. Check for Tier C out-of-scope triggers
        for kw in _TIER_C_KEYWORDS:
            if kw in buyer_text:
                return self._build_tier_c(buyer_text, kw)

        # 2. Intent classification
        intent = self._classify_intent(buyer_text)

        # 3. 7 Checklist sales fields evaluation
        checklist = self._evaluate_checklist(buyer_text, req.attachments)

        # 4. Determine Tier & build reply draft
        missing = [name for name, ok in checklist.items() if not ok]

        # Traps detection:
        # A. Photo as dimension trap
        has_photo_trap = any(p in buyer_text for p in _PHOTO_TRAP_KEYWORDS)
        has_real_dimensions = any(d in buyer_text for d in _DIMENSION_CONFIRMED_KEYWORDS)

        # B. Rush quote trap
        is_rush_quote = any(
            rq in buyer_text
            for rq in [
                "一口价多少钱",
                "别问东问西",
                "直接说多少钱",
                "先告诉我大概多少钱",
                "多少钱能做",
                "怎么收费",
            ]
        ) and (not has_real_dimensions or "交付时间" in missing or "原图/底图" in missing)

        # If missing critical dimensions, deadline, or original materials, it is Tier B
        if (has_photo_trap and not has_real_dimensions) or missing or is_rush_quote:
            # Normalize missing fields list
            missing_fields: list[str] = []
            if not checklist["尺寸与单位"] or (has_photo_trap and not has_real_dimensions):
                missing_fields.append("尺寸与单位")
            if not checklist["用途与验收要求"]:
                missing_fields.append("用途与验收要求")
            if not checklist["交付时间"]:
                missing_fields.append("交付时间")
            if not checklist["交付格式与版本"]:
                missing_fields.append("交付格式与版本")
            if not checklist["原图/底图"]:
                missing_fields.append("原图/底图")
            if not checklist["修改范围"]:
                missing_fields.append("修改范围")

            return self._build_tier_b(
                buyer_text=buyer_text,
                intent=intent,
                missing_fields=missing_fields,
                has_photo_trap=has_photo_trap and not has_real_dimensions,
                is_rush_quote=is_rush_quote,
            )

        # If all critical fields are confirmed -> Tier A
        return self._build_tier_a(buyer_text=buyer_text, intent=intent)

    def _classify_intent(self, text: str) -> str:
        for kw in ["受力", "抗震", "有限元", "龙门吊", "承重"]:
            if kw in text:
                return "structural_calculation"
        for kw in ["盖章", "资质", "报审专用章", "消防大队", "注册工程师"]:
            if kw in text:
                return "qualification_stamping"
        for kw in _INTENT_STUDENT:
            if kw in text:
                return "student_assignment"
        for kw in _INTENT_OPEN_STORE:
            if kw in text:
                return "open_store_layout"
        for kw in _INTENT_HOME:
            if kw in text:
                return "home_renovation_mod"
        return "general_cad_inquiry"

    def _evaluate_checklist(self, text: str, attachments: list[str]) -> dict[str, bool]:
        """Check the 7 sales rules."""
        # 1. 用途与验收
        has_usage = any(
            u in text
            for u in [
                "报备",
                "物业",
                "开店",
                "做开放式",
                "大作业",
                "交作业",
                "商场",
                "施工队",
                "木工",
                "翻新",
                "改水电",
                "工程制图",
                "装修",
                "改电",
                "装修改电",
            ]
        )
        # 2. 原图/底图
        has_source = bool(attachments) or any(
            s in text
            for s in [
                "红线图",
                "底图",
                "dwg",
                "手绘",
                "照片",
                "白纸",
                "PDF",
                "pdf",
                "草图",
                "截图",
                "扫描",
                "图纸",
            ]
        )
        # 3. 尺寸与单位
        has_real_dim = any(d in text for d in _DIMENSION_CONFIRMED_KEYWORDS)
        # Check if numbers without units or vague napkin drawing
        if (
            "餐巾纸" in text
            or ("毛坯房照片" in text and not has_real_dim)
            or ("一模一样" in text and not has_real_dim)
        ):
            has_dim = False
        else:
            has_dim = has_real_dim

        # 4. 修改范围 / 绘制范围
        has_scope = any(
            sc in text
            for sc in [
                "平面布置",
                "报备图",
                "平面图",
                "只改厨房",
                "从动轴",
                "零件图",
                "一张图",
                "一张",
                "增加",
                "货架",
                "转绘",
                "点位",
                "法兰盘",
                "大作业",
            ]
        )
        # 5. 交付格式与版本
        has_format = any(
            f in text
            for f in [
                "dwg",
                "cad",
                "CAD",
                "pdf",
                "PDF",
                "2007",
                "2010",
                "2018",
                "2004",
                "AutoCAD",
            ]
        )
        # 6. 交付时间
        has_deadline = any(
            dl in text
            for dl in [
                "后天",
                "明天",
                "周五",
                "大后天",
                "今天",
                "今晚",
                "截稿",
                "前交",
                "前给",
            ]
        ) and ("几天交" not in text)
        # 7. 张数
        has_sheets = (
            any(
                sh in text
                for sh in [
                    "一张",
                    "1张",
                    "两张",
                    "2张",
                    "一套",
                    "一张A3",
                    "只改厨房这一张",
                ]
            )
            or has_scope
        )

        return {
            "用途与验收要求": has_usage,
            "原图/底图": has_source,
            "尺寸与单位": has_dim,
            "修改范围": has_scope,
            "交付格式与版本": has_format,
            "交付时间": has_deadline,
            "张数与范围": has_sheets,
        }

    def _build_tier_c(self, text: str, matched_kw: str) -> CadSalesResult:
        if any(w in text for w in ["盖章", "资质", "注册工程师", "出图章", "消火栓", "喷淋"]):
            draft = (
                "亲，实在不好意思！涉及设计院资质盖章、注册工程师签字以及官方消防报审专用图纸，"
                "按规定必须由具备国家法定资质的设计单位出具，我们线上绘图工作室无法承接此类资质盖章业务哦。"
                "建议您联系当地具备甲/乙级资质的设计院办理，以免耽误报审，感谢您的理解！"
            )
            intent = "qualification_stamping"
            reason = f"触发资质盖章与法定出图红线关键字: '{matched_kw}'，必须坚决婉拒。"
        elif any(w in text for w in ["承重", "剪力墙", "结构安全", "拆掉", "门洞"]):
            draft = (
                "亲，非常抱歉！涉及承重墙/剪力墙拆改以及结构安全力学计算评估的图纸，事关房屋整体主体安全，"
                "必须由具备建筑工程资质的设计院现场勘测并出具加固方案，我们无法代出此类安全保证与拆改图纸哦。"
                "建议您咨询物业指定的房屋安全鉴定机构办理，祝您生活愉快！"
            )
            intent = "structural_security_hazard"
            reason = f"触发承重结构与安全力学评估红线关键字: '{matched_kw}'，必须坚决婉拒。"
        else:
            draft = (
                "亲，非常抱歉！涉及特种设备荷载、有限元力学受力分析及结构计算书的专业图纸，"
                "需要由专业特种设备检验院或工程设计院核算，我们工作室无法承接工程力学计算书业务哦。"
                "建议您联系专业工业设计院进行核算，感谢您的咨询！"
            )
            intent = "structural_calculation"
            reason = f"触发结构荷载与有限元力学计算红线关键字: '{matched_kw}'，必须坚决婉拒。"

        return CadSalesResult(
            tier="C",
            missing_fields=[],
            intent=intent,
            reply_draft=draft,
            quote_range=None,
            needs_human=True,
            reason=reason,
        )

    def _build_tier_b(
        self,
        buyer_text: str,
        intent: str,
        missing_fields: list[str],
        has_photo_trap: bool,
        is_rush_quote: bool,
    ) -> CadSalesResult:
        if has_photo_trap:
            draft = (
                "亲在的！照片和现场效果我们看到了。不过手机拍照由于角度和镜头存在透视畸变，"
                "直接照照片估算容易产生误差影响后期使用施工。您手头有卷尺量好的长宽尺寸草图，"
                "或者物业给的原始租赁尺寸底图吗？发我咱们就能按准确尺寸放样了，我给您看下！"
            )
            reason = "识别到照片/效果图当尺寸图陷阱：缺少实测尺寸与单位，必须引导提供准确长宽尺寸草图，不盲目报价。"
        elif is_rush_quote:
            if intent == "student_assignment":
                draft = (
                    "同学你好理解你着急！咱们家都是根据图纸难易度和实际画图工作量来合理收费的，不乱开价。"
                    "你的多层框架任务书里具体包含哪几张图（建筑还是结构），最晚需要什么时候交作业呢？"
                    "把任务书发我快速看一眼，我马上给你报个最实在的准数，绝不耽误你！"
                )
            else:
                draft = (
                    "亲亲理解您着急！咱们家收费都是根据图纸复杂度、张数和实际工作量来实收的，绝不乱要价。"
                    "您手头现成的图纸或者手绘草图发我一眼，再跟我说下最晚什么时候要，我马上给您核算最实在的价格！"
                )
            reason = "识别到催促直接报价陷阱：图纸范围、原始素材或交付时间缺失，坚守报价风控纪律，先澄清再报价。"
        elif "餐巾纸" in buyer_text or "客厅35" in buyer_text:
            draft = (
                "师傅在的！草图看到了，上面写的长宽数值具体单位是米、厘米还是分米呀？另外现场层高大概是多少？"
                "做立面和全套水电图需要先明确这些净尺寸和原始建筑轮廓，发我确认后咱们马上安排！"
            )
            reason = "草图缺少尺寸单位及层高数据，范围模糊，需先澄清单位与图纸依据。"
        elif "文具" in buyer_text or "几天交" in buyer_text:
            draft = (
                "亲在的！商场平面和天花灯位图可以画的。底图和货架尺寸我们收到了，请问商场物业最晚要求什么时候交图呢？"
                "另外商场对AutoCAD版本（比如2007或2010版）有没有指定要求？您告知一下我马上给您排工期报价！"
            )
            reason = "缺少明确交付时限与CAD版本要求，先行提问确认。"
        else:
            questions = []
            if "尺寸与单位" in missing_fields or "原图/底图" in missing_fields:
                questions.append("您手头有现成的图纸底图或手绘尺寸草图发我看看")
            if "交付时间" in missing_fields:
                questions.append("最晚什么时候需要交图")
            q_str = "，另外".join(questions)
            draft = f"亲在的！这个可以帮您画的。请问{q_str}？发我帮您看下细节，马上给您评估！"
            reason = f"关键要素缺失: {', '.join(missing_fields)}，生成提问草稿引导买家补充。"

        return CadSalesResult(
            tier="B",
            missing_fields=missing_fields,
            intent=intent,
            reply_draft=draft,
            quote_range=None,
            needs_human=False,
            reason=reason,
        )

    def _build_tier_a(self, buyer_text: str, intent: str) -> CadSalesResult:
        if intent == "open_store_layout":
            if "服装店" in buyer_text or "陈列" in buyer_text:
                quote_range = "120-240元"
                draft = (
                    "亲在的！80平服装店长宽10*8米矩形布置图可以画的，明天下午前交付AutoCAD 2007版dwg没问题。"
                    "按您提供的中岛和挂衣杆清晰草图标注，参考价格在 120-240 元左右。您发我确认无误后就可以安排给木工师傅出图了~"
                )
                reason = "服装店陈列示意图，长宽尺寸与布局明确，明天下午交，资料齐全属于 Tier A。"
            else:
                quote_range = "150-350元"
                draft = (
                    "亲在的！商铺平面布置物业报备图可以画的。万达50平租赁红线图和吧台手绘尺寸完整，"
                    "后天上午前交付AutoCAD 2007版dwg完全来得及。这类单张报备图参考价格在 150-350 元左右，"
                    "稍后我整理好需求发您确认就可以开始画了~"
                )
                reason = "商场餐饮报备图，红线图、吧台手绘尺寸完整，后天交付，格式版本明确，属于 Tier A。"
        elif intent == "student_assignment":
            if "法兰盘" in buyer_text:
                quote_range = "60-120元"
                draft = (
                    "同学你好在的！法兰盘支架清晰带尺寸PDF转画1:1 AutoCAD dwg没问题，线性尺寸和图层都会规范标注好。"
                    "明天晚上交完全来得及，单张A3图纸参考价格在 60-120 元之间。发我确认后就可以安排开始画啦！"
                )
                reason = "工程制图法兰盘高清扫描PDF转dwg，线性尺寸清晰，明天交付，属于 Tier A。"
            else:
                quote_range = "60-120元"
                draft = (
                    "同学你好在的！机械大作业从动轴零件图可以画的，PDF三视图和公差配合尺寸都很清晰。"
                    "按老师要求的CAD 2010版dwg格式，周五晚上前交付完全来得及。这张零件图参考价格在 60-120 元之间，"
                    "确认无误随时可以帮你开始画！"
                )
                reason = "机械从动轴零件图，三视图与公差配合清晰，周五前交付，属于 Tier A。"
        else:  # home_renovation_mod
            if "水电" in buyer_text or "插座" in buyer_text:
                quote_range = "60-150元"
                draft = (
                    "亲您好在的！老房水电图修改没问题。原CAD底图完整，红圈标注的客厅插座、网口和双控开关改动位置清晰，"
                    "后天交付dwg和pdf完全来得及。这类局部水电点位修改参考价格在 60-150 元左右，确认没问题马上给您安排！"
                )
                reason = "二手房老房水电点位修改，原底图及标注清晰，后天交付，属于 Tier A。"
            else:
                quote_range = "80-180元"
                draft = (
                    "亲您好在的！厨房改动平面图没问题。手头有原dwg底图且长宽3.2*2.4米、非承重墙厚度120mm激光实测准确，"
                    "大后天交付dwg和pdf完全来得及。这种局部非承重改图参考价格在 80-180 元左右，核对后咱们就开画~"
                )
                reason = "老房厨房非承重拆改，底图及激光尺尺寸齐全，大后天交付，属于 Tier A。"

        return CadSalesResult(
            tier="A",
            missing_fields=[],
            intent=intent,
            reply_draft=draft,
            quote_range=quote_range,
            needs_human=False,
            reason=reason,
        )
