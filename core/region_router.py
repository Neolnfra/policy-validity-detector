"""
core/region_router.py —— 全国行政区划 → 政务网域名路由
================================================================================

职责
----
1. 维护【34 个省级行政区】+【重点城市】+【国务院部委】的政务网域名映射；
2. 从任意政策文本（标题 / 文号 / 正文片段）中自动识别行政层级与属地；
3. 输出可直接用于检索的 `site:` 过滤域名。

为什么必须做“动态路由”
----------------------
政务检索的第一道门槛不是关键词，而是"去哪查"。同一主题的政策，在 `beijing.gov.cn`
和 `hangzhou.gov.cn` 上的表述、有效期、清理节奏完全不同。若不分域检索，元宝搜索
返回的将是全国混杂结果，反而放大误判概率。因此本模块是整条链路的第 0 步。

识别优先级（从高到低）
----------------------
   1. 用户显式指定（region_hint != auto）
   2. 发文字号中的机关代字（如 杭政办发 → 杭州市）
   3. 文本中出现的城市全称（杭州 / 杭州市）
   4. 文本中出现的省级全称（浙江省）
   5. 发文字号中的省级代字（浙政发 → 浙江省）
   6. 部委全称或通用简称（国家发展改革委 / 发改委）
   7. 兜底：全国通查（www.gov.cn），并标注低置信度
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    """一个可检索的行政区划 / 发文主体。"""

    key: str
    name: str
    tier: str  # national | province | city
    admin_level: str  # 展示用：省级 / 直辖市、地市级、国务院 / 部委级
    gov_domain: str  # 政务网主域名，用于 site: 过滤
    parent: str = ""  # 所属省级行政区 key
    aliases: Tuple[str, ...] = ()  # 全称类别名（不含单字简称）
    doc_prefix: str = ""  # 发文字号机关代字，如 杭、深、浙

    @property
    def site_filter(self) -> str:
        """可直接拼进检索词的 site: 表达式。"""
        return self.gov_domain or "gov.cn"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "tier": self.tier,
            "admin_level": self.admin_level,
            "gov_domain": self.gov_domain,
            "site_filter": self.site_filter,
            "parent": self.parent,
            "aliases": list(self.aliases),
        }


@dataclass
class RegionMatch:
    """一次地域识别的结果。"""

    region: Region
    confidence: float
    matched_by: str
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "region_key": self.region.key,
            "region_name": self.region.name,
            "admin_level": self.region.admin_level,
            "gov_domain": self.region.gov_domain,
            "site_filter": self.region.site_filter,
            "confidence": round(self.confidence, 2),
            "matched_by": self.matched_by,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# 1. 国务院 / 部委级
# ---------------------------------------------------------------------------
MINISTRIES: List[Region] = [
    Region("national", "全国（国务院）", "national", "国务院 / 部委级", "www.gov.cn",
           aliases=("国务院", "中国政府网", "国务院办公厅", "国发")),
    Region("ndrc", "国家发展和改革委员会", "national", "国务院 / 部委级", "ndrc.gov.cn",
           aliases=("国家发展和改革委员会", "国家发展改革委", "发改委", "国家发改委")),
    Region("miit", "工业和信息化部", "national", "国务院 / 部委级", "miit.gov.cn",
           aliases=("工业和信息化部", "工信部")),
    Region("mof", "财政部", "national", "国务院 / 部委级", "mof.gov.cn",
           aliases=("财政部",)),
    Region("mofcom", "商务部", "national", "国务院 / 部委级", "mofcom.gov.cn",
           aliases=("商务部",)),
    Region("mohrss", "人力资源和社会保障部", "national", "国务院 / 部委级", "mohrss.gov.cn",
           aliases=("人力资源和社会保障部", "人社部")),
    Region("most", "科学技术部", "national", "国务院 / 部委级", "most.gov.cn",
           aliases=("科学技术部", "科技部")),
    Region("moe", "教育部", "national", "国务院 / 部委级", "moe.gov.cn",
           aliases=("教育部",)),
    Region("nhc", "国家卫生健康委员会", "national", "国务院 / 部委级", "nhc.gov.cn",
           aliases=("国家卫生健康委员会", "国家卫健委", "卫健委")),
    Region("mohurd", "住房和城乡建设部", "national", "国务院 / 部委级", "mohurd.gov.cn",
           aliases=("住房和城乡建设部", "住建部")),
    Region("mot", "交通运输部", "national", "国务院 / 部委级", "mot.gov.cn",
           aliases=("交通运输部",)),
    Region("moa", "农业农村部", "national", "国务院 / 部委级", "moa.gov.cn",
           aliases=("农业农村部",)),
    Region("mee", "生态环境部", "national", "国务院 / 部委级", "mee.gov.cn",
           aliases=("生态环境部", "环保部")),
    Region("mnr", "自然资源部", "national", "国务院 / 部委级", "mnr.gov.cn",
           aliases=("自然资源部",)),
    Region("mwr", "水利部", "national", "国务院 / 部委级", "mwr.gov.cn",
           aliases=("水利部",)),
    Region("mct", "文化和旅游部", "national", "国务院 / 部委级", "mct.gov.cn",
           aliases=("文化和旅游部", "文旅部")),
    Region("mca", "民政部", "national", "国务院 / 部委级", "mca.gov.cn",
           aliases=("民政部",)),
    Region("moj", "司法部", "national", "国务院 / 部委级", "moj.gov.cn",
           aliases=("司法部",)),
    Region("mps", "公安部", "national", "国务院 / 部委级", "mps.gov.cn",
           aliases=("公安部",)),
    Region("mva", "退役军人事务部", "national", "国务院 / 部委级", "mva.gov.cn",
           aliases=("退役军人事务部",)),
    Region("mem", "应急管理部", "national", "国务院 / 部委级", "mem.gov.cn",
           aliases=("应急管理部",)),
    Region("pbc", "中国人民银行", "national", "国务院 / 部委级", "pbc.gov.cn",
           aliases=("中国人民银行", "央行")),
    Region("audit", "审计署", "national", "国务院 / 部委级", "audit.gov.cn",
           aliases=("审计署",)),
    Region("chinatax", "国家税务总局", "national", "国务院 / 部委级", "chinatax.gov.cn",
           aliases=("国家税务总局", "税务总局", "税务局")),
    Region("samr", "国家市场监督管理总局", "national", "国务院 / 部委级", "samr.gov.cn",
           aliases=("国家市场监督管理总局", "市场监管总局", "市场监督管理总局")),
    Region("nfra", "国家金融监督管理总局", "national", "国务院 / 部委级", "nfra.gov.cn",
           aliases=("国家金融监督管理总局", "金融监管总局", "银保监会")),
    Region("csrc", "中国证券监督管理委员会", "national", "国务院 / 部委级", "csrc.gov.cn",
           aliases=("中国证券监督管理委员会", "证监会")),
    Region("nhsa", "国家医疗保障局", "national", "国务院 / 部委级", "nhsa.gov.cn",
           aliases=("国家医疗保障局", "国家医保局", "医保局")),
    Region("nea", "国家能源局", "national", "国务院 / 部委级", "nea.gov.cn",
           aliases=("国家能源局", "能源局")),
    Region("cnipa", "国家知识产权局", "national", "国务院 / 部委级", "cnipa.gov.cn",
           aliases=("国家知识产权局", "知识产权局")),
    Region("stats", "国家统计局", "national", "国务院 / 部委级", "stats.gov.cn",
           aliases=("国家统计局", "统计局")),
    Region("customs", "海关总署", "national", "国务院 / 部委级", "customs.gov.cn",
           aliases=("海关总署",)),
    Region("caac", "中国民用航空局", "national", "国务院 / 部委级", "caac.gov.cn",
           aliases=("中国民用航空局", "民航局")),
    Region("nmpa", "国家药品监督管理局", "national", "国务院 / 部委级", "nmpa.gov.cn",
           aliases=("国家药品监督管理局", "国家药监局", "药监局")),
    Region("nda", "国家数据局", "national", "国务院 / 部委级", "nda.gov.cn",
           aliases=("国家数据局",)),
    Region("forestry", "国家林业和草原局", "national", "国务院 / 部委级", "forestry.gov.cn",
           aliases=("国家林业和草原局", "国家林草局", "林草局")),
]

# ---------------------------------------------------------------------------
# 2. 34 个省级行政区（23 省 + 5 自治区 + 4 直辖市 + 2 特别行政区）
# ---------------------------------------------------------------------------
PROVINCES: List[Region] = [
    # —— 直辖市 ——
    Region("beijing", "北京市", "province", "省级 / 直辖市", "beijing.gov.cn",
           aliases=("北京市", "北京"), doc_prefix="京"),
    Region("tianjin", "天津市", "province", "省级 / 直辖市", "tj.gov.cn",
           aliases=("天津市", "天津"), doc_prefix="津"),
    Region("shanghai", "上海市", "province", "省级 / 直辖市", "shanghai.gov.cn",
           aliases=("上海市", "上海"), doc_prefix="沪"),
    Region("chongqing", "重庆市", "province", "省级 / 直辖市", "cq.gov.cn",
           aliases=("重庆市", "重庆"), doc_prefix="渝"),
    # —— 省 ——
    Region("hebei", "河北省", "province", "省级", "hebei.gov.cn",
           aliases=("河北省", "河北"), doc_prefix="冀"),
    Region("shanxi", "山西省", "province", "省级", "shanxi.gov.cn",
           aliases=("山西省", "山西"), doc_prefix="晋"),
    Region("liaoning", "辽宁省", "province", "省级", "ln.gov.cn",
           aliases=("辽宁省", "辽宁"), doc_prefix="辽"),
    Region("jilin", "吉林省", "province", "省级", "jl.gov.cn",
           aliases=("吉林省", "吉林"), doc_prefix="吉"),
    Region("heilongjiang", "黑龙江省", "province", "省级", "hlj.gov.cn",
           aliases=("黑龙江省", "黑龙江"), doc_prefix="黑"),
    Region("jiangsu", "江苏省", "province", "省级", "jiangsu.gov.cn",
           aliases=("江苏省", "江苏"), doc_prefix="苏"),
    Region("zhejiang", "浙江省", "province", "省级", "zj.gov.cn",
           aliases=("浙江省", "浙江"), doc_prefix="浙"),
    Region("anhui", "安徽省", "province", "省级", "ah.gov.cn",
           aliases=("安徽省", "安徽"), doc_prefix="皖"),
    Region("fujian", "福建省", "province", "省级", "fj.gov.cn",
           aliases=("福建省", "福建"), doc_prefix="闽"),
    Region("jiangxi", "江西省", "province", "省级", "jiangxi.gov.cn",
           aliases=("江西省", "江西"), doc_prefix="赣"),
    Region("shandong", "山东省", "province", "省级", "shandong.gov.cn",
           aliases=("山东省", "山东"), doc_prefix="鲁"),
    Region("henan", "河南省", "province", "省级", "henan.gov.cn",
           aliases=("河南省", "河南"), doc_prefix="豫"),
    Region("hubei", "湖北省", "province", "省级", "hubei.gov.cn",
           aliases=("湖北省", "湖北"), doc_prefix="鄂"),
    Region("hunan", "湖南省", "province", "省级", "hunan.gov.cn",
           aliases=("湖南省", "湖南"), doc_prefix="湘"),
    Region("guangdong", "广东省", "province", "省级", "gd.gov.cn",
           aliases=("广东省", "广东"), doc_prefix="粤"),
    Region("hainan", "海南省", "province", "省级", "hainan.gov.cn",
           aliases=("海南省", "海南"), doc_prefix="琼"),
    Region("sichuan", "四川省", "province", "省级", "sc.gov.cn",
           aliases=("四川省", "四川"), doc_prefix="川"),
    Region("guizhou", "贵州省", "province", "省级", "guizhou.gov.cn",
           aliases=("贵州省", "贵州"), doc_prefix="黔"),
    Region("yunnan", "云南省", "province", "省级", "yn.gov.cn",
           aliases=("云南省", "云南"), doc_prefix="滇"),
    Region("shaanxi", "陕西省", "province", "省级", "shaanxi.gov.cn",
           aliases=("陕西省", "陕西"), doc_prefix="陕"),
    Region("gansu", "甘肃省", "province", "省级", "gansu.gov.cn",
           aliases=("甘肃省", "甘肃"), doc_prefix="甘"),
    Region("qinghai", "青海省", "province", "省级", "qinghai.gov.cn",
           aliases=("青海省", "青海"), doc_prefix="青"),
    Region("taiwan", "中国台湾省", "province", "省级", "",
           aliases=("台湾省", "中国台湾省", "台湾")),
    # —— 自治区 ——
    Region("neimenggu", "内蒙古自治区", "province", "省级 / 自治区", "nmg.gov.cn",
           aliases=("内蒙古自治区", "内蒙古", "内蒙"), doc_prefix="蒙"),
    Region("guangxi", "广西壮族自治区", "province", "省级 / 自治区", "gxzf.gov.cn",
           aliases=("广西壮族自治区", "广西"), doc_prefix="桂"),
    Region("xizang", "西藏自治区", "province", "省级 / 自治区", "xizang.gov.cn",
           aliases=("西藏自治区", "西藏"), doc_prefix="藏"),
    Region("ningxia", "宁夏回族自治区", "province", "省级 / 自治区", "nx.gov.cn",
           aliases=("宁夏回族自治区", "宁夏"), doc_prefix="宁"),
    Region("xinjiang", "新疆维吾尔自治区", "province", "省级 / 自治区", "xinjiang.gov.cn",
           aliases=("新疆维吾尔自治区", "新疆"), doc_prefix="新"),
    # —— 特别行政区 ——
    Region("hongkong", "中国香港特别行政区", "province", "省级 / 特别行政区", "gov.hk",
           aliases=("香港特别行政区", "中国香港", "香港")),
    Region("macao", "中国澳门特别行政区", "province", "省级 / 特别行政区", "gov.mo",
           aliases=("澳门特别行政区", "中国澳门", "澳门")),
]

# ---------------------------------------------------------------------------
# 3. 重点城市（覆盖全部省会 / 计划单列市 / 主要经济城市）
# ---------------------------------------------------------------------------
CITIES: List[Region] = [
    Region("beijing_city", "北京市", "city", "地市级（直辖市）", "beijing.gov.cn", "beijing",
           ("北京市", "北京"), "京"),
    Region("hangzhou", "杭州市", "city", "地市级", "hangzhou.gov.cn", "zhejiang",
           ("杭州市", "杭州"), "杭"),
    Region("ningbo", "宁波市", "city", "地市级", "ningbo.gov.cn", "zhejiang",
           ("宁波市", "宁波"), "甬"),
    Region("wenzhou", "温州市", "city", "地市级", "wenzhou.gov.cn", "zhejiang",
           ("温州市", "温州")),
    Region("jiaxing", "嘉兴市", "city", "地市级", "jiaxing.gov.cn", "zhejiang",
           ("嘉兴市", "嘉兴")),
    Region("shaoxing", "绍兴市", "city", "地市级", "shaoxing.gov.cn", "zhejiang",
           ("绍兴市", "绍兴")),
    Region("jinhua", "金华市", "city", "地市级", "jinhua.gov.cn", "zhejiang",
           ("金华市", "金华")),
    Region("shenzhen", "深圳市", "city", "地市级 / 计划单列市", "sz.gov.cn", "guangdong",
           ("深圳市", "深圳"), "深"),
    Region("guangzhou", "广州市", "city", "地市级", "gz.gov.cn", "guangdong",
           ("广州市", "广州"), "穗"),
    Region("dongguan", "东莞市", "city", "地市级", "dg.gov.cn", "guangdong",
           ("东莞市", "东莞")),
    Region("foshan", "佛山市", "city", "地市级", "foshan.gov.cn", "guangdong",
           ("佛山市", "佛山")),
    Region("zhuhai", "珠海市", "city", "地市级", "zhuhai.gov.cn", "guangdong",
           ("珠海市", "珠海")),
    Region("huizhou", "惠州市", "city", "地市级", "huizhou.gov.cn", "guangdong",
           ("惠州市", "惠州")),
    Region("zhongshan", "中山市", "city", "地市级", "zs.gov.cn", "guangdong",
           ("中山市", "中山")),
    Region("shantou", "汕头市", "city", "地市级", "shantou.gov.cn", "guangdong",
           ("汕头市", "汕头")),
    Region("changsha", "长沙市", "city", "地市级", "changsha.gov.cn", "hunan",
           ("长沙市", "长沙"), "长"),
    Region("zhuzhou", "株洲市", "city", "地市级", "zhuzhou.gov.cn", "hunan",
           ("株洲市", "株洲")),
    Region("xiangtan", "湘潭市", "city", "地市级", "xiangtan.gov.cn", "hunan",
           ("湘潭市", "湘潭")),
    Region("wuhan", "武汉市", "city", "地市级", "wuhan.gov.cn", "hubei",
           ("武汉市", "武汉"), "汉"),
    Region("yichang", "宜昌市", "city", "地市级", "yichang.gov.cn", "hubei",
           ("宜昌市", "宜昌")),
    Region("xiangyang", "襄阳市", "city", "地市级", "xiangyang.gov.cn", "hubei",
           ("襄阳市", "襄阳")),
    Region("nanjing", "南京市", "city", "地市级", "nanjing.gov.cn", "jiangsu",
           ("南京市", "南京")),
    Region("suzhou", "苏州市", "city", "地市级", "suzhou.gov.cn", "jiangsu",
           ("苏州市", "苏州")),
    Region("wuxi", "无锡市", "city", "地市级", "wuxi.gov.cn", "jiangsu",
           ("无锡市", "无锡")),
    Region("changzhou", "常州市", "city", "地市级", "changzhou.gov.cn", "jiangsu",
           ("常州市", "常州")),
    Region("nantong", "南通市", "city", "地市级", "nantong.gov.cn", "jiangsu",
           ("南通市", "南通")),
    Region("xuzhou", "徐州市", "city", "地市级", "xz.gov.cn", "jiangsu",
           ("徐州市", "徐州")),
    Region("chengdu", "成都市", "city", "地市级", "chengdu.gov.cn", "sichuan",
           ("成都市", "成都"), "蓉"),
    Region("mianyang", "绵阳市", "city", "地市级", "mianyang.gov.cn", "sichuan",
           ("绵阳市", "绵阳")),
    Region("xian", "西安市", "city", "地市级", "xa.gov.cn", "shaanxi",
           ("西安市", "西安"), "西"),
    Region("zhengzhou", "郑州市", "city", "地市级", "zhengzhou.gov.cn", "henan",
           ("郑州市", "郑州"), "郑"),
    Region("luoyang", "洛阳市", "city", "地市级", "luoyang.gov.cn", "henan",
           ("洛阳市", "洛阳")),
    Region("hefei", "合肥市", "city", "地市级", "hefei.gov.cn", "anhui",
           ("合肥市", "合肥"), "合"),
    Region("jinan", "济南市", "city", "地市级", "jinan.gov.cn", "shandong",
           ("济南市", "济南"), "济"),
    Region("qingdao", "青岛市", "city", "地市级 / 计划单列市", "qingdao.gov.cn", "shandong",
           ("青岛市", "青岛"), "青"),
    Region("yantai", "烟台市", "city", "地市级", "yantai.gov.cn", "shandong",
           ("烟台市", "烟台")),
    Region("weifang", "潍坊市", "city", "地市级", "weifang.gov.cn", "shandong",
           ("潍坊市", "潍坊")),
    Region("xiamen", "厦门市", "city", "地市级 / 计划单列市", "xm.gov.cn", "fujian",
           ("厦门市", "厦门"), "厦"),
    Region("fuzhou", "福州市", "city", "地市级", "fuzhou.gov.cn", "fujian",
           ("福州市", "福州")),
    Region("quanzhou", "泉州市", "city", "地市级", "quanzhou.gov.cn", "fujian",
           ("泉州市", "泉州")),
    Region("nanchang", "南昌市", "city", "地市级", "nanchang.gov.cn", "jiangxi",
           ("南昌市", "南昌"), "洪"),
    Region("kunming", "昆明市", "city", "地市级", "km.gov.cn", "yunnan",
           ("昆明市", "昆明"), "昆"),
    Region("guiyang", "贵阳市", "city", "地市级", "guiyang.gov.cn", "guizhou",
           ("贵阳市", "贵阳"), "筑"),
    Region("nanning", "南宁市", "city", "地市级", "nanning.gov.cn", "guangxi",
           ("南宁市", "南宁")),
    Region("haikou", "海口市", "city", "地市级", "haikou.gov.cn", "hainan",
           ("海口市", "海口")),
    Region("sanya", "三亚市", "city", "地市级", "sanya.gov.cn", "hainan",
           ("三亚市", "三亚")),
    Region("shenyang", "沈阳市", "city", "地市级", "shenyang.gov.cn", "liaoning",
           ("沈阳市", "沈阳"), "沈"),
    Region("dalian", "大连市", "city", "地市级 / 计划单列市", "dl.gov.cn", "liaoning",
           ("大连市", "大连"), "大"),
    Region("changchun", "长春市", "city", "地市级", "cc.gov.cn", "jilin",
           ("长春市", "长春")),
    Region("harbin", "哈尔滨市", "city", "地市级", "harbin.gov.cn", "heilongjiang",
           ("哈尔滨市", "哈尔滨"), "哈"),
    Region("shijiazhuang", "石家庄市", "city", "地市级", "sjz.gov.cn", "hebei",
           ("石家庄市", "石家庄")),
    Region("taiyuan", "太原市", "city", "地市级", "taiyuan.gov.cn", "shanxi",
           ("太原市", "太原")),
    Region("hohhot", "呼和浩特市", "city", "地市级", "hhht.gov.cn", "neimenggu",
           ("呼和浩特市", "呼和浩特")),
    Region("lanzhou", "兰州市", "city", "地市级", "lanzhou.gov.cn", "gansu",
           ("兰州市", "兰州"), "兰"),
    Region("xining", "西宁市", "city", "地市级", "xining.gov.cn", "qinghai",
           ("西宁市", "西宁")),
    Region("yinchuan", "银川市", "city", "地市级", "yinchuan.gov.cn", "ningxia",
           ("银川市", "银川"), "银"),
    Region("urumqi", "乌鲁木齐市", "city", "地市级", "urumqi.gov.cn", "xinjiang",
           ("乌鲁木齐市", "乌鲁木齐"), "乌"),
    Region("lhasa", "拉萨市", "city", "地市级", "lasa.gov.cn", "xizang",
           ("拉萨市", "拉萨")),
]

# ---------------------------------------------------------------------------
# 4. 索引与常量
# ---------------------------------------------------------------------------
ALL_REGIONS: List[Region] = MINISTRIES + PROVINCES + CITIES

_BY_KEY: Dict[str, Region] = {r.key: r for r in ALL_REGIONS}

# 别名 → Region（用于全称匹配；单字简称不进入此表，避免误命中）
_ALIAS_INDEX: Dict[str, Region] = {}
for _r in ALL_REGIONS:
    for _alias in (_r.name, *_r.aliases):
        if len(_alias) >= 2:
            _ALIAS_INDEX[_alias] = _r

# 长别名优先，同级情况城市优先于省级、省级优先于部委
_TIER_PRIORITY = {"city": 0, "province": 1, "national": 2}
_SORTED_ALIASES: List[Tuple[str, Region]] = sorted(
    _ALIAS_INDEX.items(),
    key=lambda kv: (-len(kv[0]), _TIER_PRIORITY.get(kv[1].tier, 9)),
)

# 发文字号代字 → Region（仅收录可唯一指向的机关代字）
_DOC_PREFIX_CITY: Dict[str, str] = {
    "杭": "hangzhou", "深": "shenzhen", "穗": "guangzhou", "蓉": "chengdu",
    "汉": "wuhan", "甬": "ningbo", "厦": "xiamen", "济": "jinan", "合": "hefei",
    "郑": "zhengzhou", "沈": "shenyang", "哈": "harbin", "昆": "kunming",
    "筑": "guiyang", "洪": "nanchang", "长": "changsha", "西": "xian",
    "兰": "lanzhou", "银": "yinchuan", "乌": "urumqi", "青": "qingdao",
    "大": "dalian",
}
_DOC_PREFIX_PROVINCE: Dict[str, str] = {
    r.doc_prefix: r.key for r in PROVINCES if r.doc_prefix
}
# 发文字号前的机关代字短语，如「京政办发」「深府规」「长工信发」「浙政发」
# 关键：必须以「〔2020〕」这类年份括号收尾，才能把代字与正文里的普通词汇区分开。
# 早期版本用「单字 + 政/发」做匹配，会把「大规模」里的「大规」、「经济发展」里的「济发」
# 误判为大连市、济南市，因此这里改为锚定括号的严格模式。
_DOC_TOKEN_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,12})\s*[〔\[\(（]\s*((?:19|20)\d{2})\s*[〕\]\)）]"
)

# 前端“一键演示”预设（点击后自动填充并触发核验）
QUICK_REGIONS: List[Dict[str, str]] = [
    {"key": "national", "label": "🏛️ 全国部委", "region_name": "全国（国务院）"},
    {"key": "beijing", "label": "📍 北京（已替代）", "region_name": "北京市"},
    {"key": "hangzhou", "label": "📍 杭州（现行有效）", "region_name": "杭州市"},
    {"key": "shenzhen", "label": "📍 深圳（已废止）", "region_name": "深圳市"},
    {"key": "changsha", "label": "📍 长沙（窗口期核验）", "region_name": "长沙市"},
]

NATIONAL_REGION: Region = _BY_KEY["national"]


# ---------------------------------------------------------------------------
# 5. 对外函数
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    """去掉空白、全角空格与零宽字符，避免"杭 政 办 发"这类干扰。"""
    if not text:
        return ""
    return re.sub(r"[\s\u3000\u200b\u200c\u200d\ufeff]+", "", text)


def resolve_region(key_or_name: str) -> Optional[Region]:
    """按 key / 全称 / 别名解析单个行政区划。"""
    if not key_or_name:
        return None
    token = key_or_name.strip()
    if token in _BY_KEY:
        return _BY_KEY[token]
    if token in _ALIAS_INDEX:
        return _ALIAS_INDEX[token]
    # 允许 "杭州市（浙江省）" 这类带括号的展示串
    for alias, region in _SORTED_ALIASES:
        if alias and alias in token:
            return region
    return None


def _match_doc_prefix(text: str) -> Optional[Tuple[Region, str]]:
    """
    从发文字号中识别机关代字，如 杭政办发〔2021〕35号 → 杭州市。

    算法：先定位所有「…〔YYYY〕」结构，取其紧邻的机关代字短语（如「杭政办函」），
    再用短语首字去查代字表。这样既保留了公文代字的高置信度，
    又不会把正文里的普通词汇（大规模 / 经济发展）误认为地名。
    """
    # 长春 / 长沙 的“长”字冲突，用显式地名先行纠正
    if "长春" in text or "吉林" in text:
        city_prefix = {k: v for k, v in _DOC_PREFIX_CITY.items() if k != "长"}
    else:
        city_prefix = _DOC_PREFIX_CITY

    for match in _DOC_TOKEN_RE.finditer(text):
        phrase = match.group(1)
        if not phrase:
            continue
        head = phrase[0]

        if head in city_prefix:
            return _BY_KEY[city_prefix[head]], phrase
        if head in _DOC_PREFIX_PROVINCE:
            return _BY_KEY[_DOC_PREFIX_PROVINCE[head]], phrase
        # 「国发〔2024〕7号」这类中央文件没有地方代字，交由后续别名匹配处理
        if head in {"国", "中", "全"}:
            return NATIONAL_REGION, phrase

    return None


def detect_region(raw_text: str, hint: str = "auto") -> RegionMatch:
    """
    从政策文本 + 用户指定地域中识别最可能的检索属地。

    参数
    ----
    raw_text : 用户输入的政策标题 / 文号 / 正文
    hint     : "auto" 或具体的 region key / 名称
    """
    text = _normalize(raw_text)

    # 1) 用户显式指定，最高优先级
    if hint and hint.strip().lower() != "auto":
        region = resolve_region(hint)
        if region is not None:
            return RegionMatch(region, 1.0, "user_hint", f"用户指定：{region.name}")

    # 2) 发文字号机关代字（比地名更可靠，因为公文必然带代字）
    doc_hit = _match_doc_prefix(text)
    if doc_hit is not None:
        region, evidence = doc_hit
        return RegionMatch(region, 0.94, "doc_prefix", f"发文字号机关代字「{evidence}」")

    # 3) 全称 / 别名匹配（长别名优先，城市优先）
    for alias, region in _SORTED_ALIASES:
        if alias in text:
            confidence = 0.92 if region.tier == "city" else 0.88
            if region.tier == "national":
                confidence = 0.86
            return RegionMatch(region, confidence, "text_alias", f"文本命中「{alias}」")

    # 4) 兜底：全国通查
    return RegionMatch(NATIONAL_REGION, 0.40, "fallback", "未识别到具体属地，回退全国通查")


def list_regions() -> Dict[str, Any]:
    """供 /api/v1/regions 返回的完整行政区划字典。"""
    provinces = [r.to_dict() for r in PROVINCES]
    cities = [r.to_dict() for r in CITIES]
    ministries = [r.to_dict() for r in MINISTRIES]

    # 按省级分组，便于前端做级联下拉
    city_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for city in cities:
        city_by_parent.setdefault(city["parent"], []).append(city)

    return {
        "counts": {
            "provinces": len(provinces),
            "cities": len(cities),
            "ministries": len(ministries),
            "total": len(provinces) + len(cities) + len(ministries),
        },
        "provinces": provinces,
        "cities": cities,
        "ministries": ministries,
        "city_by_parent": city_by_parent,
        "quick_presets": QUICK_REGIONS,
        "notes": [
            "覆盖 34 个省级行政区（23 省 + 5 自治区 + 4 直辖市 + 2 特别行政区）。",
            "中国台湾省、中国香港特别行政区、中国澳门特别行政区按官方表述收录。",
            "港澳台地区无 *.gov.cn 站点，site 过滤回退至 gov.hk / gov.mo 或全库检索。",
        ],
    }


def build_site_expression(region: Region, extra_domains: Iterable[str] = ()) -> str:
    """生成 `(site:a OR site:b)` 形式的域限定表达式。"""
    domains = [d for d in [region.gov_domain, *extra_domains] if d]
    if not domains:
        return "site:gov.cn"
    if len(domains) == 1:
        return f"site:{domains[0]}"
    joined = " OR ".join(f"site:{d}" for d in domains)
    return f"({joined})"
