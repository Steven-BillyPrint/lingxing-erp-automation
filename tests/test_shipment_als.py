from shipment_automation.candidate_scanner import extract_als_from_remark


def test_extract_standard_als_number():
    result = extract_als_from_remark("重发邮件 ALS01789020252，请处理")

    assert result.selected_als_no == "ALS01789020252"
    assert result.valid_als_numbers == ["ALS01789020252"]
    assert result.warnings == []


def test_extract_truncates_long_sticky_als_number():
    result = extract_als_from_remark("物流单 ALS017964450901037 后面粘了系统单号")

    assert result.selected_als_no == "ALS01796445090"
    assert result.truncated_als_numbers == ["ALS01796445090"]
    assert "截断" in result.warnings[0]


def test_extract_excludes_invalid_context_but_keeps_replacement():
    result = extract_als_from_remark("作废 ALS01789020252，改 ALS01789020253")

    assert result.selected_als_no == "ALS01789020253"
    assert result.excluded_als_numbers == ["ALS01789020252"]


def test_extract_multiple_valid_als_uses_first_and_warns():
    result = extract_als_from_remark("ALS01789020252，另一个 ALS01789020253")

    assert result.selected_als_no == "ALS01789020252"
    assert result.valid_als_numbers == ["ALS01789020252", "ALS01789020253"]
    assert "多个有效物流单号" in "；".join(result.warnings)
    assert "ALS01789020253" in "；".join(result.warnings)


def test_extract_duplicate_als_warns_once():
    result = extract_als_from_remark("ALS01789020252，重复 ALS01789020252")

    assert result.selected_als_no == "ALS01789020252"
    assert result.valid_als_numbers == ["ALS01789020252"]
    assert result.duplicate_als_numbers == ["ALS01789020252"]
    assert "重复出现物流单号" in "；".join(result.warnings)
