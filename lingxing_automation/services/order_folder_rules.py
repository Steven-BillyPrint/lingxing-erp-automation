from __future__ import annotations

import re
from dataclasses import dataclass, field


TITLE_FRAME = "Frame Options"
TITLE_FRAME_RECOMMENDED = "Frame Options - Our Frame Recommended for Best Fit"
TITLE_FRAME_COMPATIBILITY_ALERT = "Frame Options - Compatibility Alert for Frame"
TITLE_FRAME_COMPATIBILITY_ALERT_SHORT = "Compatibility Alert for Frame"
TITLE_SIDE_WALL = "Side Wall and Rail Options"
TITLE_FABRIC = "Fabric Material Options"
TITLE_FABRIC_SINGULAR = "Fabric Material Option"
TITLE_DOUBLE_SIDE = "Double-sided Printing Options"
TITLE_DOUBLE_SIDE_SINGULAR = "Double-sided Printing Option"
TITLE_DOUBLE_SIDE_ONLY_SIDE_WALL = "Double-sided Printing Options(Only Side Wall Options Chosen)"
TITLE_DOUBLE_SIDE_ONLY_SIDE_WALL_SPACED = "Double-sided Printing Options (Only Side Wall Options Chosen)"
TITLE_ROLLER_BAG = "Roller Bag Options"
TITLE_ROPE_STAKE = "Rope & Stake Kit Options"
TITLE_SANDBAGS = "Sandbags (4 piece set)"
TITLE_SANDBAGS_6PCS = "Sandbags (6 piece set)"
TITLE_FITTED_TABLE_CLOTH = "Custom Fitted Table Cloth with Your Logo"
TITLE_TABLE_CLOTH = "Custom Table Cloth with Your Logo"
TITLE_FLAG = "Custom Feather/Teardrop Flag"
TITLE_RAIL_ADAPTER = "Add Half Wall Rail & Frame Adapter?"
TITLE_FULL_WALL_ATTACHMENT = "Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper."
TITLE_FULL_WALL_ATTACHMENT_NO_PERIOD = "Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper"
TITLE_FULL_WALL_SIZE = "Select the Full Wall Size That Fits Your Canopy Tent Frame"
TITLE_CANOPY_FRAME_SIZE = "Select a Standard Size or Provide Custom Canopy Frame Dimensions for a Perfect Fit"
TITLE_CAR_MAGNET_SIZE = "Car Magnet Size"
TITLE_CAR_MAGNET_SHAPE = "Shapes / Die Cut"
TITLE_CAR_MAGNET_SURFACE = "Surface Material Option"
TITLE_CAR_MAGNET_CORNER = "Corner"
TITLE_CAR_MAGNET_THICKNESS = "Choose Your Magnet Thickness"
# 这个 Proof 标题目前被磁贴和易拉宝共用，放在通用标题列表里供页面 tooltip 文本解析。
TITLE_CAR_MAGNET_PROOF = "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping"
TITLE_PRINTING_PROCESS = "Printing Process"
TITLE_CAR_MAGNET_SAME_DESIGN = "Is The Right Side Using The Same Design As The Left Side?"
TITLE_TENT_SAME_DESIGN = "Do you want the Topper Left/Right and Front/Back to have the same design and text?"

