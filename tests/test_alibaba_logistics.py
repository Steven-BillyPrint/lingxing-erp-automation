import asyncio

import pytest

from shipment_automation.alibaba_logistics import (
    FULL_ROUTE_SERVICE_LINES,
    LogisticsFieldGroup,
    apply_logistics_detail_to_candidate,
    classify_tracking_candidate,
    extract_logistics_field_groups,
    is_full_route_service_line,
    is_real_overseas_carrier,
    merge_logistics_detail_sources,
    parse_logistics_detail_from_field_groups,
    parse_logistics_detail_from_json_payloads,
    parse_logistics_detail_from_text,
    is_not_ready_logistics_status,
    logistics_readiness_decision,
    normalize_carrier_name,
    normalize_service_line,
    tracking_number_matches_carrier,
)
from shipment_automation.models import (
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    LOGISTICS_WAITING,
    LOGISTICS_BLOCKED,
    LogisticsDetail,
    ShipmentCandidate,
)


def _candidate():
    return ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        shipment_tag_name="帐篷标发",
    )


def test_logistics_not_ready_statuses_include_arrived_warehouse():
    for status in [
        "待揽收",
        "已揽收",
        "已入库",
        "查询失败",
        "未出库",
        "货物抵达仓库",
        "订单关闭",
        "订单取消",
        "订单中止",
        "已核查",
        "已取消",
    ]:
        assert is_not_ready_logistics_status(status) is True
        decision = logistics_readiness_decision(
            LogisticsDetail(
                logistics_no="ALS01781406025",
                status_text=status,
            )
        )
        assert decision.logistics_state == LOGISTICS_WAITING
        assert decision.should_continue is False


def test_service_line_whitelist_normalizes_prefix_case_space_and_dashes():
    assert normalize_service_line(" 无忧  Express–HK Saver ") == "expresshksaver"
    assert is_full_route_service_line("无忧 Express–HK Saver") is True
    assert is_full_route_service_line("无忧全球普货专线") is False
    assert len(FULL_ROUTE_SERVICE_LINES) == 12
    assert all(is_full_route_service_line(value) for value in FULL_ROUTE_SERVICE_LINES)


def test_query_failed_status_stays_waiting_even_with_tail_fields():
    detail = LogisticsDetail(
        logistics_no="ALS01807515431",
        status_text="查询失败",
        carrier="FedEx",
        international_tracking_no="1234567890",
        actual_total="CNY 100.00",
        chargeable_weight_kg="10.000",
    )

    decision = logistics_readiness_decision(detail)

    assert decision.logistics_state == LOGISTICS_WAITING
    assert decision.should_continue is False
    assert decision.reason == "阿里物流状态未就绪：查询失败"


def test_logistics_ready_status_requires_tail_fields():
    detail = LogisticsDetail(
        logistics_no="ALS01781406025",
        status_text="已出库",
        carrier="FedEx",
        international_tracking_no="1234567890",
        actual_total="12.34",
        chargeable_weight_kg="2.5",
    )

    decision = logistics_readiness_decision(detail)

    assert decision.logistics_state == LOGISTICS_READY
    assert decision.should_continue is True


def test_real_overseas_carrier_allowlist_supports_expected_names():
    for carrier in ["UPS", "FedEx", "DHL", "USPS", "GOFO", "Yanwen", "SpeedX", "UniUni", "1ST", "SwiftX"]:
        assert is_real_overseas_carrier(carrier) is True

    assert is_real_overseas_carrier("FEDEX") is True
    assert is_real_overseas_carrier("speed-x") is True


@pytest.mark.parametrize(
    ("carrier", "tracking_no", "normalized_carrier"),
    [
        ("FedEx", "874084304695", "FEDEX"),
        ("UPS", "1Z9253126709651051", "UPS"),
        ("DHL", "1234567890", "DHL"),
        ("USPS", "9400100000000000000000", "USPS"),
        ("GOFO Express", "GFUS01029396906368", "GOFO"),
        ("Yanwen", "UG854485508YP", "YANWEN"),
        ("Speed-X", "SPX121055010785353", "SPEEDX"),
        ("UNI", "JY26CAA0T052507364", "UNIUNI"),
        ("1ST Group", "1ST08237532113", "1ST"),
        ("SwiftX", "SWX870030000004143598", "SWIFTX"),
    ],
)
def test_tracking_number_matches_supported_carrier_formats(carrier, tracking_no, normalized_carrier):
    assert normalize_carrier_name(carrier) == normalized_carrier
    assert tracking_number_matches_carrier(carrier, tracking_no) is True


