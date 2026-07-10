from shipment_automation.alibaba_logistics import (
    apply_logistics_detail_to_candidate,
    is_real_overseas_carrier,
    parse_logistics_detail_from_text,
    is_not_ready_logistics_status,
    logistics_readiness_decision,
)
from shipment_automation.models import (
    LogisticsDetail,
    QUEUE_STATUS_MANUAL_REVIEW,
    QUEUE_STATUS_NOT_READY,
    QUEUE_STATUS_READY_TO_MARK,
    ShipmentCandidate,
)


def _candidate():
    return ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        als_no="ALS01781406025",
        shipment_tag_name="帐篷标发",
    )


def test_logistics_not_ready_statuses_include_arrived_warehouse():
    for status in [
        "待揽收",
        "已揽收",
        "已入库",
        "未出库",
        "货物抵达仓库",
        "订单关闭",
        "订单取消",
        "订单中止",
        "已核查",
        "已取消",
    ]:
        assert is_not_ready_logistics_status(status) is True
        decision = logistics_readiness_decision(LogisticsDetail(als_no="ALS01781406025", status_text=status))
        assert decision.queue_status == QUEUE_STATUS_NOT_READY
        assert decision.should_continue is False


def test_logistics_ready_status_requires_tail_fields():
    detail = LogisticsDetail(
        als_no="ALS01781406025",
        status_text="已出库",
        logistics_order_no="1781406025",
        carrier="FedEx",
        international_tracking_no="1234567890",
        actual_total="12.34",
        chargeable_weight_kg="2.5",
    )

    decision = logistics_readiness_decision(detail)

    assert decision.queue_status == QUEUE_STATUS_READY_TO_MARK
    assert decision.should_continue is True


def test_real_overseas_carrier_allowlist_supports_expected_names():
    for carrier in ["UPS", "FedEx", "DHL", "USPS", "GOFO", "Yanwen", "SpeedX", "UniUni", "1ST", "SwiftX"]:
        assert is_real_overseas_carrier(carrier) is True

    assert is_real_overseas_carrier("FEDEX") is True
    assert is_real_overseas_carrier("speed-x") is True


def test_non_real_overseas_carrier_does_not_ready_to_mark():
    for carrier in ["YHA", "JY Express"]:
        detail = LogisticsDetail(
            als_no="ALS01781406025",
            status_text="运输中",
            logistics_order_no="ALS01781406025",
            carrier=carrier,
            international_tracking_no="TRACK123",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        )

        decision = logistics_readiness_decision(detail)

        assert decision.queue_status == QUEUE_STATUS_NOT_READY
        assert decision.should_continue is False
        assert "不是真实海外尾程承运商" in decision.reason
        assert carrier in decision.reason


def test_logistics_ready_status_missing_fields_goes_manual_review():
    detail = LogisticsDetail(
        als_no="ALS01781406025",
        status_text="已出库",
        logistics_order_no="1781406025",
    )

    decision = logistics_readiness_decision(detail)

    assert decision.queue_status == QUEUE_STATUS_NOT_READY
    assert decision.should_continue is False
    assert "缺少国际物流服务商或国际物流单号" in decision.reason


def test_apply_logistics_detail_updates_candidate_status_and_fields():
    detail = LogisticsDetail(
        als_no="ALS01781406025",
        status_text="已出库",
        logistics_order_no="1781406025",
        carrier="FedEx",
        international_tracking_no="1234567890",
        actual_total="12.34",
        chargeable_weight_kg="2.5",
        package_count=1,
    )

    updated, decision = apply_logistics_detail_to_candidate(_candidate(), detail)

    assert decision.should_continue is True
    assert updated.queue_status == QUEUE_STATUS_READY_TO_MARK
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
    assert detail.actual_total == "CNY 1349.25"
    assert detail.chargeable_weight_kg == "19.000"
    assert detail.carrier == "FedEx"
    assert detail.international_tracking_no == "525885537168"


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
    assert detail.actual_total == "CNY 247.47"
    assert detail.chargeable_weight_kg == "4.500"
    assert detail.carrier == "UPS"
    assert detail.international_tracking_no == "1ZB60Y660414003112"


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

    assert detail.carrier == "UPS"
    assert detail.international_tracking_no == "1ZB60Y660405150426"


def test_parse_no_permission_goes_manual_review():
    detail = parse_logistics_detail_from_text("暂无权限查看该物流订单", fallback_als_no="ALS01781406025")

    decision = logistics_readiness_decision(detail)

    assert detail.page_error
    assert decision.queue_status == QUEUE_STATUS_MANUAL_REVIEW