ORDER_FOLDER_TITLES = (
    TITLE_FRAME_RECOMMENDED,
    TITLE_FRAME_COMPATIBILITY_ALERT,
    TITLE_FRAME_COMPATIBILITY_ALERT_SHORT,
    TITLE_FRAME,
    TITLE_SIDE_WALL,
    TITLE_FABRIC,
    TITLE_FABRIC_SINGULAR,
    TITLE_DOUBLE_SIDE,
    TITLE_DOUBLE_SIDE_SINGULAR,
    TITLE_DOUBLE_SIDE_ONLY_SIDE_WALL,
    TITLE_DOUBLE_SIDE_ONLY_SIDE_WALL_SPACED,
    TITLE_ROLLER_BAG,
    TITLE_ROPE_STAKE,
    TITLE_SANDBAGS,
    TITLE_SANDBAGS_6PCS,
    TITLE_FITTED_TABLE_CLOTH,
    TITLE_TABLE_CLOTH,
    TITLE_FLAG,
    TITLE_RAIL_ADAPTER,
    TITLE_FULL_WALL_ATTACHMENT,
    TITLE_FULL_WALL_ATTACHMENT_NO_PERIOD,
    TITLE_FULL_WALL_SIZE,
    TITLE_CANOPY_FRAME_SIZE,
    TITLE_CAR_MAGNET_SIZE,
    TITLE_CAR_MAGNET_SHAPE,
    TITLE_CAR_MAGNET_SURFACE,
    TITLE_CAR_MAGNET_CORNER,
    TITLE_CAR_MAGNET_THICKNESS,
    TITLE_CAR_MAGNET_PROOF,
    TITLE_PRINTING_PROCESS,
    TITLE_CAR_MAGNET_SAME_DESIGN,
    TITLE_TENT_SAME_DESIGN,
)
ORDER_FOLDER_TITLE_ALIASES = {
    # Amazon 定制化 JSON 里同一个支架选项会因兼容性提示换标题；
    # 业务上仍然按普通 Frame Options 处理，否则支架片段会漏进文件夹名。
    TITLE_FRAME_COMPATIBILITY_ALERT: TITLE_FRAME,
    TITLE_FRAME_COMPATIBILITY_ALERT_SHORT: TITLE_FRAME,
    TITLE_FABRIC_SINGULAR: TITLE_FABRIC,
    TITLE_DOUBLE_SIDE_SINGULAR: TITLE_DOUBLE_SIDE,
    TITLE_DOUBLE_SIDE_ONLY_SIDE_WALL: TITLE_DOUBLE_SIDE,
    TITLE_DOUBLE_SIDE_ONLY_SIDE_WALL_SPACED: TITLE_DOUBLE_SIDE,
    TITLE_FULL_WALL_ATTACHMENT_NO_PERIOD: TITLE_FULL_WALL_ATTACHMENT,
}

FRAME_TITLES = (TITLE_FRAME_RECOMMENDED, TITLE_FRAME_COMPATIBILITY_ALERT, TITLE_FRAME_COMPATIBILITY_ALERT_SHORT, TITLE_FRAME)
TABLE_CLOTH_TITLES = (TITLE_FITTED_TABLE_CLOTH, TITLE_TABLE_CLOTH)

# No / None / No Wall 等空选项表示客户明确没有选择该配件，
# 业务上只是跳过文件夹片段，不应当当成规则缺失。
EMPTY_OPTION_VALUES = {
    "",
    "no",
    "none",
    "no wall",
    "no walls",
    "no roller bag",
    "no rope & stake kit",
    "no rope and stake kit",
    "no sandbags",
    "no sandbags (4 piece set)",
    "no sandbags (6 piece set)",
    "no table cloth",
    "no flag",
    "not selected",
    "n/a",
}


@dataclass(frozen=True)
class WallRuleComponent:
    kind: str
    text: str


@dataclass(frozen=True)
class OrderFolderRules:
    """订单文件夹规则表。

    规则集中维护，避免英文选项到中文片段的映射散落到流程代码里。
    未知选项必须暴露为folder_rule_missing，避免猜测后误建文件夹。
    """

    frame_options: dict[str, str] = field(default_factory=dict)
    fabric_options: dict[str, str] = field(default_factory=dict)
    wall_options: dict[str, tuple[WallRuleComponent, ...]] = field(default_factory=dict)
    table_cloth_options: dict[str, str] = field(default_factory=dict)
    accessory_options: dict[tuple[str, str], str] = field(default_factory=dict)
    rail_adapter_options: dict[str, str] = field(default_factory=dict)
    full_wall_attachment_options: dict[str, str] = field(default_factory=dict)
    full_wall_size_options: dict[str, str] = field(default_factory=dict)
    canopy_frame_size_options: dict[str, str] = field(default_factory=dict)
    car_magnet_surface_options: dict[str, str] = field(default_factory=dict)
    car_magnet_corner_options: dict[str, str] = field(default_factory=dict)
    car_magnet_thickness_options: dict[str, str] = field(default_factory=dict)
    car_magnet_shape_options: dict[str, tuple[str, str]] = field(default_factory=dict)