@pytest.mark.parametrize(
    ("carrier", "tracking_no"),
    [
        ("FedEx", "JYCP00000093286"),
        ("UPS", "SPX121055010785353"),
        ("DHL", "1Z9253126709651051"),
        ("USPS", "GFUS01029396906368"),
        ("GOFO", "UG854485508YP"),
        ("Yanwen", "1234567890"),
        ("SpeedX", "JY26CAA0T052507364"),
        ("UniUni", "SWX870030000004143598"),
        ("1ST", "1STABC"),
        ("SwiftX", "SWX123"),
    ],
)
def test_tracking_number_rejects_other_carrier_or_invalid_formats(carrier, tracking_no):
    assert tracking_number_matches_carrier(carrier, tracking_no) is False


def test_tracking_mismatch_is_blocked_until_exact_pair_is_manually_confirmed():
    detail = LogisticsDetail(
        logistics_no="ALS01798551368",
        status_text="运输中",
        carrier="FedEx",
        international_tracking_no="1Z9253126709651051",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )

    blocked = logistics_readiness_decision(detail)
    confirmed = logistics_readiness_decision(detail, tracking_manually_verified=True)

    assert blocked.logistics_state == LOGISTICS_BLOCKED
    assert blocked.should_continue is False
    assert "国际物流单号与承运商不匹配" in blocked.reason
    assert confirmed.logistics_state == LOGISTICS_READY
    assert confirmed.should_continue is True


def test_jycp_intermediary_number_waits_for_real_tail_tracking():
    detail = LogisticsDetail(
        logistics_no="ALS01798551368",
        status_text="运输中",
        carrier="FedEx",
        international_tracking_no="JYCP00000093286",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )

    decision = logistics_readiness_decision(detail)

    assert decision.logistics_state == LOGISTICS_WAITING
    assert decision.should_continue is False
    assert decision.reason == (
        "阿里页面的国际物流单号仍为 JYCP00000093286，等待真实尾程单号。"
    )


def test_non_real_overseas_carrier_does_not_ready_to_mark():
    for carrier in ["YHA", "JY Express"]:
        detail = LogisticsDetail(
            logistics_no="ALS01781406025",
            status_text="运输中",
            carrier=carrier,
            international_tracking_no="TRACK123",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        )

        decision = logistics_readiness_decision(detail)

        assert decision.logistics_state == LOGISTICS_WAITING
        assert decision.should_continue is False
        assert "不是真实海外尾程承运商" in decision.reason
        assert carrier in decision.reason


def test_logistics_ready_status_missing_fields_goes_manual_review():
    detail = LogisticsDetail(
        logistics_no="ALS01781406025",
        status_text="已出库",
    )

    decision = logistics_readiness_decision(detail)

    assert decision.logistics_state == LOGISTICS_WAITING
    assert decision.should_continue is False
    assert "缺少国际物流服务商或国际物流单号" in decision.reason


def test_logistics_page_load_timeout_is_retryable():
    detail = LogisticsDetail(
        logistics_no="ALS01781406025",
        page_error="等待阿里国际站物流详情页加载或登录完成超时。",
    )

    decision = logistics_readiness_decision(detail)

    assert decision.logistics_state == LOGISTICS_RETRYABLE
    assert decision.should_continue is False


def test_apply_logistics_detail_updates_candidate_status_and_fields():
    detail = LogisticsDetail(
        logistics_no="ALS01781406025",
        status_text="已出库",
        carrier="FedEx",
        international_tracking_no="1234567890",
        actual_total="12.34",
        chargeable_weight_kg="2.5",
        package_count=1,
    )

    updated, decision = apply_logistics_detail_to_candidate(_candidate(), detail)

    assert decision.should_continue is True
    assert decision.logistics_state == LOGISTICS_READY
    assert updated.carrier == "FedEx"
    assert updated.international_tracking_no == "1234567890"


def test_parse_door_to_door_uses_estimated_fee_and_weight():
    text = """
物流订单详情
ALS01774710794
订单状态
派送完成
物流订单号
服务类型
服务线路
国际物流服务商
ALS01774710794
快递门到门
FedEx-IP
FedEx
国际物流单号
取件码
525885537168
BSDA3007
预估包裹
计费重(KG)19.000
实际包裹
计费重(KG)0.000
预估费用信息
预估总额
CNY 1349.25
实际费用信息
实际总额
CNY 0
"""

    detail = parse_logistics_detail_from_text(text)

    assert detail.service_type == "快递门到门"
    assert detail.service_line == "FedEx-IP"
    assert detail.actual_total == "CNY 1349.25"
    assert detail.chargeable_weight_kg == "19.000"
    assert detail.carrier is None
    assert detail.international_tracking_no is None
    assert detail.raw["critical_tail_fields_ignored"] is True


def test_parse_warehouse_to_door_uses_actual_fee_and_weight():
    text = """
物流订单详情
ALS01791288660
订单状态
运输中
物流订单号
服务类型
服务线路
仓库名称
ALS01791288660
快递仓到门
无忧全球普货专线
华南中心仓
国内物流服务商
国内物流单号
国际物流服务商
国际物流单号
阿里官方揽收(递四方)
310866357395
UPS
1ZB60Y660414003112
预估包裹
计费重(KG)4.000
实际包裹
计费重(KG)4.500
费用明细
预估费用信息
预估总额
CNY 227.47
实际费用信息
实际总额
CNY 247.47
"""

    detail = parse_logistics_detail_from_text(text)

    assert detail.service_type == "快递仓到门"
    assert detail.service_line == "无忧全球普货专线"
    assert detail.actual_total == "CNY 247.47"
    assert detail.chargeable_weight_kg == "4.500"
    assert detail.carrier is None
    assert detail.international_tracking_no is None


def test_parse_international_fields_after_estimated_arrival_date():
    text = """
物流订单详情
ALS01793232076
订单状态
已出库
物流订单号
服务类型
服务线路
仓库名称
ALS01793232076
快递仓到门
无忧全球普货专线
华南中心仓
预计到仓时间
国际物流服务商
国际物流单号
2026.07.06
UPS
1ZB60Y660405150426
实际包裹
计费重(KG)4.000
实际费用信息
实际总额
CNY 231.05
"""

    detail = parse_logistics_detail_from_text(text)

    assert detail.carrier is None
    assert detail.international_tracking_no is None


def test_structured_express_layout_ignores_adjacent_abnormal_contact_header():
    detail = parse_logistics_detail_from_field_groups(
        [
            LogisticsFieldGroup(
                source="table",
                group_id="table:0",
                fields={
                    "物流订单号": "ALS01811025989",
                    "服务类型": "快递门到门",
                    "服务线路": "FedEx-IP",
                    "国际物流服务商": "FedEx",
                    "国际物流单号": "525885561600",
                    "订单异常联系人": "13900000000",
                    "取件码": "BSDA3120",
                },
            )
        ],
        "ALS01811025989",
    )

    assert detail.carrier == "FedEx"
    assert detail.international_tracking_no == "525885561600"
    assert detail.raw["tracking_candidate_class"] == "candidate"
    assert "订单异常联系人" in detail.raw["selected_labels"]


def test_structured_multimodal_als_value_is_waiting_placeholder():
    structured = parse_logistics_detail_from_field_groups(
        [
            LogisticsFieldGroup(
                source="table",
                group_id="table:0",
                fields={
                    "物流订单号": "ALS01782864331",
                    "服务类型": "多式联运",
                    "预计到仓时间": "2026.07.02",
                    "国际物流服务商": "FEDEX",
                    "国际物流单号": "ALS01782864331",
                },
            )
        ],
        "ALS01782864331",
    )
    text = LogisticsDetail(
        logistics_no="ALS01782864331",
        status_text="已开船",
        actual_total="CNY 631.2",
        chargeable_weight_kg="30.000",
        raw={"source": "text"},
    )
    merged = merge_logistics_detail_sources(
        "ALS01782864331",
        text_detail=text,
        structured_detail=structured,
    )
    decision = logistics_readiness_decision(merged)

    assert merged.carrier == "FEDEX"
    assert merged.international_tracking_no is None
    assert merged.raw["tracking_candidate_class"] == "placeholder"
    assert decision.logistics_state == LOGISTICS_WAITING
    assert "等待真实尾程单号" in decision.reason