def normalize_rule_key(value: str | None) -> str:
    """规范化规则键，便于后续匹配和比较。"""
    text = str(value or "").strip().lower()
    text = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("′", "'")
        .replace("″", '"')
        .replace("‘", "'")
        .replace("’", "'")
        .replace("×", "x")
        .replace("–", "-")
        .replace("—", "-")
    )
    text = re.sub(r'(?<=\d)"(?=[a-z])', '" ', text)
    text = re.sub(r"\s*-\s*", " - ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_empty_option(value: str | None) -> bool:
    """判断空值选项是否满足业务条件。"""
    return normalize_rule_key(value) in EMPTY_OPTION_VALUES


def _wall(*items: tuple[str, str]) -> tuple[WallRuleComponent, ...]:
    """处理侧墙相关逻辑，并返回后续流程所需结果。"""
    return tuple(WallRuleComponent(kind=kind, text=text) for kind, text in items)


def load_default_order_folder_rules() -> OrderFolderRules:
    """加载默认规则。

    拖轮包、绳子地钉、沙袋属于实物配件，
    客户选择后需要进入文件夹名，方便生产侧按标题备货。
    """
    frame_options = {
        normalize_rule_key("NO FRAME – I accept compatibility risk"): "",
        normalize_rule_key("NO FRAME - I accept compatibility risk"): "",
        normalize_rule_key('Standard 1.6"/40mm square aluminum'): "40mm方形铝",
        normalize_rule_key('Add Standard 1.6"/40mm square alum frame'): "40mm方形铝",
        normalize_rule_key('Standard 1.5"/38mm square aluminum'): "40mm方形铝",
        normalize_rule_key('BEST DEAL Commercial 1.6"/40mm hexagonal aluminum'): "40mm六角铝",
        normalize_rule_key('Commercial 1.6"/40mm hexagonal aluminum'): "40mm六角铝",
        normalize_rule_key('Add Commercial 1.6"/40mm hex alum frame'): "40mm六角铝",
        normalize_rule_key('Premium 2"/50mm hexagonal aluminum'): "50mm六角铝",
        normalize_rule_key('Add Premium 2"/50mm hex alum frame'): "50mm六角铝",
    }
    fabric_options = {
        normalize_rule_key("400D Polyester Fabric"): "400D面料",
        normalize_rule_key("600D Flame Retardant Polyester Fabric"): "600D阻燃面料",
    }
    wall_options = {
        normalize_rule_key("No Wall"): (),
        normalize_rule_key("1 Full Wall"): _wall(("full_wall", "1全高背墙")),
        normalize_rule_key("1 Full and 2 Half Walls without Rails"): _wall(
            ("full_wall", "1全高背墙"),
            ("half_wall", "2半高侧墙"),
        ),
        normalize_rule_key("1 Full and 2 Half Walls with Rails"): _wall(
            ("full_wall", "1全高背墙"),
            ("half_wall", "2半高侧墙(带横杆)"),
        ),
        normalize_rule_key("1 Full and 3 Half Walls without Rails"): _wall(
            ("full_wall", "1全高背墙"),
            ("half_wall", "3半高侧墙"),
        ),
        normalize_rule_key("1 Full and 3 Half Walls with Rails"): _wall(
            ("full_wall", "1全高背墙"),
            ("half_wall", "3半高侧墙(带横杆)"),
        ),
        normalize_rule_key("3 Full Walls"): _wall(("full_wall", "3全高背墙")),
        normalize_rule_key("3 Full and 1 Half Wall without Rail"): _wall(
            ("full_wall", "3全高背墙"),
            ("half_wall", "1半高侧墙"),
        ),
        normalize_rule_key("3 Full and 1 Half Wall with Rail"): _wall(
            ("full_wall", "3全高背墙"),
            ("half_wall", "1半高侧墙(带横杆)"),
        ),
        normalize_rule_key("1 Mesh Window+3 Full Walls with Roll-up"): _wall(
            ("full_wall", "1网格带窗全墙"),
            ("full_wall", "3全墙其中背墙带卷帘门"),
        ),
        normalize_rule_key("1 Mesh Window+2 Mesh Walls+1 Full Rollup"): _wall(
            ("full_wall", "1网格带窗全墙"),
            ("full_wall", "2网格全墙"),
            ("full_wall", "1全墙带卷帘门"),
        ),
    }
    table_cloth_options: dict[str, str] = {}
    for size in ("4Ft", "5Ft", "6Ft", "8Ft"):
        upper_size = size.upper()
        # ERP 有时只返回 “6Ft with Back” 这种简写；它和带 260GSM 材质后缀的选项是同一业务规则。
        # 这里集中维护 alias，避免文件夹生成时把页面简写误判为未知桌布规则。
        with_back_text = f"1个{upper_size}方套桌布+260g经编布"
        no_back_text = f"1个{upper_size}方套桌布（背后开口）+260g经编布"
        table_cloth_options[normalize_rule_key(f"{size} with Back (260GSM Polyester Fabric)")] = with_back_text
        table_cloth_options[normalize_rule_key(f"{size} with Back")] = with_back_text
        table_cloth_options[normalize_rule_key(f"{size} No Back (260GSM Polyester Fabric)")] = no_back_text
        table_cloth_options[normalize_rule_key(f"{size} No Back")] = no_back_text
    accessory_options = {
        (TITLE_ROLLER_BAG, normalize_rule_key("Add Roller Bag")): "拖轮包",
        (TITLE_ROLLER_BAG, normalize_rule_key("No Roller Bag")): "",
        (TITLE_ROPE_STAKE, normalize_rule_key("Yes")): "绳子地钉",
        (TITLE_ROPE_STAKE, normalize_rule_key("Add Rope & Stake Kit")): "绳子地钉",
        (TITLE_ROPE_STAKE, normalize_rule_key("Bonus Rope & Stake Kit")): "绳子地钉",
        # 加拿大站部分订单只返回 “Bonus” 简写；业务含义仍是赠送绳子地钉。
        (TITLE_ROPE_STAKE, normalize_rule_key("Bonus")): "绳子地钉",
        (TITLE_ROPE_STAKE, normalize_rule_key("None")): "",
        (TITLE_SANDBAGS, normalize_rule_key("Add Sandbags (4 piece set)")): "沙袋四件套",
        (TITLE_SANDBAGS, normalize_rule_key("Yes")): "沙袋四件套",
        (TITLE_SANDBAGS, normalize_rule_key("No Sandbags (4 piece set)")): "",
        (TITLE_SANDBAGS, normalize_rule_key("No")): "",
        (TITLE_SANDBAGS_6PCS, normalize_rule_key("Add Sandbags (6 piece set)")): "沙袋六件套",
        (TITLE_SANDBAGS_6PCS, normalize_rule_key("Yes")): "沙袋六件套",
        (TITLE_SANDBAGS_6PCS, normalize_rule_key("No")): "",
        (TITLE_SANDBAGS_6PCS, normalize_rule_key("No Sandbags (6 piece set)")): "",
    }
    rail_adapter_options = {
        normalize_rule_key("No Rail"): "",
        # Amazon 新选项明确表示不要横杆，也不要横杆袋。
        # 它与 No Rail 的生产含义相同，不应在文件夹名中追加任何组件。
        normalize_rule_key("No Rail and No Rail Pocket"): "",
        normalize_rule_key('Add Rail for 1.2"/30mm Square Leg Frame'): "加横杆适配30mm方形铝夹具",
        normalize_rule_key('Add Rail for 1.5"/38mm Square Leg Frame'): "加横杆适配38mm方形铝夹具",
        normalize_rule_key('Add Rail for 1.6"/40mm Square Leg Frame'): "加横杆适配40mm方形铝夹具",
        normalize_rule_key('Add Rail for 1.6"/40mm Hex Leg Frame'): "加横杆适配40mm六角铝夹具",
        normalize_rule_key('Add Rail for 2"/50mm Hex Leg Frame'): "加横杆适配50mm六角铝夹具",
    }
    full_wall_attachment_options = {
        # 单卖全围的顶部连接方式是三选一字段，不能把魔术贴选项再拼上“系带”；
        # 否则生产侧会误以为同一片全围同时需要两种连接方式。
        normalize_rule_key("Full Wall uses ties for attachment"): "系带",
        normalize_rule_key("Full Wall uses the Velcro loop side"): "魔术贴毛面",
        normalize_rule_key("Full Wall uses the Velcro hook side"): "魔术贴钩面",
    }
    full_wall_size_options = {
        normalize_rule_key('118×85.4" Wall – for Straight Leg 10x10‘'): "适配直腿足尺寸架子",
        normalize_rule_key('118x85.4" Wall - for Straight Leg 10x10\''): "适配直腿足尺寸架子",
        normalize_rule_key('114×85.4"Wall– for Straight Leg 9.5x9.5’'): "适配直腿不足尺寸架子",
        normalize_rule_key('114x85.4" Wall - for Straight Leg 9.5x9.5\''): "适配直腿不足尺寸架子",
        normalize_rule_key('96×118×85.4" Wall – for Slant Leg 10x10‘'): "适配斜腿架子",
        normalize_rule_key('96x118x85.4" Wall - for Slant Leg 10x10\''): "适配斜腿架子",
    }
    canopy_frame_size_options = {
        normalize_rule_key('A=91.6" B=12.6" C=175.2" D=118"Fits 10\' Commercial'): "适用足尺寸架子",
        normalize_rule_key('A=91.6" B=12.6" C=234.25" D=118"Fits10\' Commercial'): "适用足尺寸架子",
        normalize_rule_key('A=91.6" B=12.6" C=234.25" D=118" Fits 10\' Commercial'): "适用足尺寸架子",
        normalize_rule_key('A=91.6", B=12.6", C=118" Fits 10\' Commercial'): "适用足尺寸架子",
        normalize_rule_key('A=91" B=12.6" C=169.3" D=114" Fits 9.5\' Standard'): "适用不足尺寸的架子",
        normalize_rule_key('A=91" B=12.6" C=224.4" D=114" Fits 9.5\' Standard'): "适用不足尺寸的架子",
        normalize_rule_key('A=91", B=12.6", C=114" Fits 9.5\' Standard'): "适用不足尺寸的架子",
        normalize_rule_key('I will provide A, B, C, Din "Customize Canopy Top"'): "自定义尺寸",
        normalize_rule_key('I will provide A, B, C in "Customize Canopy Top"'): "自定义尺寸",
    }
    car_magnet_surface_options = {
        normalize_rule_key("Reflective Vinyl"): "反光膜",
        normalize_rule_key("Standard Vinyl"): "",
    }
    car_magnet_corner_options = {
        normalize_rule_key("Square"): "",
        normalize_rule_key("Sharp"): "",
        normalize_rule_key("Rounded"): "圆角",
        normalize_rule_key("Round"): "圆角",
    }
    car_magnet_thickness_options = {
        normalize_rule_key("Standard Strength 20mil/0.5mm Magnetic"): "0.5mm",
        normalize_rule_key("Heavy Strength 40mil/1mm Magnetic"): "1mm",
    }
    car_magnet_shape_options = {
        # tuple 第一个值是品名形状片段，第二个值是额外组件；圆角作为独立组件放在品名后。
        normalize_rule_key("Square Rectangle Sharp Corners"): ("方形汽车磁贴", ""),
        normalize_rule_key("Square Rectangle Round Corners"): ("方形汽车磁贴", "圆角"),
        normalize_rule_key("Rectangle (Length:Width=2:1)"): ("方形汽车磁贴", ""),
        normalize_rule_key("Rectangle (Length:Width=4:3)"): ("方形汽车磁贴", ""),
        normalize_rule_key("Round"): ("圆形汽车磁贴", ""),
        normalize_rule_key("Oval"): ("椭圆汽车磁贴", ""),
        normalize_rule_key("Heart Shape"): ("心形汽车磁贴", ""),
        normalize_rule_key("Die Cut ( Fit to Your Design )"): ("异形汽车磁贴", ""),
        normalize_rule_key("Die Cut (Fit to Your Design)"): ("异形汽车磁贴", ""),
    }
    return OrderFolderRules(
        frame_options=frame_options,
        fabric_options=fabric_options,
        wall_options=wall_options,
        table_cloth_options=table_cloth_options,
        accessory_options=accessory_options,
        rail_adapter_options=rail_adapter_options,
        full_wall_attachment_options=full_wall_attachment_options,
        full_wall_size_options=full_wall_size_options,
        canopy_frame_size_options=canopy_frame_size_options,
        car_magnet_surface_options=car_magnet_surface_options,
        car_magnet_corner_options=car_magnet_corner_options,
        car_magnet_thickness_options=car_magnet_thickness_options,
        car_magnet_shape_options=car_magnet_shape_options,
    )


DEFAULT_ORDER_FOLDER_RULES = load_default_order_folder_rules()