def test_merge_blocks_conflicting_service_line_sources():
    merged = merge_logistics_detail_sources(
        "ALS01811025989",
        text_detail=LogisticsDetail(
            logistics_no="ALS01811025989",
            service_line="普通专线",
            raw={"source": "text"},
        ),
        structured_detail=LogisticsDetail(
            logistics_no="ALS01811025989",
            service_line="UPS-Saver",
            raw={"source": "dom_structured"},
        ),
    )

    assert merged.page_error == "阿里物流服务线路来源冲突，无法安全选择 ERP 物流渠道。"


def test_structured_parser_uses_identity_match_and_arbitrary_column_order():
    detail = parse_logistics_detail_from_field_groups(
        [
            {
                "source": "label_value_card",
                "group_id": "card:wrong",
                "fields": {
                    "国际物流单号": "1Z0000000000000000",
                    "物流订单号": "ALS00000000001",
                    "国际物流服务商": "UPS",
                },
            },
            {
                "source": "definition_list",
                "group_id": "dl:target",
                "fields": {
                    "国际物流单号": "525885561600",
                    "订单异常联系人": "页面新增字段",
                    "物流订单号": "ALS01811025989",
                    "国际物流服务商": "FedEx",
                },
            },
        ],
        "ALS01811025989",
    )

    assert detail.carrier == "FedEx"
    assert detail.international_tracking_no == "525885561600"
    assert detail.raw["selected_group_id"] == "dl:target"


def test_structured_parser_refuses_conflicting_components_and_split_fields():
    groups = [
        LogisticsFieldGroup(
            source="table",
            group_id="table:0",
            fields={"国际物流服务商": "FedEx"},
        ),
        LogisticsFieldGroup(
            source="table",
            group_id="table:1",
            fields={"国际物流单号": "525885561600"},
        ),
    ]

    detail = parse_logistics_detail_from_field_groups(groups, "ALS01811025989")
    decision = logistics_readiness_decision(
        LogisticsDetail(
            logistics_no=detail.logistics_no,
            status_text="运输中",
            page_error=detail.page_error,
            raw=detail.raw,
        )
    )

    assert detail.page_error
    assert decision.logistics_state == LOGISTICS_RETRYABLE


@pytest.mark.parametrize(
    ("candidate", "category"),
    [
        ("ALS01782864331", "placeholder"),
        ("JYCP00000093286", "intermediary"),
        ("订单异常联系人", "ui_text"),
        ("国际物流单号", "ui_text"),
        ("525885561600", "candidate"),
    ],
)
def test_tracking_candidate_classification(candidate, category):
    decision = classify_tracking_candidate("ALS01782864331", "FedEx", candidate)
    assert decision.category == category
    assert decision.usable is (category == "candidate")


def test_json_parser_requires_explicit_identity_bound_keys():
    poisoned = parse_logistics_detail_from_json_payloads(
        [
            {
                "labels": ["国际物流单号", "订单异常联系人"],
                "values": ["525885561600", "13900000000"],
                "logisticsNo": "ALS01811025989",
            }
        ],
        fallback_logistics_no="ALS01811025989",
    )
    explicit = parse_logistics_detail_from_json_payloads(
        [
            {
                "logisticsNo": "ALS01811025989",
                "internationalTrackingNo": "525885561600",
                "internationalCarrier": "FedEx",
                "logisticsStatus": "运输中",
            }
        ],
        fallback_logistics_no="ALS01811025989",
    )

    assert poisoned is None
    assert explicit is not None
    assert explicit.international_tracking_no == "525885561600"
    assert explicit.carrier == "FedEx"


def test_extract_logistics_field_groups_normalizes_page_payload():
    class FakePage:
        async def evaluate(self, _script):
            return [
                {
                    "source": "table",
                    "group_id": "table:0",
                    "fields": {
                        "物流订单号": " ALS01811025989 ",
                        "国际物流单号": " 525885561600 ",
                        "无关字段": "不得进入结果",
                    },
                }
            ]

    groups = asyncio.run(extract_logistics_field_groups(FakePage()))

    assert groups == [
        LogisticsFieldGroup(
            source="table",
            group_id="table:0",
            fields={
                "物流订单号": "ALS01811025989",
                "国际物流单号": "525885561600",
            },
        )
    ]


def test_parse_no_permission_goes_manual_review():
    detail = parse_logistics_detail_from_text("暂无权限查看该物流订单", fallback_logistics_no="ALS01781406025")

    decision = logistics_readiness_decision(detail)

    assert detail.page_error
    assert decision.logistics_state == LOGISTICS_BLOCKED
